"""Persistent message drafts with a closed, fail-closed delivery path."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Literal, Mapping

from sqlalchemy import func, select

from .authorization import AuthorizationError, AuthorizationService, payload_digest
from .contracts import ToolActionEnvelope, ToolActionPreview, ToolActionReceipt, VoiceOutboxBinding
from .db import Database
from .models import (
    ActionAuthorization,
    OutboxItem,
    ToolActionGrant,
    ToolActionOutboxItem,
    ToolActionPreviewRecord,
    TrialBatch,
    utc_now,
)
from cutefish.integrations.registry import HandlerUnavailable, PRODUCTION_HANDLER_REGISTRY


OUTBOX_STATES = frozenset({"prepared", "sending", "sent", "failed", "unknown", "cancelled"})
PENDING_STATES = ("prepared", "sending", "unknown")
_SAFE_RESULT_CODE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


@dataclass(frozen=True, slots=True)
class SenderReceipt:
    """Structured proof returned by a sender adapter after one dispatch attempt."""

    outcome: Literal["sent", "rejected"]
    evidence_ref: str
    remote_receipt_id: str | None = None
    result_code: str | None = None
    no_side_effect_proven: bool = False


class OutboxError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 409):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


class OutboxService:
    """Store reviewable QQ message segments. No sender is registered in this run."""

    def __init__(
        self,
        db: Database,
        session_factory,
        batches,
        authorizations: AuthorizationService,
        *,
        get_delivery_guard: Callable,
        get_deployment_profile: Callable | None = None,
        get_runtime_policy_valid: Callable | None = None,
        voice_binding_provider: Callable | None = None,
        voice_validator: Callable | None = None,
        voice_status_provider: Callable | None = None,
        action_handlers=None,
        sender=None,
    ):
        self.db = db
        self.session_factory = session_factory
        self.batches = batches
        self.authorizations = authorizations
        self.get_delivery_guard = get_delivery_guard
        self.get_deployment_profile = get_deployment_profile or (lambda: "local_trial")
        self.get_runtime_policy_valid = get_runtime_policy_valid or (lambda: True)
        self.voice_binding_provider = voice_binding_provider
        self.voice_validator = voice_validator
        self.voice_status_provider = voice_status_provider
        self.action_handlers = action_handlers or PRODUCTION_HANDLER_REGISTRY
        self.sender = sender

    async def pending_count(self) -> int:
        async with self.db.engine.connect() as connection:
            qq_count = int(
                await connection.scalar(
                    select(func.count())
                    .select_from(OutboxItem.__table__)
                    .where(OutboxItem.__table__.c.state.in_(PENDING_STATES))
                )
                or 0
            )
            action_count = int(
                await connection.scalar(
                    select(func.count())
                    .select_from(ToolActionOutboxItem.__table__)
                    .where(ToolActionOutboxItem.__table__.c.state.in_(("prepared", "sending", "unknown")))
                )
                or 0
            )
            return qq_count + action_count

    async def list(self, *, state: str | None = None, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        if state is not None and state not in OUTBOX_STATES:
            raise OutboxError("invalid_outbox_state", "Outbox状态无效。", 422)
        if type(limit) is not int or not 1 <= limit <= 200:
            raise OutboxError("invalid_pagination", "Outbox每页需为1到200条。", 422)
        if type(offset) is not int or offset < 0 or offset > 1_000_000:
            raise OutboxError("invalid_pagination", "Outbox偏移量无效。", 422)
        conditions = [OutboxItem.__table__.c.state == state] if state is not None else []
        async with self.db.engine.connect() as connection:
            total = int(
                await connection.scalar(
                    select(func.count()).select_from(OutboxItem.__table__).where(*conditions)
                )
                or 0
            )
            rows = (
                await connection.execute(
                    select(OutboxItem.__table__)
                    .where(*conditions)
                    .order_by(OutboxItem.__table__.c.created_at.desc())
                    .limit(limit)
                    .offset(offset)
                )
            ).mappings().all()
        return {
            "items": [self._serialize(row, include_payload=False) for row in rows],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    async def get(self, item_id: str) -> dict[str, Any]:
        async with self.db.engine.connect() as connection:
            row = await self._entry(connection, item_id)
        if row is None:
            raise OutboxError("outbox_not_found", "Outbox消息草稿不存在。", 404)
        return self._serialize(row, include_payload=True)

    async def issue_voice_authorization(self, payload: dict[str, Any], *, principal) -> dict[str, Any]:
        """Issue one exact QQ voice grant from a server-resolved approved artifact."""
        if self.get_deployment_profile() != "local_trial":
            raise OutboxError("external_actions_disabled", "target_staging 禁止签发语音外发授权。", 409)
        if getattr(principal, "role", None) != "owner":
            raise OutboxError("owner_required", "只有管理员可以为语音外发签发授权。", 403)
        if payload.get("action_id") != "qq.voice.send":
            raise OutboxError("unsupported_action", "当前只允许为QQ语音动作签发授权。", 422)
        expires_in_seconds = payload.get("expires_in_seconds", 600)
        if type(expires_in_seconds) is not int or not 1 <= expires_in_seconds <= 1800:
            raise OutboxError("invalid_expiry", "授权有效期需为1到1800秒。", 422)
        conversation_id = str(payload.get("conversation_id", ""))
        batch_id = str(payload.get("batch_id", ""))
        artifact_id = str(payload.get("artifact_id", ""))
        expected_revision = payload.get("expected_revision")
        if not conversation_id or not batch_id or not artifact_id:
            raise OutboxError("target_required", "语音授权必须绑定产物、会话和试用批次。", 422)
        if type(expected_revision) is not int or expected_revision < 0:
            raise OutboxError("invalid_revision", "会话版本号无效。", 422)
        guard = await self.get_delivery_guard(conversation_id)
        try:
            self.authorizations._assert_live_guard(
                guard,
                conversation_id=conversation_id,
                expected_revision=expected_revision,
                batch_id=batch_id,
                action_id="qq.voice.send",
            )
        except AuthorizationError as exc:
            raise OutboxError(exc.code, exc.message, exc.status_code) from exc
        await self._validate_qq_batch(
            batch_id,
            action_id="qq.voice.send",
            recipient_id=str(guard["recipient_id"]),
            allow_draft=True,
        )
        _binding, record_ref = await self._resolve_voice_binding(
            artifact_id,
            conversation_id=conversation_id,
            expected_revision=expected_revision,
            batch_id=batch_id,
            account_id=str(guard["account_id"]),
            recipient_id=str(guard["recipient_id"]),
            person_id=str(guard["person_id"]),
        )
        canonical_ref = json.dumps(record_ref, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        try:
            result = await self.authorizations.issue_message_grant(
                principal=principal,
                conversation_id=conversation_id,
                expected_revision=expected_revision,
                batch_id=batch_id,
                payload=canonical_ref,
                action_id="qq.voice.send",
                expires_in_seconds=expires_in_seconds,
            )
        except AuthorizationError as exc:
            raise OutboxError(exc.code, exc.message, exc.status_code) from exc
        return {**result, "authorization_id": result["id"], "record_ref": record_ref}

    async def prepare(self, payload: dict[str, Any], *, actor_id: str) -> dict[str, Any]:
        if self.get_deployment_profile() != "local_trial":
            raise OutboxError("outbox_disabled", "target_staging 禁止创建新的外发草稿。", 409)
        if actor_id != "admin:1":
            raise OutboxError("owner_required", "只有本机管理员可以准备QQ外发草稿。", 403)
        if payload.get("action_id", "qq.message.send") == "qq.voice.send":
            return await self._prepare_voice(payload, actor_id=actor_id)
        if payload.get("action_id", "qq.message.send") != "qq.message.send":
            raise OutboxError("unsupported_action", "当前只支持准备QQ文本消息。", 422)
        message = payload.get("payload")
        if not isinstance(message, str) or not message or len(message) > 4000:
            raise OutboxError("invalid_payload", "消息内容必须为1到4000个字符。", 422)
        expected_revision = payload.get("expected_revision")
        if type(expected_revision) is not int or expected_revision < 0:
            raise OutboxError("invalid_revision", "会话版本号无效。", 422)
        conversation_id = str(payload.get("conversation_id", ""))
        batch_id = str(payload.get("batch_id", ""))
        if not conversation_id or not batch_id:
            raise OutboxError("target_required", "必须指定会话和试用批次。", 422)
        idempotency_key = str(payload.get("idempotency_key", ""))
        if not idempotency_key or len(idempotency_key) > 256:
            raise OutboxError("invalid_idempotency_key", "必须提供不超过256字符的草稿幂等键。", 422)
        segment_index = payload.get("segment_index", 0)
        segment_count = payload.get("segment_count", 1)
        if type(segment_index) is not int or type(segment_count) is not int or not 1 <= segment_count <= 100:
            raise OutboxError("invalid_segment", "消息片段序号或总数无效。", 422)
        if not 0 <= segment_index < segment_count:
            raise OutboxError("invalid_segment", "消息片段序号超出总数范围。", 422)
        requested_group_id = payload.get("group_id")
        group_id_explicit = requested_group_id is not None
        if requested_group_id is None:
            group_id = str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"cutefish-outbox:{conversation_id}:{batch_id}:{idempotency_key}",
                )
            )
        elif isinstance(requested_group_id, str) and requested_group_id.strip() and len(requested_group_id.strip()) <= 128:
            group_id = requested_group_id.strip()
        else:
            raise OutboxError("invalid_group_id", "消息组标识不能为空且最多128个字符。", 422)
        dispatch_reason = payload.get("dispatch_reason", "owner_explicit")
        if dispatch_reason != "owner_explicit":
            raise OutboxError("unsupported_dispatch_reason", "当前只记录管理员明确提出的单次动作。", 422)

        guard = await self.get_delivery_guard(conversation_id)
        try:
            self.authorizations._assert_live_guard(
                guard,
                conversation_id=conversation_id,
                expected_revision=expected_revision,
                batch_id=batch_id,
            )
        except AuthorizationError as exc:
            raise OutboxError(exc.code, exc.message, exc.status_code) from exc
        recipient_id = str(guard["recipient_id"])
        account_id = str(guard["account_id"])
        person_id = str(guard["person_id"])
        delivery_mode = str(guard["delivery_mode"])
        async with self.session_factory() as session:
            batch = await session.get(TrialBatch, batch_id)
            if batch is None:
                raise OutboxError("batch_not_found", "试用批次不存在。", 404)
            if batch.state not in {"draft", "running"} or _utc(batch.expires_at) <= utc_now():
                raise OutboxError("batch_unavailable", "试用批次已结束或过期。", 409)
            self._assert_qq_batch_row(
                batch,
                action_id="qq.message.send",
                recipient_id=recipient_id,
                allow_draft=True,
            )
            batch_version = int(batch.version)

        payload_hash = payload_digest(message)
        authorization_id = payload.get("authorization_id")
        now = utc_now()
        item = {
            "id": str(uuid.uuid4()),
            "idempotency_key": idempotency_key,
            "request_id": payload.get("request_id"),
            "conversation_id": conversation_id,
            "expected_revision": expected_revision,
            "batch_id": batch_id,
            "recipient_id": recipient_id,
            "account_id": account_id,
            "actor_id": actor_id,
            "person_id": person_id,
            "action_id": "qq.message.send",
            "authorization_id": authorization_id,
            "delivery_mode": delivery_mode,
            "batch_version": batch_version,
            "dispatch_reason": dispatch_reason,
            "group_id": group_id,
            "segment_index": segment_index,
            "segment_count": segment_count,
            "payload_hash": payload_hash,
            "payload": message,
            "state": "prepared",
            "attempt_count": 0,
            "created_at": now,
        }
        if authorization_id:
            try:
                await self.authorizations.validate_for_item(item)
            except AuthorizationError as exc:
                raise OutboxError(exc.code, exc.message, exc.status_code) from exc

        async with self.db.immediate_transaction() as connection:
            existing = (
                await connection.execute(
                    select(OutboxItem.__table__).where(OutboxItem.idempotency_key == idempotency_key)
                )
            ).mappings().first()
            if existing is not None:
                comparable_fields = (
                    "request_id", "conversation_id", "expected_revision", "batch_id", "recipient_id",
                    "account_id", "actor_id", "person_id", "action_id", "authorization_id", "delivery_mode",
                    "batch_version", "dispatch_reason", "segment_index", "segment_count", "payload_hash", "payload",
                )
                if group_id_explicit:
                    comparable_fields += ("group_id",)
                same = all(
                    existing[name] == item[name]
                    for name in comparable_fields
                )
                if not same:
                    raise OutboxError("idempotency_conflict", "同一幂等键对应不同消息内容或目标。", 409)
                return self._serialize(existing, include_payload=True)
            group_rows = (
                await connection.execute(
                    select(OutboxItem.__table__).where(
                        OutboxItem.conversation_id == conversation_id,
                        OutboxItem.batch_id == batch_id,
                        OutboxItem.group_id == group_id,
                    )
                )
            ).mappings().all()
            if any(
                row["segment_count"] != segment_count
                or row["recipient_id"] != recipient_id
                or row["account_id"] != account_id
                or row["person_id"] != person_id
                for row in group_rows
            ):
                raise OutboxError("group_scope_conflict", "消息组内的目标或片段总数不一致。", 409)
            if any(row["segment_index"] == segment_index for row in group_rows):
                raise OutboxError("segment_slot_conflict", "消息组中的这个片段序号已被占用。", 409)
            await connection.execute(OutboxItem.__table__.insert().values(**item))
        return self._serialize(item, include_payload=True)

    async def _prepare_voice(self, payload: dict[str, Any], *, actor_id: str) -> dict[str, Any]:
        if self.get_deployment_profile() != "local_trial":
            raise OutboxError("outbox_disabled", "target_staging 禁止创建语音外发草稿。", 409)
        conversation_id = str(payload.get("conversation_id", ""))
        batch_id = str(payload.get("batch_id", ""))
        artifact_id = str(payload.get("artifact_id", ""))
        expected_revision = payload.get("expected_revision")
        idempotency_key = str(payload.get("idempotency_key", ""))
        authorization_id = str(payload.get("authorization_id", ""))
        if not conversation_id or not batch_id or not artifact_id:
            raise OutboxError("target_required", "语音草稿必须绑定产物、会话和试用批次。", 422)
        if type(expected_revision) is not int or expected_revision < 0:
            raise OutboxError("invalid_revision", "会话版本号无效。", 422)
        if not idempotency_key or len(idempotency_key) > 256:
            raise OutboxError("invalid_idempotency_key", "必须提供不超过256字符的草稿幂等键。", 422)
        if not authorization_id or len(authorization_id) > 36:
            raise OutboxError("authorization_required", "语音外发必须先取得一次性授权。", 403)
        if payload.get("dispatch_reason", "owner_explicit") != "owner_explicit":
            raise OutboxError("unsupported_dispatch_reason", "当前只记录管理员明确提出的单次动作。", 422)

        guard = await self.get_delivery_guard(conversation_id)
        try:
            self.authorizations._assert_live_guard(
                guard,
                conversation_id=conversation_id,
                expected_revision=expected_revision,
                batch_id=batch_id,
                action_id="qq.voice.send",
            )
        except AuthorizationError as exc:
            raise OutboxError(exc.code, exc.message, exc.status_code) from exc
        recipient_id = str(guard["recipient_id"])
        account_id = str(guard["account_id"])
        person_id = str(guard["person_id"])
        delivery_mode = str(guard["delivery_mode"])
        async with self.session_factory() as session:
            batch = await session.get(TrialBatch, batch_id)
            if batch is None:
                raise OutboxError("batch_not_found", "试用批次不存在。", 404)
            self._assert_qq_batch_row(
                batch,
                action_id="qq.voice.send",
                recipient_id=recipient_id,
                allow_draft=True,
            )
            batch_version = int(batch.version)

        _binding, record_ref = await self._resolve_voice_binding(
            artifact_id,
            conversation_id=conversation_id,
            expected_revision=expected_revision,
            batch_id=batch_id,
            account_id=account_id,
            recipient_id=recipient_id,
            person_id=person_id,
        )
        payload_hash = _voice_record_digest(record_ref)
        group_id = str(
            uuid.uuid5(uuid.NAMESPACE_URL, f"cutefish-voice-outbox:{conversation_id}:{batch_id}:{idempotency_key}")
        )
        now = utc_now()
        item = {
            "id": str(uuid.uuid4()),
            "idempotency_key": idempotency_key,
            "request_id": payload.get("request_id"),
            "conversation_id": conversation_id,
            "expected_revision": expected_revision,
            "batch_id": batch_id,
            "recipient_id": recipient_id,
            "account_id": account_id,
            "actor_id": actor_id,
            "person_id": person_id,
            "action_id": "qq.voice.send",
            "message_type": "record",
            "record_ref": record_ref,
            "authorization_id": authorization_id,
            "delivery_mode": delivery_mode,
            "batch_version": batch_version,
            "dispatch_reason": "owner_explicit",
            "group_id": group_id,
            "segment_index": 0,
            "segment_count": 1,
            "payload_hash": payload_hash,
            "payload": "",
            "state": "prepared",
            "attempt_count": 0,
            "created_at": now,
        }
        try:
            await self.authorizations.validate_for_item(item)
        except AuthorizationError as exc:
            raise OutboxError(exc.code, exc.message, exc.status_code) from exc

        async with self.db.immediate_transaction() as connection:
            existing = (
                await connection.execute(
                    select(OutboxItem.__table__).where(OutboxItem.idempotency_key == idempotency_key)
                )
            ).mappings().first()
            if existing is not None:
                comparable_fields = (
                    "request_id", "conversation_id", "expected_revision", "batch_id", "recipient_id",
                    "account_id", "actor_id", "person_id", "action_id", "message_type", "record_ref",
                    "authorization_id", "delivery_mode", "batch_version", "dispatch_reason", "group_id",
                    "segment_index", "segment_count", "payload_hash", "payload",
                )
                if not all(existing[name] == item[name] for name in comparable_fields):
                    raise OutboxError("idempotency_conflict", "同一幂等键对应不同语音产物、授权或目标。", 409)
                return self._serialize(existing, include_payload=True)
            await connection.execute(OutboxItem.__table__.insert().values(**item))
        return self._serialize(item, include_payload=True)

    async def _resolve_voice_binding(
        self,
        artifact_id: str,
        *,
        conversation_id: str,
        expected_revision: int,
        batch_id: str,
        account_id: str,
        recipient_id: str,
        person_id: str,
    ) -> tuple[VoiceOutboxBinding, dict[str, str]]:
        if self.get_deployment_profile() != "local_trial":
            raise OutboxError("outbox_dispatch_disabled", "target_staging 禁止解析语音外发绑定。", 409)
        provider = self.voice_binding_provider
        validator = self.voice_validator
        status_provider = self.voice_status_provider
        if not callable(provider) or not callable(validator) or not callable(status_provider):
            raise OutboxError("voice_binding_unavailable", "受控语音解析器、校验器或状态源未配置。", 409)
        try:
            status = status_provider()
            if hasattr(status, "__await__"):
                status = await status
            if (
                not isinstance(status, dict)
                or status.get("deployment_profile") != "local_trial"
                or status.get("readiness") != "ready"
                or status.get("production_enabled") is not False
                or status.get("runtime_enabled") is not True
                or status.get("voice_outbox_enabled") is not True
            ):
                raise OutboxError("voice_outbox_not_ready", "语音运行时、获批声线或本次外发开关未就绪。", 409)
            resolved = provider(
                artifact_id,
                conversation_id=conversation_id,
                expected_revision=expected_revision,
                batch_id=batch_id,
                account_id=account_id,
                recipient_id=recipient_id,
                person_id=person_id,
            )
            if hasattr(resolved, "__await__"):
                resolved = await resolved
            binding_data = resolved.model_dump() if hasattr(resolved, "model_dump") else resolved
            binding = VoiceOutboxBinding.model_validate(binding_data)
            record_ref = {
                "artifact_id": binding.artifact_id,
                "sha256": binding.sha256,
                "voice_version": f"{binding.profile_id}@{binding.profile_version}",
                "approval_ref": binding.approval_id,
            }
            validator_payload = {
                **binding.model_dump(),
                "conversation_id": conversation_id,
                "expected_revision": expected_revision,
                "batch_id": batch_id,
                "account_id": account_id,
                "recipient_id": recipient_id,
                "person_id": person_id,
            }
            validated = validator(validator_payload)
            if hasattr(validated, "__await__"):
                await validated
            return binding, record_ref
        except OutboxError:
            raise
        except Exception as exc:
            code = getattr(exc, "code", None)
            message = getattr(exc, "message", None)
            status_code = getattr(exc, "status_code", 409)
            if not isinstance(code, str):
                code = "voice_binding_invalid"
            if not isinstance(message, str):
                message = "语音产物、审批、声线版本或媒体内容已变化，拒绝外发。"
            if type(status_code) is not int or not 400 <= status_code <= 599:
                status_code = 409
            raise OutboxError(code, message, status_code) from exc

    async def _validate_qq_batch(
        self,
        batch_id: str,
        *,
        action_id: str,
        recipient_id: str,
        allow_draft: bool,
    ) -> None:
        async with self.session_factory() as session:
            batch = await session.get(TrialBatch, batch_id)
            if batch is None:
                raise OutboxError("batch_not_found", "试用批次不存在。", 404)
            self._assert_qq_batch_row(
                batch,
                action_id=action_id,
                recipient_id=recipient_id,
                allow_draft=allow_draft,
            )

    @staticmethod
    def _assert_qq_batch_row(
        batch,
        *,
        action_id: str,
        recipient_id: str,
        allow_draft: bool,
        expected_version: int | None = None,
    ) -> None:
        value = (lambda key: batch.get(key)) if isinstance(batch, Mapping) else (lambda key: getattr(batch, key))
        if value("module_id") != "qq" or list(value("object_ids") or []) != [recipient_id]:
            raise OutboxError("batch_scope_mismatch", "QQ外发批次必须绑定唯一且精确的收件目标。", 403)
        if value("capability_id") != action_id:
            raise OutboxError("batch_capability_mismatch", "批次能力必须与本次QQ动作完全一致。", 403)
        allowed_states = {"running", "draft"} if allow_draft else {"running"}
        if value("state") not in allowed_states or _utc(value("expires_at")) <= utc_now():
            raise OutboxError("batch_unavailable", "试用批次未运行、已结束或已过期。", 409)
        if expected_version is not None and int(value("version")) != expected_version:
            raise OutboxError("batch_stale", "试用批次已变化，需要重新确认授权。", 409)
        stop_condition = value("stop_condition") or {}
        max_actions = stop_condition.get("max_actions") if isinstance(stop_condition, dict) else None
        if (
            not isinstance(stop_condition, dict)
            or stop_condition.get("kind") != "after_n"
            or type(max_actions) is not int
            or not 1 <= max_actions <= 100
        ):
            raise OutboxError("finite_batch_required", "QQ外发批次必须有有效的有限动作上限。", 409)
        if int(value("action_count")) >= max_actions:
            raise OutboxError("batch_action_limit", "QQ外发批次已达到动作上限。", 409)

    async def cancel(self, item_id: str) -> dict[str, Any]:
        async with self.db.immediate_transaction() as connection:
            row = await self._entry(connection, item_id)
            if row is None:
                raise OutboxError("outbox_not_found", "Outbox消息草稿不存在。", 404)
            if row["state"] == "cancelled":
                return self._serialize(row, include_payload=True)
            if row["state"] != "prepared":
                raise OutboxError("cannot_cancel_after_dispatch", "已派发或结果不明的消息不能撤销。", 409)
            now = utc_now()
            await connection.execute(
                OutboxItem.__table__.update()
                .where(OutboxItem.id == item_id, OutboxItem.state == "prepared")
                .values(state="cancelled", finished_at=now)
            )
            return self._serialize({**row, "state": "cancelled", "finished_at": now}, include_payload=True)

    async def preflight(self, item_id: str) -> dict[str, Any]:
        """Recheck exact live state for future senders; never performs a send here."""
        if self.get_deployment_profile() != "local_trial":
            raise OutboxError("outbox_dispatch_disabled", "target_staging 禁止预检或派发外发动作。", 409)
        async with self.db.engine.connect() as connection:
            item = await self._entry(connection, item_id)
        if item is None:
            raise OutboxError("outbox_not_found", "Outbox消息草稿不存在。", 404)
        guard = await self._validate_prepared_item(item)
        self._assert_sender_available()
        return {"state": "validated", "conversation_revision": guard["revision"], "outbox_id": item_id}

    async def dispatch_one(self, item_id: str) -> dict[str, Any]:
        """Claim one exact prepared segment, invoke one sender call, and persist its receipt."""
        if self.get_deployment_profile() != "local_trial":
            raise OutboxError("outbox_dispatch_disabled", "target_staging 禁止派发外发动作。", 409)
        async with self.db.engine.connect() as connection:
            current = await self._entry(connection, item_id)
        if current is None:
            raise OutboxError("outbox_not_found", "Outbox消息草稿不存在。", 404)
        if current["state"] != "prepared":
            # Repeated dispatches are observational and never replay a segment.
            return self._serialize(current, include_payload=True)
        await self._validate_prepared_item(current)
        self._assert_sender_available()

        now = utc_now()
        attempt_id = str(uuid.uuid4())
        async with self.db.immediate_transaction() as connection:
            item = await self._entry(connection, item_id)
            if item is None:
                raise OutboxError("outbox_not_found", "Outbox消息草稿不存在。", 404)
            if item["state"] != "prepared":
                return self._serialize(item, include_payload=True)
            await self._validate_prepared_item(item, connection=connection)
            await self._consume_batch_action(connection, item)

            grant_update = await connection.execute(
                ActionAuthorization.__table__.update()
                .where(
                    ActionAuthorization.id == item["authorization_id"],
                    ActionAuthorization.revoked_at.is_(None),
                    ActionAuthorization.expires_at > now,
                    ActionAuthorization.used_uses < ActionAuthorization.max_uses,
                )
                .values(used_uses=ActionAuthorization.used_uses + 1)
            )
            if grant_update.rowcount != 1:
                raise OutboxError("authorization_consumed", "该消息授权已撤销、过期或使用。", 403)

            claim = await connection.execute(
                OutboxItem.__table__.update()
                .where(OutboxItem.id == item_id, OutboxItem.state == "prepared")
                .values(
                    state="sending",
                    attempt_count=OutboxItem.attempt_count + 1,
                    attempt_id=attempt_id,
                    sending_at=now,
                    finished_at=None,
                    result_code=None,
                    dispatch_evidence_ref=None,
                    remote_receipt_id=None,
                )
            )
            if claim.rowcount != 1:
                raise OutboxError("outbox_claim_conflict", "消息片段已被其他派发操作占用。", 409)
            item = {
                **item,
                "state": "sending",
                "attempt_count": int(item["attempt_count"]) + 1,
                "attempt_id": attempt_id,
                "sending_at": now,
            }

        try:
            receipt = await self._call_sender(item)
        except asyncio.CancelledError:
            await self._finish_attempt_durably(
                item_id,
                attempt_id,
                state="unknown",
                result_code="dispatch_cancelled_after_call",
            )
            raise
        except Exception:
            # Transport text can contain credentials or payloads; persist only a fixed code.
            return await self._finish_attempt_durably(
                item_id,
                attempt_id,
                state="unknown",
                result_code="sender_outcome_unknown",
            )

        if isinstance(receipt, SenderReceipt):
            evidence_ref = _bounded_receipt_value(receipt.evidence_ref, 512)
            result_code = _safe_result_code(receipt.result_code)
            if receipt.outcome == "sent":
                remote_id = _bounded_receipt_value(receipt.remote_receipt_id, 256)
                if evidence_ref and remote_id:
                    return await self._finish_attempt_durably(
                        item_id,
                        attempt_id,
                        state="sent",
                        result_code=result_code or "confirmed_sent",
                        remote_receipt_id=remote_id,
                        dispatch_evidence_ref=evidence_ref,
                    )
            elif receipt.outcome == "rejected" and receipt.no_side_effect_proven and evidence_ref:
                return await self._finish_attempt_durably(
                    item_id,
                    attempt_id,
                    state="failed",
                    result_code=result_code or "confirmed_not_accepted",
                    dispatch_evidence_ref=evidence_ref,
                )

        return await self._finish_attempt_durably(
            item_id,
            attempt_id,
            state="unknown",
            result_code="invalid_or_ambiguous_sender_receipt",
        )

    async def _finish_attempt_durably(self, *args, **kwargs) -> dict[str, Any]:
        """Persist a result even if the request task is cancelled while SQLite commits."""
        finish_task = asyncio.create_task(self._finish_attempt(*args, **kwargs))
        try:
            return await asyncio.shield(finish_task)
        except asyncio.CancelledError:
            try:
                await finish_task
            except Exception:
                # The durable state remains `sending`; startup recovery marks it
                # unknown, which still prevents a replay.
                pass
            raise

    async def _validate_prepared_item(self, item: Any, *, connection=None) -> dict[str, Any]:
        if item["state"] != "prepared":
            raise OutboxError("invalid_outbox_state", "只有prepared状态可以进入派发校验。", 409)
        message_type = item.get("message_type", "text")
        if item["action_id"] == "qq.voice.send":
            if message_type != "record" or item.get("payload") not in (None, ""):
                raise OutboxError("invalid_voice_outbox_item", "语音外发项的类型或文本负载无效。", 409)
            stored_record_ref = item.get("record_ref")
            if not isinstance(stored_record_ref, dict) or set(stored_record_ref) != {
                "artifact_id", "sha256", "voice_version", "approval_ref"
            }:
                raise OutboxError("invalid_voice_binding", "语音外发项缺少精确的登记素材绑定。", 409)
            expected_hash = _voice_record_digest(stored_record_ref)
        else:
            if item["action_id"] != "qq.message.send" or message_type != "text" or item.get("record_ref"):
                raise OutboxError("unsupported_action", "Outbox消息类型与动作标识不匹配。", 422)
            if payload_digest(item["payload"]) != item["payload_hash"]:
                raise OutboxError("payload_hash_mismatch", "消息内容与已批准摘要不一致。", 409)
            expected_hash = item["payload_hash"]
        if expected_hash != item["payload_hash"]:
            raise OutboxError("payload_hash_mismatch", "外发内容与已批准摘要不一致。", 409)
        try:
            guard = await self.authorizations.validate_for_item(item, connection=connection)
        except AuthorizationError as exc:
            raise OutboxError(exc.code, exc.message, exc.status_code) from exc

        if connection is None:
            async with self.db.engine.connect() as own_connection:
                batch = await self._batch_row(own_connection, item["batch_id"])
                sent_segments = await self._sent_segment_indexes(own_connection, item)
        else:
            batch = await self._batch_row(connection, item["batch_id"])
            sent_segments = await self._sent_segment_indexes(connection, item)
        if batch is None:
            raise OutboxError("batch_not_found", "试用批次不存在。", 404)
        self._assert_qq_batch_row(
            batch,
            action_id=item["action_id"],
            recipient_id=item["recipient_id"],
            allow_draft=False,
            expected_version=int(item["batch_version"]),
        )
        expected_prior = set(range(int(item["segment_index"])))
        if sent_segments != expected_prior:
            raise OutboxError("chunk_sequence_blocked", "前置消息片段未全部确认送达，停止后续片段。", 409)
        if item["action_id"] == "qq.voice.send":
            _binding, current_record_ref = await self._resolve_voice_binding(
                str(item["record_ref"]["artifact_id"]),
                conversation_id=str(item["conversation_id"]),
                expected_revision=int(item["expected_revision"]),
                batch_id=str(item["batch_id"]),
                account_id=str(item["account_id"]),
                recipient_id=str(item["recipient_id"]),
                person_id=str(item["person_id"]),
            )
            if current_record_ref != item["record_ref"]:
                raise OutboxError("voice_binding_changed", "语音审批、声线版本或媒体哈希已变化。", 409)
        return guard

    def _assert_sender_available(self) -> None:
        if self.sender is None:
            raise OutboxError("sender_disabled", "本轮QQ发送器未配置；消息仍保留为草稿。", 409)
        if not callable(self.sender) and not callable(getattr(self.sender, "send", None)):
            raise OutboxError("sender_unavailable", "QQ发送器没有可调用的send入口。", 409)

    async def _consume_batch_action(self, connection, item: Any) -> None:
        batch = await self._batch_row(connection, item["batch_id"])
        if batch is None:
            raise OutboxError("batch_not_found", "试用批次不存在。", 404)
        stop_condition = batch["stop_condition"] or {}
        if not isinstance(stop_condition, dict):
            raise OutboxError("batch_stop_condition_invalid", "试用批次停止条件无效。", 409)
        if stop_condition.get("kind") == "after_n":
            max_actions = stop_condition.get("max_actions")
            if type(max_actions) is not int or not 1 <= max_actions <= 100:
                raise OutboxError("batch_stop_condition_invalid", "试用批次动作上限无效。", 409)
        elif stop_condition.get("kind") == "manual":
            max_actions = None
        else:
            raise OutboxError("batch_stop_condition_invalid", "试用批次停止条件类型无效。", 409)
        if max_actions is not None and int(batch["action_count"]) >= int(max_actions):
            raise OutboxError("batch_action_limit", "试用批次已达到动作上限，不能继续派发。", 409)
        conditions = [
            TrialBatch.id == item["batch_id"],
            TrialBatch.version == item["batch_version"],
            TrialBatch.state == "running",
        ]
        if max_actions is not None:
            conditions.append(TrialBatch.action_count < int(max_actions))
        result = await connection.execute(
            TrialBatch.__table__.update()
            .where(*conditions)
            .values(action_count=TrialBatch.action_count + 1)
        )
        if result.rowcount != 1:
            raise OutboxError("batch_action_limit", "试用批次已变化或达到动作上限，不能继续派发。", 409)

    async def _batch_row(self, connection, batch_id: str):
        return (
            await connection.execute(
                select(TrialBatch.__table__).where(TrialBatch.id == batch_id)
            )
        ).mappings().first()

    async def _sent_segment_indexes(self, connection, item: Any) -> set[int]:
        rows = (
            await connection.execute(
                select(OutboxItem.segment_index).where(
                    OutboxItem.conversation_id == item["conversation_id"],
                    OutboxItem.batch_id == item["batch_id"],
                    OutboxItem.group_id == item["group_id"],
                    OutboxItem.segment_count == item["segment_count"],
                    OutboxItem.state == "sent",
                )
            )
        ).all()
        return {int(row[0]) for row in rows if int(row[0]) < int(item["segment_index"])}

    async def _call_sender(self, item: Any):
        sender = self.sender
        method = getattr(sender, "send", None)
        if callable(method):
            result = method(self._sender_payload(item))
        elif callable(sender):
            result = sender(self._sender_payload(item))
        else:
            raise TypeError("sender must be async callable or expose send(payload)")
        return await result if inspect.isawaitable(result) else result

    @staticmethod
    def _sender_payload(item: Any) -> dict[str, Any]:
        payload = {
            "attempt_id": item["attempt_id"],
            "outbox_id": item["id"],
            "account_id": item["account_id"],
            "recipient_id": item["recipient_id"],
            "person_id": item["person_id"],
            "conversation_id": item["conversation_id"],
            "expected_revision": int(item["expected_revision"]),
            "batch_id": item["batch_id"],
            "message_type": item.get("message_type", "text"),
            "payload": item["payload"],
            "segment_index": int(item["segment_index"]),
            "segment_count": int(item["segment_count"]),
        }
        if payload["message_type"] == "record":
            payload["record_ref"] = item.get("record_ref")
        return payload

    async def _finish_attempt(
        self,
        item_id: str,
        attempt_id: str,
        *,
        state: str,
        result_code: str,
        remote_receipt_id: str | None = None,
        dispatch_evidence_ref: str | None = None,
    ) -> dict[str, Any]:
        now = utc_now()
        async with self.db.immediate_transaction() as connection:
            result = await connection.execute(
                OutboxItem.__table__.update()
                .where(
                    OutboxItem.id == item_id,
                    OutboxItem.state == "sending",
                    OutboxItem.attempt_id == attempt_id,
                )
                .values(
                    state=state,
                    result_code=_safe_result_code(result_code) or "sender_outcome_unknown",
                    remote_receipt_id=remote_receipt_id,
                    dispatch_evidence_ref=dispatch_evidence_ref,
                    finished_at=now,
                )
            )
            row = await self._entry(connection, item_id)
            if result.rowcount != 1 and (row is None or row["attempt_id"] != attempt_id):
                raise OutboxError("outbox_result_conflict", "派发结果与当前尝试不匹配，状态需要人工核对。", 409)
            if result.rowcount == 1:
                row = await self._entry(connection, item_id)
        if row is None:
            raise OutboxError("outbox_not_found", "Outbox消息草稿不存在。", 404)
        return self._serialize(row, include_payload=True)

    async def recover_inflight(self) -> int:
        now = utc_now()
        async with self.db.immediate_transaction() as connection:
            result = await connection.execute(
                OutboxItem.__table__.update()
                .where(OutboxItem.state == "sending")
                .values(state="unknown", result_code="restart_outcome_unknown", finished_at=now)
            )
            action_result = await connection.execute(
                ToolActionOutboxItem.__table__.update()
                .where(ToolActionOutboxItem.state == "sending")
                .values(state="unknown", result_code="restart_outcome_unknown", finished_at=now)
            )
            return int(result.rowcount or 0) + int(action_result.rowcount or 0)

    async def record_action_preview(
        self,
        preview: ToolActionPreview,
        *,
        actor_id: str,
    ) -> dict[str, Any]:
        """Persist one server-derived preview; this path never calls its handler."""
        if actor_id != "admin:1" or preview.actor_id != actor_id:
            raise OutboxError("owner_required", "只有已认证管理员可以记录动作预览。", 403)
        if self.get_deployment_profile() != "local_trial":
            raise OutboxError("external_actions_disabled", "target_staging 不接受新的外部动作预览。", 409)
        registration = self._tool_handler(preview.site, preview.capability_id, preview.handler_version)
        _validate_tool_action_preview(preview)
        try:
            preview_id = str(uuid.UUID(preview.preview_id))
        except (ValueError, AttributeError) as exc:
            raise OutboxError("invalid_preview_id", "动作预览标识必须是 UUID。", 422) from exc

        now = utc_now()
        values = {
            "id": preview_id,
            "site": preview.site,
            "capability_id": preview.capability_id,
            "handler_version": registration.version,
            "account_id": preview.account_id,
            "resource_id": preview.resource_id,
            "action": preview.action,
            "payload_json": preview.payload_json,
            "payload_hash": preview.payload_hash,
            "expected_revision": preview.expected_revision,
            "actor_id": actor_id,
            "scope": preview.scope,
            "source_refs": preview.source_refs,
            "state": "preview_only",
            "created_at": now,
            "expires_at": now + timedelta(seconds=preview.expires_in_seconds),
        }
        async with self.db.immediate_transaction() as connection:
            existing = (
                await connection.execute(
                    select(ToolActionPreviewRecord.__table__).where(ToolActionPreviewRecord.id == preview_id)
                )
            ).mappings().first()
            if existing is not None:
                compare = (
                    "site", "capability_id", "handler_version", "account_id", "resource_id", "action",
                    "payload_json", "payload_hash", "expected_revision", "actor_id", "scope", "source_refs",
                )
                if any(existing[key] != values[key] for key in compare):
                    raise OutboxError("preview_id_conflict", "动作预览 ID 已绑定到其他内容。", 409)
                return _serialize_tool_action_preview(existing)
            await connection.execute(ToolActionPreviewRecord.__table__.insert().values(**values))
        return _serialize_tool_action_preview(values)

    async def issue_action_grant(
        self,
        preview_id: str,
        *,
        actor_id: str,
        batch_id: str,
        expires_in_seconds: int = 600,
    ) -> dict[str, Any]:
        """Issue a one-use grant only in a future, explicitly live profile."""
        self._require_live_tool_actions()
        if actor_id != "admin:1":
            raise OutboxError("owner_required", "只有已认证管理员可以签发动作授权。", 403)
        if type(expires_in_seconds) is not int or not 1 <= expires_in_seconds <= 1800:
            raise OutboxError("invalid_expiry", "动作授权有效期需为1到1800秒。", 422)
        now = utc_now()
        async with self.db.immediate_transaction() as connection:
            preview = await self._tool_preview(connection, preview_id)
            batch = await self._batch_row(connection, batch_id)
            if preview is None:
                raise OutboxError("preview_not_found", "动作预览不存在。", 404)
            if preview["actor_id"] != actor_id or preview["state"] != "preview_only":
                raise OutboxError("preview_unavailable", "动作预览已失效或不属于当前管理员。", 409)
            if _utc(preview["expires_at"]) <= now:
                raise OutboxError("preview_expired", "动作预览已过期，请重新生成。", 409)
            if batch is None:
                raise OutboxError("batch_not_found", "试用批次不存在。", 404)
            self._validate_tool_batch(batch, preview, require_running=True)
            active = (
                await connection.execute(
                    select(ToolActionGrant.__table__).where(
                        ToolActionGrant.preview_id == preview_id,
                        ToolActionGrant.revoked_at.is_(None),
                        ToolActionGrant.expires_at > now,
                    )
                )
            ).mappings().first()
            if active is not None:
                raise OutboxError("grant_already_active", "该预览已有有效的一次性授权。", 409)
            grant_expiry = min(
                now + timedelta(seconds=expires_in_seconds),
                _utc(preview["expires_at"]),
                _utc(batch["expires_at"]),
            )
            row = {
                "id": str(uuid.uuid4()),
                "preview_id": preview_id,
                "actor_id": actor_id,
                "batch_id": batch_id,
                "batch_version": int(batch["version"]),
                "payload_hash": str(preview["payload_hash"]),
                "scope_hash": _tool_scope_digest(preview["scope"]),
                "created_at": now,
                "expires_at": grant_expiry,
                "revoked_at": None,
                "used_at": None,
            }
            await connection.execute(ToolActionGrant.__table__.insert().values(**row))
        return _serialize_tool_action_grant(row)

    async def prepare_action(
        self,
        preview_id: str,
        grant_id: str,
        batch_id: str,
        idempotency_key: str,
        *,
        actor_id: str,
    ) -> dict[str, Any]:
        """Create exactly one durable action row from stored preview+grant data."""
        self._require_live_tool_actions()
        if actor_id != "admin:1":
            raise OutboxError("owner_required", "只有已认证管理员可以准备工具动作。", 403)
        if not idempotency_key or len(idempotency_key) > 256:
            raise OutboxError("invalid_idempotency_key", "必须提供不超过256字符的动作幂等键。", 422)
        now = utc_now()
        async with self.db.immediate_transaction() as connection:
            existing = (
                await connection.execute(
                    select(ToolActionOutboxItem.__table__).where(
                        ToolActionOutboxItem.idempotency_key == idempotency_key
                    )
                )
            ).mappings().first()
            if existing is not None:
                if (
                    existing["preview_id"] != preview_id
                    or existing["grant_id"] != grant_id
                    or existing["batch_id"] != batch_id
                    or existing["actor_id"] != actor_id
                ):
                    raise OutboxError("idempotency_conflict", "同一幂等键已绑定到其他工具动作。", 409)
                return _serialize_tool_action_item(existing, include_payload=True)

            preview = await self._tool_preview(connection, preview_id)
            grant = await self._tool_grant(connection, grant_id)
            batch = await self._batch_row(connection, batch_id)
            if preview is None or grant is None:
                raise OutboxError("preview_or_grant_not_found", "动作预览或一次性授权不存在。", 404)
            if batch is None:
                raise OutboxError("batch_not_found", "试用批次不存在。", 404)
            if preview["state"] != "preview_only" or preview["actor_id"] != actor_id:
                raise OutboxError("preview_unavailable", "动作预览已失效或不属于当前管理员。", 409)
            self._validate_tool_grant(grant, preview, batch, actor_id=actor_id, now=now)
            registration = self._tool_handler(
                str(preview["site"]), str(preview["capability_id"]), str(preview["handler_version"])
            )
            self._validate_tool_batch(batch, preview, require_running=True)
            envelope = ToolActionEnvelope(
                action_id=str(preview["capability_id"]),
                capability_id=str(preview["capability_id"]),
                handler_version=registration.version,
                site=str(preview["site"]),
                account_id=str(preview["account_id"]),
                resource_id=str(preview["resource_id"]),
                action=str(preview["action"]),
                payload_json=str(preview["payload_json"]),
                payload_hash=str(preview["payload_hash"]),
                preview_id=preview_id,
                grant_id=grant_id,
                batch_id=batch_id,
                expected_revision=int(preview["expected_revision"]),
                actor_id=actor_id,
                idempotency_key=idempotency_key,
                scope=preview["scope"],
                source_refs=preview["source_refs"],
            )
            _validate_tool_action_envelope(envelope)
            row = {
                "id": str(uuid.uuid4()),
                "idempotency_key": idempotency_key,
                "preview_id": preview_id,
                "grant_id": grant_id,
                "batch_id": batch_id,
                "batch_version": int(grant["batch_version"]),
                "site": envelope.site,
                "capability_id": envelope.capability_id,
                "handler_version": envelope.handler_version,
                "account_id": envelope.account_id,
                "resource_id": envelope.resource_id,
                "action": envelope.action,
                "payload_json": envelope.payload_json,
                "payload_hash": envelope.payload_hash,
                "expected_revision": envelope.expected_revision,
                "actor_id": envelope.actor_id,
                "scope": envelope.scope,
                "source_refs": envelope.source_refs,
                "state": "prepared",
                "attempt_count": 0,
                "attempt_id": None,
                "evidence_ref": None,
                "remote_receipt_id": None,
                "result_code": None,
                "created_at": now,
                "sending_at": None,
                "finished_at": None,
            }
            grant_update = await connection.execute(
                ToolActionGrant.__table__.update()
                .where(
                    ToolActionGrant.id == grant_id,
                    ToolActionGrant.used_at.is_(None),
                    ToolActionGrant.revoked_at.is_(None),
                    ToolActionGrant.expires_at > now,
                )
                .values(used_at=now)
            )
            if grant_update.rowcount != 1:
                raise OutboxError("grant_consumed", "该一次性动作授权已使用、撤销或过期。", 409)
            await connection.execute(ToolActionOutboxItem.__table__.insert().values(**row))
        return _serialize_tool_action_item(row, include_payload=True)

    async def dispatch_action(self, item_id: str) -> dict[str, Any]:
        """Attempt one verified handler call; ambiguous outcomes remain unknown."""
        self._require_live_tool_actions()
        async with self.db.engine.connect() as connection:
            current = await self._tool_action_entry(connection, item_id)
        if current is None:
            raise OutboxError("tool_action_not_found", "工具动作 Outbox 项不存在。", 404)
        if current["state"] != "prepared":
            return _serialize_tool_action_item(current, include_payload=True)

        now = utc_now()
        attempt_id = str(uuid.uuid4())
        async with self.db.immediate_transaction() as connection:
            row = await self._tool_action_entry(connection, item_id)
            if row is None:
                raise OutboxError("tool_action_not_found", "工具动作 Outbox 项不存在。", 404)
            if row["state"] != "prepared":
                return _serialize_tool_action_item(row, include_payload=True)
            preview = await self._tool_preview(connection, str(row["preview_id"]))
            grant = await self._tool_grant(connection, str(row["grant_id"]))
            batch = await self._batch_row(connection, str(row["batch_id"]))
            if preview is None or grant is None or batch is None:
                raise OutboxError("tool_action_binding_missing", "动作绑定记录不完整，拒绝派发。", 409)
            if preview["state"] != "preview_only":
                raise OutboxError("preview_unavailable", "动作预览已失效，拒绝派发。", 409)
            self._validate_tool_grant(grant, preview, batch, actor_id=str(row["actor_id"]), now=now, prepared=True)
            self._validate_tool_batch(batch, preview, require_running=True, expected_version=int(row["batch_version"]))
            registration = self._tool_handler(str(row["site"]), str(row["capability_id"]), str(row["handler_version"]))
            _validate_action_outbox_binding(row, preview, grant)
            await self._consume_tool_batch_action(connection, batch)
            claim = await connection.execute(
                ToolActionOutboxItem.__table__.update()
                .where(ToolActionOutboxItem.id == item_id, ToolActionOutboxItem.state == "prepared")
                .values(
                    state="sending",
                    attempt_count=ToolActionOutboxItem.attempt_count + 1,
                    attempt_id=attempt_id,
                    sending_at=now,
                    finished_at=None,
                    evidence_ref=None,
                    remote_receipt_id=None,
                    result_code=None,
                )
            )
            if claim.rowcount != 1:
                raise OutboxError("tool_action_claim_conflict", "工具动作已被其他派发操作占用。", 409)
            row = {**row, "state": "sending", "attempt_count": int(row["attempt_count"]) + 1,
                   "attempt_id": attempt_id, "sending_at": now}

        try:
            receipt = await self._call_tool_action(registration, row)
        except asyncio.CancelledError:
            await self._finish_tool_action_durably(
                item_id, attempt_id, state="unknown", result_code="dispatch_cancelled_after_call"
            )
            raise
        except Exception:
            return await self._finish_tool_action_durably(
                item_id, attempt_id, state="unknown", result_code="handler_outcome_unknown"
            )

        if isinstance(receipt, ToolActionReceipt):
            evidence_ref = _bounded_receipt_value(receipt.evidence_ref, 512)
            remote_id = _bounded_receipt_value(receipt.remote_receipt_id, 256)
            result_code = _safe_result_code(receipt.result_code)
            if receipt.outcome == "confirmed" and evidence_ref:
                return await self._finish_tool_action_durably(
                    item_id, attempt_id, state="confirmed", result_code=result_code or "confirmed",
                    evidence_ref=evidence_ref, remote_receipt_id=remote_id,
                )
            if receipt.outcome == "rejected" and receipt.no_side_effect_proven and evidence_ref:
                return await self._finish_tool_action_durably(
                    item_id, attempt_id, state="rejected", result_code=result_code or "rejected_no_side_effect",
                    evidence_ref=evidence_ref,
                )
        return await self._finish_tool_action_durably(
            item_id, attempt_id, state="unknown", result_code="invalid_or_ambiguous_handler_receipt"
        )

    async def cancel_action(self, item_id: str) -> dict[str, Any]:
        async with self.db.immediate_transaction() as connection:
            row = await self._tool_action_entry(connection, item_id)
            if row is None:
                raise OutboxError("tool_action_not_found", "工具动作 Outbox 项不存在。", 404)
            if row["state"] == "cancelled":
                return _serialize_tool_action_item(row, include_payload=True)
            if row["state"] != "prepared":
                raise OutboxError("cannot_cancel_after_dispatch", "已派发或结果不明的工具动作不能撤销。", 409)
            now = utc_now()
            await connection.execute(
                ToolActionOutboxItem.__table__.update()
                .where(ToolActionOutboxItem.id == item_id, ToolActionOutboxItem.state == "prepared")
                .values(state="cancelled", result_code="owner_cancelled", finished_at=now)
            )
            return _serialize_tool_action_item({**row, "state": "cancelled", "result_code": "owner_cancelled", "finished_at": now}, include_payload=True)

    async def list_actions(self, *, state: str | None = None, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        allowed = {"prepared", "sending", "confirmed", "rejected", "unknown", "cancelled"}
        if state is not None and state not in allowed:
            raise OutboxError("invalid_outbox_state", "工具动作 Outbox 状态无效。", 422)
        if type(limit) is not int or not 1 <= limit <= 200 or type(offset) is not int or not 0 <= offset <= 1_000_000:
            raise OutboxError("invalid_pagination", "工具动作 Outbox 分页参数无效。", 422)
        conditions = [ToolActionOutboxItem.state == state] if state else []
        async with self.db.engine.connect() as connection:
            total = int(await connection.scalar(
                select(func.count()).select_from(ToolActionOutboxItem.__table__).where(*conditions)
            ) or 0)
            rows = (await connection.execute(
                select(ToolActionOutboxItem.__table__).where(*conditions)
                .order_by(ToolActionOutboxItem.created_at.desc()).limit(limit).offset(offset)
            )).mappings().all()
        return {"items": [_serialize_tool_action_item(row, include_payload=False) for row in rows],
                "total": total, "limit": limit, "offset": offset}

    async def get_action(self, item_id: str) -> dict[str, Any]:
        async with self.db.engine.connect() as connection:
            row = await self._tool_action_entry(connection, item_id)
        if row is None:
            raise OutboxError("tool_action_not_found", "工具动作 Outbox 项不存在。", 404)
        return _serialize_tool_action_item(row, include_payload=True)

    async def _finish_tool_action_durably(self, item_id: str, attempt_id: str, **values) -> dict[str, Any]:
        task = asyncio.create_task(self._finish_tool_action(item_id, attempt_id, **values))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            try:
                await task
            except Exception:
                pass
            raise

    async def _finish_tool_action(self, item_id: str, attempt_id: str, *, state: str, result_code: str,
                                  evidence_ref: str | None = None, remote_receipt_id: str | None = None) -> dict[str, Any]:
        now = utc_now()
        async with self.db.immediate_transaction() as connection:
            result = await connection.execute(
                ToolActionOutboxItem.__table__.update()
                .where(ToolActionOutboxItem.id == item_id, ToolActionOutboxItem.state == "sending",
                       ToolActionOutboxItem.attempt_id == attempt_id)
                .values(state=state, result_code=_safe_result_code(result_code) or "handler_outcome_unknown",
                        evidence_ref=evidence_ref, remote_receipt_id=remote_receipt_id, finished_at=now)
            )
            row = await self._tool_action_entry(connection, item_id)
            if result.rowcount != 1 and (row is None or row["attempt_id"] != attempt_id):
                raise OutboxError("tool_action_result_conflict", "动作回执与当前尝试不匹配。", 409)
            if result.rowcount == 1:
                row = await self._tool_action_entry(connection, item_id)
        if row is None:
            raise OutboxError("tool_action_not_found", "工具动作 Outbox 项不存在。", 404)
        return _serialize_tool_action_item(row, include_payload=True)

    async def _call_tool_action(self, registration, row):
        handler = registration.handler
        payload = {
            "attempt_id": row["attempt_id"], "outbox_id": row["id"],
            "idempotency_key": row["idempotency_key"], "site": row["site"],
            "capability_id": row["capability_id"], "handler_version": row["handler_version"],
            "account_id": row["account_id"], "resource_id": row["resource_id"],
            "action": row["action"], "payload": json.loads(row["payload_json"]),
            "payload_hash": row["payload_hash"], "expected_revision": row["expected_revision"],
            "actor_id": row["actor_id"], "scope": row["scope"], "source_refs": row["source_refs"],
        }
        result = handler(payload)
        return await result if inspect.isawaitable(result) else result

    def _require_live_tool_actions(self) -> None:
        if self.get_deployment_profile() not in {"local_trial", "target_live"}:
            raise OutboxError("tool_actions_disabled", "target_staging 禁止工具动作授权或派发。", 409)

    def _tool_handler(self, site: str, capability_id: str, handler_version: str):
        try:
            registration = self.action_handlers.resolve(capability_id)
        except HandlerUnavailable as exc:
            raise OutboxError(exc.code, exc.message, 409) from exc
        if (
            registration.site_id != site
            or registration.capability_id != capability_id
            or registration.version != handler_version
            or registration.side_effect != "external_write"
        ):
            raise OutboxError("handler_binding_mismatch", "动作预览与已核实 handler 绑定不匹配。", 409)
        return registration

    @staticmethod
    def _validate_tool_batch(batch, preview, *, require_running: bool, expected_version: int | None = None) -> None:
        if batch["module_id"] != "integrations" or batch["capability_id"] != preview["capability_id"]:
            raise OutboxError("batch_scope_mismatch", "试用批次集成模块或能力与动作预览不匹配。", 403)
        if list(batch["object_ids"] or []) != [preview["resource_id"]]:
            raise OutboxError("batch_target_mismatch", "试用批次必须只包含此精确目标。", 403)
        stop_condition = batch["stop_condition"] or {}
        max_actions = stop_condition.get("max_actions") if isinstance(stop_condition, dict) else None
        if (
            not isinstance(stop_condition, dict)
            or stop_condition.get("kind") != "after_n"
            or type(max_actions) is not int
            or not 1 <= max_actions <= 100
        ):
            raise OutboxError("batch_stop_condition_invalid", "工具动作批次必须有有效的有限动作上限。", 409)
        if int(batch["action_count"]) >= max_actions:
            raise OutboxError("batch_action_limit", "工具动作批次已达到动作上限。", 409)
        if require_running and (batch["state"] != "running" or _utc(batch["expires_at"]) <= utc_now()):
            raise OutboxError("batch_unavailable", "工具动作批次必须仍在运行且未过期。", 409)
        if expected_version is not None and int(batch["version"]) != expected_version:
            raise OutboxError("batch_stale", "工具动作批次已变化，需重新确认授权。", 409)

    @staticmethod
    def _validate_tool_grant(grant, preview, batch, *, actor_id: str, now: datetime, prepared: bool = False) -> None:
        if (
            grant["preview_id"] != preview["id"]
            or grant["batch_id"] != batch["id"]
            or grant["actor_id"] != actor_id
            or grant["payload_hash"] != preview["payload_hash"]
            or grant["scope_hash"] != _tool_scope_digest(preview["scope"])
            or grant["batch_version"] != batch["version"]
            or grant["revoked_at"] is not None
            or _utc(grant["expires_at"]) <= now
            or (prepared and grant["used_at"] is None)
        ):
            raise OutboxError("grant_scope_mismatch", "动作授权已失效或与预览/批次范围不匹配。", 403)

    @staticmethod
    async def _tool_preview(connection, preview_id: str):
        return (await connection.execute(
            select(ToolActionPreviewRecord.__table__).where(ToolActionPreviewRecord.id == preview_id)
        )).mappings().first()

    @staticmethod
    async def _tool_grant(connection, grant_id: str):
        return (await connection.execute(
            select(ToolActionGrant.__table__).where(ToolActionGrant.id == grant_id)
        )).mappings().first()

    @staticmethod
    async def _tool_action_entry(connection, item_id: str):
        return (await connection.execute(
            select(ToolActionOutboxItem.__table__).where(ToolActionOutboxItem.id == item_id)
        )).mappings().first()

    async def _consume_tool_batch_action(self, connection, batch) -> None:
        stop_condition = batch["stop_condition"] or {}
        if not isinstance(stop_condition, dict):
            raise OutboxError("batch_stop_condition_invalid", "试用批次停止条件无效。", 409)
        if stop_condition.get("kind") == "after_n":
            max_actions = stop_condition.get("max_actions")
            if type(max_actions) is not int or not 1 <= max_actions <= 100:
                raise OutboxError("batch_stop_condition_invalid", "试用批次动作上限无效。", 409)
            if int(batch["action_count"]) >= max_actions:
                raise OutboxError("batch_action_limit", "试用批次已达到动作上限。", 409)
            conditions = [TrialBatch.id == batch["id"], TrialBatch.version == batch["version"],
                          TrialBatch.state == "running", TrialBatch.action_count < max_actions]
        elif stop_condition.get("kind") == "manual":
            conditions = [TrialBatch.id == batch["id"], TrialBatch.version == batch["version"],
                          TrialBatch.state == "running"]
        else:
            raise OutboxError("batch_stop_condition_invalid", "试用批次停止条件类型无效。", 409)
        result = await connection.execute(
            TrialBatch.__table__.update().where(*conditions).values(action_count=TrialBatch.action_count + 1)
        )
        if result.rowcount != 1:
            raise OutboxError("batch_action_limit", "试用批次已变化或达到动作上限。", 409)

    async def _entry(self, connection, item_id: str):
        return (
            await connection.execute(select(OutboxItem.__table__).where(OutboxItem.id == item_id))
        ).mappings().first()

    @staticmethod
    def _serialize(row: Any, *, include_payload: bool) -> dict[str, Any]:
        result = {
            "id": row["id"],
            "idempotency_key": row["idempotency_key"],
            "request_id": row.get("request_id"),
            "conversation_id": row["conversation_id"],
            "expected_revision": row["expected_revision"],
            "batch_id": row["batch_id"],
            "recipient_id": row["recipient_id"],
            "account_id": row["account_id"],
            "actor_id": row["actor_id"],
            "person_id": row["person_id"],
            "action_id": row["action_id"],
            "message_type": row.get("message_type", "text"),
            "record_ref": row.get("record_ref"),
            "authorization_id": row.get("authorization_id"),
            "delivery_mode": row.get("delivery_mode"),
            "batch_version": row.get("batch_version"),
            "dispatch_reason": row["dispatch_reason"],
            "group_id": row["group_id"],
            "segment_index": row["segment_index"],
            "segment_count": row["segment_count"],
            "payload_hash": row["payload_hash"],
            "state": row["state"],
            "attempt_count": row["attempt_count"],
            "attempt_id": row.get("attempt_id"),
            "remote_receipt_id": row.get("remote_receipt_id"),
            "dispatch_evidence_ref": row.get("dispatch_evidence_ref"),
            "result_code": row.get("result_code"),
            "created_at": _utc(row["created_at"]).isoformat(),
            "sending_at": _utc(row["sending_at"]).isoformat() if row.get("sending_at") else None,
            "finished_at": _utc(row["finished_at"]).isoformat() if row.get("finished_at") else None,
        }
        if include_payload:
            result["payload"] = row["payload"]
        return result


def _bounded_receipt_value(value: Any, max_length: int) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > max_length or any(ord(char) < 32 for char in text):
        return None
    return text


def _safe_result_code(value: Any) -> str | None:
    return value if isinstance(value, str) and _SAFE_RESULT_CODE.fullmatch(value) else None


def _tool_scope_digest(scope: Any) -> str:
    try:
        encoded = json.dumps(scope, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise OutboxError("invalid_action_scope", "工具动作 scope 无法规范化。", 422) from exc
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _voice_record_digest(record_ref: dict[str, str]) -> str:
    try:
        encoded = json.dumps(record_ref, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise OutboxError("invalid_voice_binding", "语音授权绑定无法规范化。", 422) from exc
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _has_secret_key(value: Any) -> bool:
    if isinstance(value, dict):
        for raw_key, item in value.items():
            normalized = str(raw_key).casefold().replace("-", "_")
            if any(part in {"authorization", "cookie", "credential", "csrf", "key", "password", "secret", "session", "token"}
                   for part in normalized.split("_")):
                return True
            if _has_secret_key(item):
                return True
    elif isinstance(value, list):
        return any(_has_secret_key(item) for item in value)
    return False


def _validate_tool_action_preview(preview: ToolActionPreview) -> None:
    if not preview.account_id or any(ord(char) < 32 for char in preview.account_id):
        raise OutboxError("invalid_action_account", "工具动作需要安全的本机账号别名。", 422)
    try:
        payload = json.loads(preview.payload_json)
    except (json.JSONDecodeError, TypeError) as exc:
        raise OutboxError("invalid_action_payload", "工具动作 payload 必须是有效 JSON。", 422) from exc
    if not isinstance(payload, dict):
        raise OutboxError("invalid_action_payload", "工具动作 payload 顶层必须是对象。", 422)
    try:
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise OutboxError("invalid_action_payload", "工具动作 payload 无法规范化。", 422) from exc
    if canonical != preview.payload_json or hashlib.sha256(canonical.encode("utf-8")).hexdigest() != preview.payload_hash:
        raise OutboxError("payload_hash_mismatch", "动作预览 payload 未通过规范化哈希校验。", 409)
    if _has_secret_key(payload) or _has_secret_key(preview.scope) or _has_secret_key(preview.source_refs):
        raise OutboxError("sensitive_action_data", "动作 payload 或来源引用包含敏感字段。", 422)
    scope = preview.scope
    if (
        not isinstance(scope, dict)
        or scope.get("kind") != "single_object"
        or scope.get("site_id") != preview.site
        or scope.get("object_id") != preview.resource_id
        or scope.get("action") != preview.action
    ):
        raise OutboxError("action_scope_mismatch", "工具动作 scope 必须精确绑定站点、对象和动作。", 422)
    if len(preview.source_refs) > 64:
        raise OutboxError("too_many_source_refs", "一个动作最多绑定64条来源证据。", 422)


def _validate_tool_action_envelope(envelope: ToolActionEnvelope) -> None:
    try:
        payload = json.loads(envelope.payload_json)
    except (json.JSONDecodeError, TypeError) as exc:
        raise OutboxError("invalid_action_payload", "工具动作 payload 必须是有效 JSON。", 422) from exc
    if not isinstance(payload, dict):
        raise OutboxError("invalid_action_payload", "工具动作 payload 顶层必须是对象。", 422)
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if canonical != envelope.payload_json or hashlib.sha256(canonical.encode("utf-8")).hexdigest() != envelope.payload_hash:
        raise OutboxError("payload_hash_mismatch", "工具动作 payload hash 与内容不匹配。", 409)
    if envelope.action_id != envelope.capability_id:
        raise OutboxError("action_capability_mismatch", "工具动作只能使用一项精确能力标识。", 422)
    if _has_secret_key(payload) or _has_secret_key(envelope.scope) or _has_secret_key(envelope.source_refs):
        raise OutboxError("sensitive_action_data", "动作 payload 或来源引用包含敏感字段。", 422)
    if (
        envelope.scope.get("kind") != "single_object"
        or envelope.scope.get("site_id") != envelope.site
        or envelope.scope.get("object_id") != envelope.resource_id
        or envelope.scope.get("action") != envelope.action
    ):
        raise OutboxError("action_scope_mismatch", "工具动作 scope 必须精确绑定站点、对象和动作。", 422)


def _validate_action_outbox_binding(row: Any, preview: Any, grant: Any) -> None:
    if (
        row["preview_id"] != preview["id"]
        or row["grant_id"] != grant["id"]
        or row["batch_id"] != grant["batch_id"]
        or row["actor_id"] != preview["actor_id"]
        or row["site"] != preview["site"]
        or row["capability_id"] != preview["capability_id"]
        or row["handler_version"] != preview["handler_version"]
        or row["account_id"] != preview["account_id"]
        or row["resource_id"] != preview["resource_id"]
        or row["action"] != preview["action"]
        or row["payload_json"] != preview["payload_json"]
        or row["payload_hash"] != preview["payload_hash"]
        or row["expected_revision"] != preview["expected_revision"]
        or row["scope"] != preview["scope"]
        or row["source_refs"] != preview["source_refs"]
        or row["payload_hash"] != grant["payload_hash"]
        or _tool_scope_digest(row["scope"]) != grant["scope_hash"]
        or hashlib.sha256(row["payload_json"].encode("utf-8")).hexdigest() != row["payload_hash"]
    ):
        raise OutboxError("tool_action_binding_mismatch", "动作 Outbox 项与原预览或授权不完全匹配。", 409)


def _serialize_tool_action_preview(row: Any) -> dict[str, Any]:
    return {
        "preview_id": row["id"],
        "site": row["site"],
        "capability_id": row["capability_id"],
        "handler_version": row["handler_version"],
        "account_id": row["account_id"],
        "resource_id": row["resource_id"],
        "action": row["action"],
        "payload_hash": row["payload_hash"],
        "expected_revision": row["expected_revision"],
        "scope": row["scope"],
        "source_refs": row["source_refs"],
        "state": row["state"],
        "created_at": _utc(row["created_at"]).isoformat(),
        "expires_at": _utc(row["expires_at"]).isoformat(),
    }


def _serialize_tool_action_grant(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "preview_id": row["preview_id"],
        "batch_id": row["batch_id"],
        "batch_version": row["batch_version"],
        "state": "consumed" if row.get("used_at") is not None else "active",
        "expires_at": _utc(row["expires_at"]).isoformat(),
    }


def _serialize_tool_action_item(row: Any, *, include_payload: bool) -> dict[str, Any]:
    result = {
        "id": row["id"],
        "idempotency_key": row["idempotency_key"],
        "preview_id": row["preview_id"],
        "grant_id": row["grant_id"],
        "batch_id": row["batch_id"],
        "batch_version": row["batch_version"],
        "site": row["site"],
        "capability_id": row["capability_id"],
        "handler_version": row["handler_version"],
        "account_id": row["account_id"],
        "resource_id": row["resource_id"],
        "action": row["action"],
        "payload_hash": row["payload_hash"],
        "expected_revision": row["expected_revision"],
        "actor_id": row["actor_id"],
        "scope": row["scope"],
        "source_refs": row["source_refs"],
        "state": row["state"],
        "attempt_count": row["attempt_count"],
        "attempt_id": row.get("attempt_id"),
        "evidence_ref": row.get("evidence_ref"),
        "remote_receipt_id": row.get("remote_receipt_id"),
        "result_code": row.get("result_code"),
        "created_at": _utc(row["created_at"]).isoformat(),
        "sending_at": _utc(row["sending_at"]).isoformat() if row.get("sending_at") else None,
        "finished_at": _utc(row["finished_at"]).isoformat() if row.get("finished_at") else None,
    }
    if include_payload:
        result["payload_json"] = row["payload_json"]
    return result
