"""Persistent trial-batch records without implicit external action execution."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from .models import TrialBatch, utc_now
from .registry import ModuleRegistry


class BatchError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 422):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


class BatchService:
    def __init__(self, session_factory, registry: ModuleRegistry, *, get_deployment_profile=None):
        self.session_factory = session_factory
        self.registry = registry
        self.get_deployment_profile = get_deployment_profile or (lambda: "local_trial")

    async def list(self) -> list[dict[str, Any]]:
        async with self.session_factory() as session:
            rows = (await session.scalars(select(TrialBatch).order_by(TrialBatch.created_at.desc()))).all()
            return [self.serialize(row) for row in rows]

    async def create(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.get_deployment_profile() != "local_trial":
            raise BatchError("batch_create_disabled", "target_staging 禁止创建新的本机试用批次。", 409)
        if not isinstance(payload, dict):
            raise BatchError("invalid_batch_payload", "试用批次数据格式无效。")
        target = payload.get("target")
        if not isinstance(target, dict):
            raise BatchError("invalid_batch_target", "批次目标数据格式无效。")
        module_id = target.get("module_id")
        if not isinstance(module_id, str) or not module_id:
            raise BatchError("invalid_batch_target", "批次目标必须指定有效模块。")
        capability_id = target.get("capability_id")
        module = self.registry.get(module_id)
        if module is None:
            raise BatchError("module_not_found", "目标模块尚未注册，无法创建试用批次。", 404)
        capability = self.registry.capability(module_id, capability_id) if capability_id else None
        if capability_id and capability is None:
            raise BatchError("capability_not_found", "目标能力不属于此模块。", 404)

        object_ids = target.get("object_ids", [])
        if not isinstance(object_ids, list) or any(not isinstance(item, str) or not item.strip() or len(item) > 256 for item in object_ids):
            raise BatchError("invalid_batch_target", "批次目标必须是非空的稳定标识。")
        if len(set(object_ids)) != len(object_ids):
            raise BatchError("invalid_batch_target", "批次目标不能重复。")

        expires_at: datetime = payload["expires_at"]
        if _utc(expires_at) <= utc_now():
            raise BatchError("invalid_expiry", "批次截止时间必须在未来。")
        if (_utc(expires_at) - utc_now()).total_seconds() > 7 * 24 * 60 * 60:
            raise BatchError("expiry_too_far", "本机试用批次的有效期不能超过7天。")
        stop_condition = payload.get("stop_condition")
        if not isinstance(stop_condition, dict):
            raise BatchError("invalid_stop_condition", "批次停止条件数据格式无效。")
        if stop_condition.get("kind") == "after_n":
            max_actions = stop_condition.get("max_actions")
            if type(max_actions) is not int or not 1 <= max_actions <= 100:
                raise BatchError("invalid_stop_condition", "动作上限需为1到100。")

        if capability is not None and capability.side_effect.value == "external_write":
            self._validate_local_external_batch(
                module_id=module_id,
                capability_id=capability_id,
                object_ids=object_ids,
                stop_condition=stop_condition,
            )

        row = TrialBatch(
            id=str(uuid.uuid4()),
            label=payload["label"].strip(),
            module_id=module_id,
            capability_id=capability_id,
            object_ids=list(object_ids),
            stop_condition=stop_condition,
            expires_at=_utc(expires_at),
            state="draft",
            action_count=0,
            created_at=utc_now(),
            version=1,
        )
        if not row.label:
            raise BatchError("invalid_label", "批次名称不能为空。")
        async with self.session_factory() as session:
            async with session.begin():
                session.add(row)
                await session.flush()
                return self.serialize(row)

    async def start(self, batch_id: str) -> dict[str, Any]:
        if self.get_deployment_profile() != "local_trial":
            raise BatchError("batch_start_disabled", "target_staging 禁止启动或恢复试用批次。", 409)
        expired = False
        async with self.session_factory() as session:
            async with session.begin():
                row = await session.get(TrialBatch, batch_id)
                if row is None:
                    raise BatchError("batch_not_found", "批次不存在。", 404)
                if row.state != "draft":
                    raise BatchError("invalid_batch_state", "只有草稿批次可以启动。", 409)
                if _utc(row.expires_at) <= utc_now():
                    row.state = "expired"
                    row.version += 1
                    await session.flush()
                    expired = True
                if not expired:
                    module = self.registry.get(row.module_id)
                    capability = (
                        self.registry.capability(row.module_id, row.capability_id)
                        if row.capability_id
                        else None
                    )
                    if module is None:
                        raise BatchError("module_not_found", "目标模块尚未注册。", 404)
                    if capability is not None and capability.side_effect.value == "external_write":
                        # Starting a finite local-trial batch records its state only.
                        # Grants and the exact Outbox path are still required for each
                        # later action; this method never calls a module handler.
                        self._validate_local_external_batch(
                            module_id=row.module_id,
                            capability_id=row.capability_id,
                            object_ids=list(row.object_ids or []),
                            stop_condition=row.stop_condition,
                        )
                    else:
                        if capability is None and row.capability_id:
                            raise BatchError("capability_not_found", "目标能力不属于此模块。", 404)
                        if not module.enabled or module.action_handler is None:
                            raise BatchError("action_unavailable", "模块没有可启动的本地执行器。", 409)
                    row.state = "running"
                    row.started_at = utc_now()
                    row.version += 1
                    await session.flush()
                    return self.serialize(row)
        raise BatchError("batch_expired", "批次已过期，不能启动。", 409)

    @staticmethod
    def _validate_local_external_batch(
        *,
        module_id: str,
        capability_id: str | None,
        object_ids: list[str],
        stop_condition: dict[str, Any],
    ) -> None:
        if module_id not in {"qq", "integrations"} or not capability_id:
            raise BatchError("action_not_authorized", "本机外发批次必须绑定已支持的精确外发能力。", 403)
        if module_id == "qq" and capability_id not in {"qq.message.send", "qq.voice.send"}:
            raise BatchError("action_not_authorized", "QQ外发批次只能绑定文本或语音发送能力。", 403)
        if module_id == "integrations" and not capability_id.startswith("hexi."):
            raise BatchError("action_not_authorized", "集成外发批次只能绑定已登记的Hexi能力。", 403)
        if len(object_ids) != 1:
            raise BatchError("single_target_required", "本机外发批次必须只包含一个精确目标。", 422)
        if not isinstance(stop_condition, dict):
            raise BatchError("finite_batch_required", "本机外发批次必须使用1到100次的有限动作上限。", 422)
        max_actions = stop_condition.get("max_actions")
        if stop_condition.get("kind") != "after_n" or type(max_actions) is not int or not 1 <= max_actions <= 100:
            raise BatchError("finite_batch_required", "本机外发批次必须使用1到100次的有限动作上限。", 422)

    async def stop(self, batch_id: str) -> dict[str, Any]:
        async with self.session_factory() as session:
            async with session.begin():
                row = await session.get(TrialBatch, batch_id)
                if row is None:
                    raise BatchError("batch_not_found", "批次不存在。", 404)
                if row.state not in {"draft", "running"}:
                    raise BatchError("invalid_batch_state", "此批次已经结束。", 409)
                row.state = "stopped"
                row.stopped_at = utc_now()
                row.stop_reason = "manual"
                row.version += 1
                await session.flush()
                return self.serialize(row)

    @staticmethod
    def serialize(row: TrialBatch) -> dict[str, Any]:
        return {
            "id": row.id,
            "label": row.label,
            "state": row.state,
            "mode": "local_trial",
            "target": {
                "module_id": row.module_id,
                "capability_id": row.capability_id,
                "object_ids": list(row.object_ids),
            },
            "expires_at": _utc(row.expires_at).isoformat(),
            "stop_condition": row.stop_condition,
            "created_at": _utc(row.created_at).isoformat(),
            "action_count": row.action_count,
            "version": row.version,
        }

