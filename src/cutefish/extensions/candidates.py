"""Persistent background candidates, kept separate from task and outbox states."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import select

from .models import TaskCandidate, TaskRecord, utc_now


CANDIDATE_STATES = frozenset({"proposed", "draft", "prepared", "cancelled", "expired"})


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


class CandidateError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 422):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


class CandidateService:
    def __init__(self, session_factory, *, current_revisions=None):
        self.session_factory = session_factory
        self.current_revisions = current_revisions

    async def list(self, *, state: str | None = None, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        if state is not None and state not in CANDIDATE_STATES:
            raise CandidateError("invalid_candidate_state", "候选状态不受支持。")
        async with self.session_factory() as session:
            statement = select(TaskCandidate)
            if state:
                statement = statement.where(TaskCandidate.state == state)
            rows = (
                await session.scalars(
                    statement.order_by(TaskCandidate.created_at.desc(), TaskCandidate.id).limit(limit).offset(offset)
                )
            ).all()
            return {"items": [self.serialize(row) for row in rows], "limit": limit, "offset": offset}

    async def get(self, candidate_id: str) -> dict[str, Any]:
        async with self.session_factory() as session:
            row = await session.get(TaskCandidate, candidate_id)
            if row is None:
                raise CandidateError("candidate_not_found", "候选不存在。", 404)
            return self.serialize(row)

    async def create(self, payload: dict[str, Any]) -> dict[str, Any]:
        expires_at = payload.get("expires_at")
        if expires_at is not None and _utc(expires_at) <= utc_now():
            raise CandidateError("candidate_expired", "候选截止时间必须在未来。")
        if payload.get("task_id"):
            async with self.session_factory() as session:
                if await session.get(TaskRecord, payload["task_id"]) is None:
                    raise CandidateError("task_not_found", "候选关联的任务不存在。", 404)
        row = TaskCandidate(
            id=str(uuid.uuid4()),
            task_id=payload.get("task_id"),
            title=str(payload["title"]).strip(),
            target_ref=payload.get("target_ref"),
            source_event_refs=list(payload.get("source_event_refs", [])),
            experience_ref=payload.get("experience_ref"),
            reason=str(payload.get("reason", "")),
            context=dict(payload.get("context", {})),
            not_before=payload.get("not_before"),
            expires_at=expires_at,
            source_revision=int(payload.get("source_revision", 1)),
            permission_revision=int(payload.get("permission_revision", 1)),
            budget_estimate=(Decimal(str(payload["budget_estimate"])) if payload.get("budget_estimate") is not None else None),
            defer_reason=payload.get("defer_reason"),
            state="proposed",
            revision=1,
            outbox_id=None,
            created_at=utc_now(),
            updated_at=utc_now(),
        )
        if not row.title:
            raise CandidateError("invalid_candidate", "候选标题不能为空。")
        async with self.session_factory() as session:
            async with session.begin():
                session.add(row)
                await session.flush()
                return self.serialize(row)

    async def preview(self, candidate_id: str) -> dict[str, Any]:
        """Return a local-only preview; this never invokes a provider or dispatcher."""
        async with self.session_factory() as session:
            async with session.begin():
                row = await session.get(TaskCandidate, candidate_id)
                if row is None:
                    raise CandidateError("candidate_not_found", "候选不存在。", 404)
                if (
                    row.state not in {"cancelled", "expired"}
                    and row.expires_at is not None
                    and _utc(row.expires_at) <= utc_now()
                ):
                    row.state = "expired"
                    row.defer_reason = "expired"
                    row.revision += 1
                    row.updated_at = utc_now()
                await session.flush()
                return self.serialize(row)

    async def cancel(self, candidate_id: str, *, expected_revision: int, reason: str | None = None) -> dict[str, Any]:
        return await self._change(candidate_id, expected_revision, "cancel", reason=reason)

    async def reassess(self, candidate_id: str, *, expected_revision: int, reason: str | None = None) -> dict[str, Any]:
        return await self._change(candidate_id, expected_revision, "reassess", reason=reason)

    async def _change(self, candidate_id: str, expected_revision: int, action: str, *, reason: str | None) -> dict[str, Any]:
        async with self.session_factory() as session:
            async with session.begin():
                row = await session.get(TaskCandidate, candidate_id)
                if row is None:
                    raise CandidateError("candidate_not_found", "候选不存在。", 404)
                if row.revision != expected_revision:
                    raise CandidateError("candidate_revision_conflict", "候选已被更新，请刷新后重试。", 409)
                if action == "cancel":
                    if row.state in {"cancelled", "expired"}:
                        raise CandidateError("invalid_candidate_state", "候选已结束。", 409)
                    row.state = "cancelled"
                    row.defer_reason = reason or "owner_cancelled"
                elif action == "reassess":
                    if row.state in {"cancelled", "expired"}:
                        raise CandidateError("invalid_candidate_state", "已结束的候选不能重新评估。", 409)
                    if row.expires_at is not None and _utc(row.expires_at) <= utc_now():
                        row.state = "expired"
                        row.defer_reason = "expired"
                    elif reason:
                        row.defer_reason = reason
                    # This method only refreshes persisted metadata. A caller may
                    # supply new revisions later, but it never dispatches or
                    # retries unknown external results.
                row.revision += 1
                row.updated_at = utc_now()
                await session.flush()
                return self.serialize(row)

    @staticmethod
    def serialize(row: TaskCandidate) -> dict[str, Any]:
        return {
            "id": row.id,
            "task_id": row.task_id,
            "title": row.title,
            "target_ref": row.target_ref,
            "source_event_refs": list(row.source_event_refs),
            "experience_ref": row.experience_ref,
            "reason": row.reason,
            "context": dict(row.context),
            "not_before": _utc(row.not_before).isoformat() if row.not_before else None,
            "expires_at": _utc(row.expires_at).isoformat() if row.expires_at else None,
            "source_revision": row.source_revision,
            "permission_revision": row.permission_revision,
            "budget_estimate": float(row.budget_estimate) if row.budget_estimate is not None else None,
            "defer_reason": row.defer_reason,
            "state": row.state,
            "revision": row.revision,
            "outbox_id": row.outbox_id,
            "created_at": _utc(row.created_at).isoformat(),
            "updated_at": _utc(row.updated_at).isoformat(),
        }
