"""Short-lived, exact-scope grants for external actions."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from sqlalchemy import select

from .auth import OwnerPrincipal
from .db import Database
from .models import ActionAuthorization, TrialBatch, utc_now


class AuthorizationError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 409):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def payload_digest(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


class AuthorizationService:
    """Issue one-use owner grants bound to one message segment and target."""

    def __init__(
        self,
        db: Database,
        session_factory,
        *,
        get_delivery_guard: Callable,
        get_deployment_profile: Callable | None = None,
    ):
        self.db = db
        self.session_factory = session_factory
        self.get_delivery_guard = get_delivery_guard
        self.get_deployment_profile = get_deployment_profile or (lambda: "local_trial")

    async def issue_message_grant(
        self,
        *,
        principal: OwnerPrincipal,
        conversation_id: str,
        expected_revision: int,
        batch_id: str,
        payload: str,
        action_id: str = "qq.message.send",
        expires_in_seconds: int = 600,
    ) -> dict[str, Any]:
        if self.get_deployment_profile() != "local_trial":
            raise AuthorizationError("external_actions_disabled", "target_staging 禁止签发外部动作授权。", 409)
        if principal.role != "owner":
            raise AuthorizationError("owner_required", "只有管理员可以为外部动作签发授权。", 403)
        if action_id not in {"qq.message.send", "qq.voice.send"}:
            raise AuthorizationError("unsupported_action", "当前只允许为QQ文本或语音动作签发授权。", 422)
        if action_id == "qq.message.send" and (not payload or len(payload) > 4000):
            raise AuthorizationError("invalid_payload", "外发消息内容必须为1到4000个字符。", 422)
        if action_id == "qq.voice.send":
            try:
                record_ref = json.loads(payload)
            except (TypeError, ValueError) as exc:
                raise AuthorizationError("invalid_voice_binding", "语音授权必须绑定服务端解析的登记产物。", 422) from exc
            if (
                not isinstance(record_ref, dict)
                or set(record_ref) != {"artifact_id", "sha256", "voice_version", "approval_ref"}
                or not all(isinstance(record_ref.get(key), str) and record_ref[key] for key in record_ref)
                or len(payload) > 2048
            ):
                raise AuthorizationError("invalid_voice_binding", "语音授权必须绑定服务端解析的登记产物。", 422)
        if type(expected_revision) is not int or expected_revision < 0:
            raise AuthorizationError("invalid_revision", "会话版本号无效。", 422)
        if type(expires_in_seconds) is not int or not 1 <= expires_in_seconds <= 1800:
            raise AuthorizationError("invalid_expiry", "授权有效期需为1到1800秒。", 422)

        guard = await self.get_delivery_guard(conversation_id)
        self._assert_live_guard(
            guard,
            conversation_id=conversation_id,
            expected_revision=expected_revision,
            batch_id=batch_id,
            action_id=action_id,
        )
        batch = await self._load_batch(batch_id, guard["recipient_id"], action_id, allow_draft=True)

        now = utc_now()
        grant_expiry = min(now + timedelta(seconds=expires_in_seconds), _utc(batch.expires_at))
        row = ActionAuthorization(
            id=str(uuid.uuid4()),
            action_id=action_id,
            actor_id=f"admin:{principal.admin_id}",
            person_id=str(guard["person_id"]),
            conversation_id=conversation_id,
            conversation_revision=expected_revision,
            delivery_mode=str(guard["delivery_mode"]),
            recipient_id=str(guard["recipient_id"]),
            account_id=str(guard["account_id"]),
            batch_id=batch_id,
            batch_version=int(batch.version),
            payload_hash=payload_digest(payload),
            max_uses=1,
            used_uses=0,
            source="owner_explicit",
            expires_at=grant_expiry,
            created_at=now,
        )
        async with self.db.immediate_transaction() as connection:
            await connection.execute(ActionAuthorization.__table__.insert().values(**self._values(row)))
        return self.serialize(row)

    async def validate_for_item(self, item: Any, *, connection=None) -> dict[str, Any]:
        if not item.get("authorization_id"):
            raise AuthorizationError("authorization_required", "此消息片段没有对应的明确授权。", 403)
        guard = await self.get_delivery_guard(item["conversation_id"])
        self._assert_live_guard(
            guard,
            conversation_id=item["conversation_id"],
            expected_revision=item["expected_revision"],
            batch_id=item["batch_id"],
            action_id=item["action_id"],
            recipient_id=item["recipient_id"],
            account_id=item["account_id"],
            person_id=item["person_id"],
            delivery_mode=item["delivery_mode"],
        )
        if connection is None:
            async with self.db.engine.connect() as own_connection:
                grant = await self._read_grant(own_connection, item["authorization_id"])
        else:
            grant = await self._read_grant(connection, item["authorization_id"])
        if grant is None:
            raise AuthorizationError("authorization_not_found", "消息授权不存在。", 403)

        now = utc_now()
        if _utc(grant["expires_at"]) <= now or grant["revoked_at"] is not None:
            raise AuthorizationError("authorization_expired", "消息授权已过期或撤销。", 403)
        expected = {
            "action_id": item["action_id"],
            "actor_id": item["actor_id"],
            "person_id": item["person_id"],
            "conversation_id": item["conversation_id"],
            "conversation_revision": item["expected_revision"],
            "delivery_mode": item["delivery_mode"],
            "recipient_id": item["recipient_id"],
            "account_id": item["account_id"],
            "batch_id": item["batch_id"],
            "batch_version": item["batch_version"],
            "payload_hash": item["payload_hash"],
        }
        if any(grant[key] != value for key, value in expected.items()):
            raise AuthorizationError("authorization_scope_mismatch", "授权范围与消息内容或目标不匹配。", 403)
        if int(grant["used_uses"]) >= int(grant["max_uses"]):
            raise AuthorizationError("authorization_consumed", "该消息授权已使用。", 403)
        return guard

    async def _load_batch(self, batch_id: str, recipient_id: str, action_id: str, *, allow_draft: bool) -> TrialBatch:
        async with self.session_factory() as session:
            batch = await session.get(TrialBatch, batch_id)
            if batch is None:
                raise AuthorizationError("batch_not_found", "试用批次不存在。", 404)
            allowed_states = {"draft", "running"} if allow_draft else {"running"}
            if batch.state not in allowed_states or _utc(batch.expires_at) <= utc_now():
                raise AuthorizationError("batch_unavailable", "试用批次未运行、已结束或已过期。", 409)
            if batch.module_id != "qq" or list(batch.object_ids or []) != [recipient_id]:
                raise AuthorizationError("batch_scope_mismatch", "试用批次未包含此QQ收件目标。", 403)
            if batch.capability_id != action_id:
                raise AuthorizationError("batch_capability_mismatch", "试用批次能力与消息动作不匹配。", 403)
            stop_condition = batch.stop_condition or {}
            max_actions = stop_condition.get("max_actions") if isinstance(stop_condition, dict) else None
            if (
                not isinstance(stop_condition, dict)
                or stop_condition.get("kind") != "after_n"
                or type(max_actions) is not int
                or not 1 <= max_actions <= 100
            ):
                raise AuthorizationError("finite_batch_required", "QQ外发批次必须有有效的有限动作上限。", 409)
            if int(batch.action_count) >= max_actions:
                raise AuthorizationError("batch_action_limit", "QQ外发批次已达到动作上限。", 409)
            return batch

    @staticmethod
    def _assert_live_guard(
        guard: dict[str, Any] | None,
        *,
        conversation_id: str,
        expected_revision: int,
        batch_id: str,
        action_id: str = "qq.message.send",
        recipient_id: str | None = None,
        account_id: str | None = None,
        person_id: str | None = None,
        delivery_mode: str | None = None,
    ) -> None:
        if guard is None:
            raise AuthorizationError("delivery_guard_unavailable", "当前会话无法验证外发状态。", 503)
        if (
            guard.get("conversation_id") != conversation_id
            or guard.get("revision") != expected_revision
            or guard.get("state") != "active"
            or bool(guard.get("stop_requested"))
            or bool(guard.get("human_takeover"))
        ):
            raise AuthorizationError("conversation_stale", "会话已停止、被接管或发生变化，授权已失效。", 409)
        if guard.get("channel") != "qq" or guard.get("delivery_mode") != "qq":
            raise AuthorizationError("delivery_mode_unavailable", "当前会话不是可外发的QQ模式。", 403)
        if guard.get("scope") != "production":
            raise AuthorizationError("delivery_scope_unavailable", "校准会话不能用于QQ外发。", 403)
        if not guard.get("person_id") or not guard.get("recipient_id") or not guard.get("account_id"):
            raise AuthorizationError("delivery_target_unavailable", "会话缺少已核验的人物、账号或收件目标。", 403)
        if action_id != "qq.voice.send" and guard.get("batch_id") != batch_id:
            raise AuthorizationError("batch_scope_mismatch", "会话绑定的批次已变化。", 403)
        expected_values = {
            "recipient_id": recipient_id,
            "account_id": account_id,
            "person_id": person_id,
            "delivery_mode": delivery_mode,
        }
        if any(value is not None and guard.get(key) != value for key, value in expected_values.items()):
            raise AuthorizationError("delivery_target_changed", "会话账号、收件目标或模式已变化，授权已失效。", 409)

    @staticmethod
    async def _read_grant(connection, grant_id: str):
        return (
            await connection.execute(
                select(ActionAuthorization.__table__).where(ActionAuthorization.id == grant_id)
            )
        ).mappings().first()

    @staticmethod
    def _values(row: ActionAuthorization) -> dict[str, Any]:
        return {column.name: getattr(row, column.name) for column in ActionAuthorization.__table__.columns}

    @staticmethod
    def serialize(row: ActionAuthorization) -> dict[str, Any]:
        return {
            "id": row.id,
            "action_id": row.action_id,
            "person_id": row.person_id,
            "conversation_id": row.conversation_id,
            "conversation_revision": row.conversation_revision,
            "delivery_mode": row.delivery_mode,
            "recipient_id": row.recipient_id,
            "account_id": row.account_id,
            "batch_id": row.batch_id,
            "batch_version": row.batch_version,
            "payload_hash": row.payload_hash,
            "expires_at": _utc(row.expires_at).isoformat(),
            "state": "active",
        }
