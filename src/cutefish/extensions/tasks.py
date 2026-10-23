"""Durable, local-only task controls. No method dispatches external actions."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from .models import TaskRecord, utc_now


TASK_STATUSES = frozenset({"open", "active", "paused", "done", "abandoned"})
EFFECT_STATES = frozenset({"not_dispatched", "pending", "sending", "sent", "failed", "unknown", "cancelled"})


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


class TaskError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 422):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


class TaskService:
    def __init__(self, session_factory, *, hard_lock_check=None, get_deployment_profile=None):
        self.session_factory = session_factory
        self.hard_lock_check = hard_lock_check
        self.get_deployment_profile = get_deployment_profile or (lambda: "local_trial")

    async def list(self, *, status: str | None = None, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        if status is not None and status not in TASK_STATUSES:
            raise TaskError("invalid_task_status", "任务状态不受支持。")
        async with self.session_factory() as session:
            statement = select(TaskRecord)
            if status:
                statement = statement.where(TaskRecord.status == status)
            statement = statement.order_by(TaskRecord.updated_at.desc(), TaskRecord.id).limit(limit).offset(offset)
            rows = (await session.scalars(statement)).all()
            return {"items": [self.serialize(row) for row in rows], "limit": limit, "offset": offset}

    async def get(self, task_id: str) -> dict[str, Any]:
        async with self.session_factory() as session:
            row = await session.get(TaskRecord, task_id)
            if row is None:
                raise TaskError("task_not_found", "任务不存在。", 404)
            return self.serialize(row)

    async def create(self, payload: dict[str, Any]) -> dict[str, Any]:
        title = str(payload["title"]).strip()
        intent = str(payload["intent"]).strip()
        if not title or not intent:
            raise TaskError("invalid_task", "任务标题和意图不能为空。")
        now = utc_now()
        staging = self.get_deployment_profile() != "local_trial"
        row = TaskRecord(
            id=str(uuid.uuid4()),
            title=title,
            intent=intent,
            progress=payload.get("progress"),
            next_step=payload.get("next_step"),
            status="paused" if staging else "open",
            source_refs=list(payload.get("source_refs", [])),
            capability_refs=list(payload.get("capability_refs", [])),
            budget_refs=list(payload.get("budget_refs", [])),
            revision=1,
            explicit_pause=staging,
            manual_takeover=False,
            pause_reason="target_staging" if staging else None,
            effect_state="not_dispatched",
            created_at=now,
            updated_at=now,
        )
        async with self.session_factory() as session:
            async with session.begin():
                session.add(row)
                await session.flush()
                return self.serialize(row)

    async def control(
        self,
        task_id: str,
        action: str,
        *,
        expected_revision: int,
        reason: str | None = None,
        release_handover: bool = False,
    ) -> dict[str, Any]:
        if action not in {"pause", "resume", "cancel", "handover"}:
            raise TaskError("invalid_task_action", "任务操作不受支持。")
        if release_handover and action != "resume":
            raise TaskError("invalid_task_action", "release_handover 只能用于恢复任务。")
        async with self.session_factory() as session:
            async with session.begin():
                row = await session.scalar(
                    select(TaskRecord).where(TaskRecord.id == task_id).with_for_update()
                )
                if row is None:
                    raise TaskError("task_not_found", "任务不存在。", 404)
                if row.revision != expected_revision:
                    raise TaskError("task_revision_conflict", "任务已被其他操作更新，请刷新后重试。", 409)
                if action == "pause":
                    if row.status in {"done", "abandoned"}:
                        raise TaskError("invalid_task_state", "已结束的任务不能暂停。", 409)
                    row.status = "paused"
                    row.explicit_pause = True
                    row.pause_reason = reason or "owner_pause"
                elif action == "resume":
                    if self.get_deployment_profile() != "local_trial":
                        raise TaskError("task_resume_disabled", "target_staging 禁止恢复任务。", 409)
                    if row.status in {"done", "abandoned"}:
                        raise TaskError("invalid_task_state", "已结束的任务不能恢复。", 409)
                    row.explicit_pause = False
                    if release_handover:
                        row.manual_takeover = False
                    # Resume clears only this task's explicit pause. Handover
                    # and global controls remain independent hard locks.
                    lock_reason = None
                    if self.hard_lock_check is not None:
                        lock_reason = await self.hard_lock_check(row.id)
                    if row.manual_takeover:
                        row.status = "paused"
                        row.pause_reason = "manual_takeover"
                    elif lock_reason:
                        row.status = "paused"
                        row.pause_reason = str(lock_reason)
                    else:
                        if row.status == "paused":
                            row.status = "open"
                        row.pause_reason = None
                elif action == "cancel":
                    if row.status in {"done", "abandoned"}:
                        raise TaskError("invalid_task_state", "已结束的任务不能取消。", 409)
                    row.status = "abandoned"
                    row.explicit_pause = True
                    row.pause_reason = reason or "owner_cancelled"
                    if row.effect_state in {"not_dispatched", "pending"}:
                        row.effect_state = "cancelled"
                elif action == "handover":
                    if row.status in {"done", "abandoned"}:
                        raise TaskError("invalid_task_state", "已结束的任务不能接管。", 409)
                    row.manual_takeover = True
                    row.status = "paused"
                    row.pause_reason = reason or "manual_takeover"
                row.revision += 1
                row.updated_at = utc_now()
                await session.flush()
                return self.serialize(row)

    @staticmethod
    def serialize(row: TaskRecord) -> dict[str, Any]:
        return {
            "id": row.id,
            "title": row.title,
            "intent": row.intent,
            "progress": row.progress,
            "next_step": row.next_step,
            "status": row.status,
            "source_refs": list(row.source_refs),
            "capability_refs": list(row.capability_refs),
            "budget_refs": list(row.budget_refs),
            "revision": row.revision,
            "explicit_pause": row.explicit_pause,
            "manual_takeover": row.manual_takeover,
            "pause_reason": row.pause_reason,
            "effect_state": row.effect_state,
            "created_at": _utc(row.created_at).isoformat(),
            "updated_at": _utc(row.updated_at).isoformat(),
        }
