"""Local NapCat account metadata and sender-to-person bindings."""

from __future__ import annotations

import hashlib
import hmac
import asyncio
import json
import time
import uuid
from datetime import timezone
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import select, update

from cutefish.chat.models import Conversation
from cutefish.core.models import TrialBatch, utc_now
from cutefish.memory.models import MemoryPerson
from cutefish.memory.service import MemoryError, MemoryService, OWNER_PERSON_ID

from .models import QqAccount, QqContactIdentity, QqInboundEvent


class QqError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 422):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


class QqService:
    MERGE_QUIET_SECONDS = 1.0
    MERGE_MAX_SECONDS = 5.0

    def __init__(self, session_factory, secrets, memory_service: MemoryService | None = None, *, chat_service=None, batches=None):
        self.session_factory = session_factory
        self.secrets = secrets
        self.memory_service = memory_service or MemoryService(session_factory)
        self.chat_service = chat_service
        self.batches = batches
        self._merge_lock = asyncio.Lock()
        self._merge_windows: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        self._dispatch_locks: dict[tuple[str, str, str], asyncio.Lock] = {}

    async def create_account(self, *, account_id: str, display_name: str, service_url: str | None = None) -> dict[str, Any]:
        account = account_id.strip()
        label = display_name.strip()
        if not account or len(account) > 128:
            raise QqError("invalid_account_id", "QQ账号标识不能为空且最多128个字符。")
        if not label or len(label) > 128:
            raise QqError("invalid_display_name", "账号显示名称不能为空且最多128个字符。")
        url = _validate_local_service_url(service_url)
        digest = hashlib.sha256(account.encode("utf-8")).hexdigest()[:32]
        row = QqAccount(
            account_id=account,
            display_name=label,
            service_url=url,
            secret_name=f"qq_bridge_{digest}",
            webhook_secret_name=f"qq_webhook_{digest}",
            owner_sender_id=None,
            enabled=False,
            group_chat_enabled=False,
            state="needs_credentials",
        )
        async with self.session_factory() as session:
            async with session.begin():
                if await session.get(QqAccount, account) is not None:
                    raise QqError("account_exists", "这个QQ账号标识已经登记。", 409)
                session.add(row)
                await session.flush()
        return self.serialize_account(row, needs_credentials=True)

    async def list_accounts(self) -> list[dict[str, Any]]:
        async with self.session_factory() as session:
            rows = (await session.scalars(select(QqAccount).order_by(QqAccount.created_at))).all()
            accounts = [self._account_snapshot(row) for row in rows]
        result = []
        for account in accounts:
            has_secret = await self._has_secret(account["secret_name"])
            has_webhook_secret = await self._has_secret(account["webhook_secret_name"])
            result.append(self.serialize_account(account, needs_credentials=not (has_secret and has_webhook_secret)))
        return result

    async def status(self) -> dict[str, Any]:
        accounts = await self.list_accounts()
        if not accounts:
            return {
                "display_name": "NapCat QQ桥",
                "status": "needs_credentials",
                "connected": False,
                "needs_credentials": True,
                "needs_connection": False,
                "owner_configured": False,
                "enabled": False,
                "group_chat_enabled": False,
                "detail": "尚未登记QQ桥账号，也没有可用的本机凭据。",
            }
        enabled = any(item["enabled"] for item in accounts)
        credentials_ready = all(not item["needs_credentials"] for item in accounts if item["enabled"])
        owner_configured = any(item["owner_configured"] for item in accounts if item["enabled"])
        needs_credentials = any(item["needs_credentials"] for item in accounts if item["enabled"])
        needs_connection = enabled and credentials_ready
        state = "needs_credentials" if needs_credentials or not enabled else "needs_connection"
        detail = (
            "至少一个已启用账号缺少本机凭据。"
            if needs_credentials
            else "账号已登记；尚未连接或验证本机NapCat服务。"
            if enabled
            else "QQ账号已登记但未启用；控制台不会自动登录或发送消息。"
        )
        return {
            "display_name": "NapCat QQ桥",
            "status": state,
            "connected": False,
            "needs_credentials": needs_credentials or not enabled,
            "needs_connection": needs_connection,
            "owner_configured": owner_configured,
            "enabled": enabled,
            "group_chat_enabled": any(item["group_chat_enabled"] for item in accounts),
            "detail": detail,
        }

    async def bind_owner(self, account_id: str, sender_id: str) -> dict[str, Any]:
        sender = _validate_sender_id(sender_id)
        await self.memory_service.ensure_owner()
        account_snapshot = None
        async with self.session_factory() as session:
            async with session.begin():
                account = await session.get(QqAccount, account_id)
                if account is None:
                    raise QqError("account_not_found", "QQ账号不存在。", 404)
                previous_sender = account.owner_sender_id
                if previous_sender and previous_sender != sender:
                    previous_identity = await session.scalar(
                        select(QqContactIdentity).where(
                            QqContactIdentity.account_id == account_id,
                            QqContactIdentity.sender_id == previous_sender,
                        )
                    )
                    if previous_identity is not None and previous_identity.person_id == OWNER_PERSON_ID:
                        previous_identity.is_owner = False
                identity = await session.scalar(
                    select(QqContactIdentity).where(
                        QqContactIdentity.account_id == account_id,
                        QqContactIdentity.sender_id == sender,
                    )
                )
                if identity is not None and identity.person_id != OWNER_PERSON_ID:
                    raise QqError("sender_already_bound", "这个QQ发送身份已经绑定到其他联系人。", 409)
                if identity is None:
                    identity = QqContactIdentity(
                        id=str(uuid.uuid4()),
                        account_id=account_id,
                        person_id=OWNER_PERSON_ID,
                        sender_id=sender,
                        is_owner=True,
                        is_group=False,
                    )
                    session.add(identity)
                identity.is_owner = True
                identity.is_group = False
                account.owner_sender_id = sender
                account.updated_at = utc_now()
                await session.flush()
                account_snapshot = self._account_snapshot(account)
        return self.serialize_account(
            account_snapshot,
            needs_credentials=not (
                await self._has_secret(account_snapshot["secret_name"])
                and await self._has_secret(account_snapshot["webhook_secret_name"])
            ),
        )

    async def configure_credentials(self, account_id: str, *, napcat_access_token: str, webhook_secret: str) -> dict[str, Any]:
        access = napcat_access_token.strip()
        event_secret = webhook_secret.strip()
        if not access or len(access) > 4096:
            raise QqError("invalid_access_token", "NapCat访问凭据不能为空且最多4096个字符。")
        if len(event_secret) < 32 or len(event_secret) > 4096:
            raise QqError("invalid_webhook_secret", "入站webhook密钥需为32到4096个字符。")
        async with self.session_factory() as session:
            account = await session.get(QqAccount, account_id)
            if account is None:
                raise QqError("account_not_found", "QQ账号不存在。", 404)
            access_name = account.secret_name
            webhook_name = account.webhook_secret_name
        try:
            await self.secrets.put(access_name, access)
            await self.secrets.put(webhook_name, event_secret)
        except Exception as exc:
            raise QqError("credential_store_unavailable", "本机无法安全保存QQ桥接凭据。", 503) from exc
        return {"account_id": account_id, "credentials_configured": True, "enabled": False}

    async def set_enabled(self, account_id: str, *, enabled: bool) -> dict[str, Any]:
        if type(enabled) is not bool:
            raise QqError("invalid_enabled_state", "enabled必须是布尔值。")
        async with self.session_factory() as session:
            async with session.begin():
                account = await session.get(QqAccount, account_id)
                if account is None:
                    raise QqError("account_not_found", "QQ账号不存在。", 404)
                if enabled:
                    if not account.service_url:
                        raise QqError("qq_endpoint_required", "启用前必须配置本机NapCat HTTP地址。", 409)
                    if not account.owner_sender_id:
                        raise QqError("owner_identity_required", "启用前必须绑定管理员QQ身份。", 409)
                    identities = await session.scalars(
                        select(QqContactIdentity).where(
                            QqContactIdentity.account_id == account_id,
                            QqContactIdentity.sender_id == account.owner_sender_id,
                            QqContactIdentity.person_id == OWNER_PERSON_ID,
                            QqContactIdentity.is_owner.is_(True),
                            QqContactIdentity.is_group.is_(False),
                        )
                    )
                    if identities.first() is None:
                        raise QqError("owner_identity_required", "管理员身份映射尚未核验。", 409)
                    if not await self._has_secret(account.secret_name) or not await self._has_secret(account.webhook_secret_name):
                        raise QqError("needs_credentials", "启用前必须保存NapCat访问凭据和独立入站密钥。", 409)
                account.enabled = enabled
                account.state = "enabled" if enabled else "disabled"
                account.updated_at = utc_now()
                await session.flush()
                snapshot = self._account_snapshot(account)
        needs_credentials = not (
            await self._has_secret(snapshot["secret_name"])
            and await self._has_secret(snapshot["webhook_secret_name"])
        )
        return self.serialize_account(snapshot, needs_credentials=needs_credentials)

    async def validate_delivery_target(self, guard: dict[str, Any]) -> None:
        account_id = str(guard.get("account_id") or "")
        sender_id = str(guard.get("recipient_id") or "")
        person_id = str(guard.get("person_id") or "")
        if guard.get("delivery_mode") != "qq" or not account_id or not sender_id or not person_id:
            raise QqError("delivery_target_unavailable", "外发目标缺少已核验的QQ账号、人物或sender身份。", 403)
        async with self.session_factory() as session:
            account = await session.get(QqAccount, account_id)
            identity = await session.scalar(
                select(QqContactIdentity).where(
                    QqContactIdentity.account_id == account_id,
                    QqContactIdentity.sender_id == sender_id,
                )
            )
        if account is None or not account.enabled:
            raise QqError("qq_account_disabled", "QQ账号未启用或不存在。", 403)
        if identity is None or identity.person_id != person_id:
            raise QqError("qq_identity_mismatch", "当前QQ身份未绑定到该人物。", 403)
        if identity.is_group:
            raise QqError("qq_group_disabled", "当前local_trial只允许私聊。", 403)
        if not identity.is_owner:
            raise QqError("qq_owner_only_trial", "当前local_trial只允许已绑定的管理员QQ身份。", 403)
        try:
            person = await self.memory_service.get_person(person_id)
        except MemoryError as exc:
            raise QqError(exc.code, exc.message, exc.status_code) from exc
        if person.state != "active":
            raise QqError("qq_person_unavailable", "绑定人物当前不可用。", 403)
        if identity.is_owner:
            if person.role != "owner" or sender_id != account.owner_sender_id or not account.owner_sender_id:
                raise QqError("qq_owner_identity_mismatch", "管理员QQ身份与账号当前绑定不一致。", 403)
        else:
            raise QqError("qq_owner_identity_mismatch", "当前local_trial只允许管理员QQ身份。", 403)
        url = _validate_local_service_url(account.service_url)
        if not url or not await self._has_secret(account.secret_name):
            raise QqError("needs_credentials", "NapCat本机地址或访问凭据缺失。", 409)

    async def sender_credentials_for_delivery(self, guard: dict[str, Any]) -> dict[str, str]:
        """Return internal sender credentials only after rechecking the exact live target."""
        await self.validate_delivery_target(guard)
        account_id = str(guard["account_id"])
        async with self.session_factory() as session:
            account = await session.get(QqAccount, account_id)
            if account is None or not account.enabled:
                raise QqError("qq_account_disabled", "QQ账号未启用或不存在。", 403)
            service_url = _validate_local_service_url(account.service_url)
            secret_name = account.secret_name
        if not service_url:
            raise QqError("needs_credentials", "NapCat本机地址或访问凭据缺失。", 409)
        try:
            access_token = await self.secrets.get(secret_name)
        except Exception as exc:
            raise QqError("needs_credentials", "NapCat访问凭据不可用。", 409) from exc
        if not access_token:
            raise QqError("needs_credentials", "NapCat访问凭据不可用。", 409)
        return {"service_url": service_url, "access_token": access_token}

    async def webhook_secret_for(self, account_id: str) -> str | None:
        async with self.session_factory() as session:
            account = await session.get(QqAccount, account_id)
            if account is None or not account.enabled:
                return None
            name = account.webhook_secret_name
        try:
            return await self.secrets.get(name)
        except Exception:
            return None

    async def recover_pending_events(self) -> int:
        """Never replay an unfinished inbound event on startup; require local review."""
        now = utc_now()
        async with self.session_factory() as session:
            async with session.begin():
                rows = (
                    await session.scalars(
                        select(QqInboundEvent).where(QqInboundEvent.state.in_(("pending_merge", "processing")))
                    )
                ).all()
                for row in rows:
                    row.state = "needs_review"
                    row.reason = "startup_interrupted"
                    row.updated_at = now
                return len(rows)

    async def accept_event(self, payload: dict[str, Any], *, webhook_secret: str | None) -> dict[str, Any]:
        self_id = _safe_external_id(payload.get("self_id"))
        if not self_id or not webhook_secret:
            raise QqError("webhook_auth_failed", "QQ入站未通过本地账号凭据验证。", 403)
        configured_secret = await self.webhook_secret_for(self_id)
        if not configured_secret or not hmac.compare_digest(configured_secret, webhook_secret):
            raise QqError("webhook_auth_failed", "QQ入站未通过本地账号凭据验证。", 403)

        message_id = _safe_external_id(payload.get("message_id"))
        valid_message_id = bool(message_id and len(message_id) <= 128)
        if not valid_message_id:
            canonical_payload = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            message_id = "event:" + hashlib.sha256(canonical_payload.encode("utf-8")).hexdigest()
        post_type = _event_label(payload.get("post_type"))
        message_type = _event_label(payload.get("message_type"))
        event_type = f"{post_type}:{message_type}"[:64]
        sender_id = _safe_external_id(payload.get("user_id"))
        if sender_id is None and isinstance(payload.get("sender"), dict):
            sender_id = _safe_external_id(payload["sender"].get("user_id"))
        normalized_text = _normalize_message_text(payload.get("message"))
        if normalized_text is not None and len(normalized_text) > 12000:
            normalized_text = None

        async with self.session_factory() as session:
            async with session.begin():
                duplicate = await session.scalar(
                    select(QqInboundEvent).where(
                        QqInboundEvent.account_id == self_id,
                        QqInboundEvent.message_id == message_id,
                        QqInboundEvent.event_type == event_type,
                    )
                )
                if duplicate is not None:
                    return {"event_id": duplicate.id, "state": duplicate.state, "duplicate": True}

                account = await session.get(QqAccount, self_id)
                if account is None or not account.enabled:
                    raise QqError("webhook_auth_failed", "QQ入站未通过本地账号凭据验证。", 403)

                state = "ignored"
                reason: str | None = None
                person_id: str | None = None
                batch_id: str | None = None
                batch_version: int | None = None
                if not valid_message_id:
                    reason = "message_id_missing_or_invalid"
                elif post_type != "message" or message_type != "private":
                    reason = "non_private_message"
                elif sender_id is None:
                    reason = "sender_missing"
                elif sender_id == self_id:
                    reason = "self_echo"
                else:
                    identity = await session.scalar(
                        select(QqContactIdentity).where(
                            QqContactIdentity.account_id == self_id,
                            QqContactIdentity.sender_id == sender_id,
                        )
                    )
                    person = await session.get(MemoryPerson, identity.person_id) if identity is not None else None
                    if identity is None or identity.is_group or person is None:
                        reason = "identity_unbound"
                    elif not identity.is_owner or account.owner_sender_id != sender_id:
                        # S02's local_trial admits only the explicitly bound owner.
                        # Known contacts remain durably recorded but never reach ChatService.
                        reason = "owner_only_trial"
                    elif person.state != "active":
                        reason = "person_unavailable"
                    elif identity.is_owner and (
                        person.role != "owner" or account.owner_sender_id != sender_id
                    ):
                        reason = "owner_identity_mismatch"
                    elif not identity.is_owner and (person.role != "contact" or not person.chat_only):
                        reason = "contact_identity_mismatch"
                    elif not normalized_text:
                        reason = "no_text_content"
                    elif len(normalized_text) > 12000:
                        reason = "message_too_large"
                    else:
                        batch = await self._active_batch_for_session(session, sender_id)
                        if batch is None:
                            state = "needs_review"
                            reason = "running_local_trial_batch_required"
                        else:
                            state = "pending_merge"
                            person_id = person.person_id
                            batch_id = batch.id
                            batch_version = batch.version

                row = QqInboundEvent(
                    id=str(uuid.uuid4()),
                    account_id=self_id,
                    message_id=message_id,
                    event_type=event_type,
                    message_type=message_type,
                    sender_id=sender_id,
                    person_id=person_id,
                    batch_id=batch_id,
                    batch_version=batch_version,
                    normalized_text=normalized_text,
                    payload_json=payload,
                    state=state,
                    reason=reason,
                    created_at=utc_now(),
                    updated_at=utc_now(),
                )
                session.add(row)
                await session.flush()
                result = {"event_id": row.id, "state": row.state, "duplicate": False}

        if state == "pending_merge" and person_id and sender_id and batch_id:
            self._schedule_merge(self_id, person_id, sender_id, batch_id, row.id)
        return result

    async def _active_batch_for_session(self, session, sender_id: str, *, batch_id: str | None = None, version: int | None = None):
        statement = select(TrialBatch).where(
            TrialBatch.module_id == "qq",
            TrialBatch.state == "running",
        )
        if batch_id is not None:
            statement = statement.where(TrialBatch.id == batch_id)
        rows = (await session.scalars(statement.order_by(TrialBatch.started_at.desc()))).all()
        now = utc_now()
        for row in rows:
            if _utc(row.expires_at) <= now or sender_id not in list(row.object_ids or []):
                continue
            if version is not None and row.version != version:
                continue
            condition = row.stop_condition or {}
            if condition.get("kind") == "after_n" and int(row.action_count) >= int(condition.get("max_actions", 0)):
                continue
            return row
        return None

    def _schedule_merge(self, account_id: str, person_id: str, sender_id: str, batch_id: str, event_id: str) -> None:
        key = (account_id, person_id, sender_id, batch_id)
        now = time.monotonic()
        window = self._merge_windows.get(key)
        if window is not None and now - window["started_at"] < self.MERGE_MAX_SECONDS:
            window["event_ids"].append(event_id)
            window["last_received"] = now
            window["changed"].set()
            return
        if window is not None:
            window["changed"].set()
        next_window = {
            "started_at": now,
            "last_received": now,
            "event_ids": [event_id],
            "changed": asyncio.Event(),
        }
        self._merge_windows[key] = next_window
        asyncio.create_task(self._wait_and_merge(key, next_window))

    async def _wait_and_merge(self, key: tuple[str, str, str, str], window: dict[str, Any]) -> None:
        try:
            while True:
                now = time.monotonic()
                quiet_remaining = self.MERGE_QUIET_SECONDS - (now - window["last_received"])
                hard_remaining = self.MERGE_MAX_SECONDS - (now - window["started_at"])
                remaining = min(quiet_remaining, hard_remaining)
                if remaining <= 0:
                    break
                window["changed"].clear()
                try:
                    await asyncio.wait_for(window["changed"].wait(), timeout=remaining)
                except TimeoutError:
                    break
            async with self._merge_lock:
                if self._merge_windows.get(key) is window:
                    self._merge_windows.pop(key, None)
            await self._merge_events(key, list(window["event_ids"]))
        except asyncio.CancelledError:
            raise
        except Exception:
            await self._set_event_state(list(window["event_ids"]), state="needs_review", reason="merge_failed")

    async def _merge_events(self, key: tuple[str, str, str, str], event_ids: list[str]) -> None:
        account_id, person_id, sender_id, batch_id = key
        dispatch_key = (account_id, person_id, sender_id)
        dispatch_lock = self._dispatch_locks.setdefault(dispatch_key, asyncio.Lock())
        async with dispatch_lock:
            async with self.session_factory() as session:
                rows = (
                    await session.scalars(
                        select(QqInboundEvent)
                        .where(QqInboundEvent.id.in_(event_ids))
                        .order_by(QqInboundEvent.created_at, QqInboundEvent.id)
                    )
                ).all()
            if len(rows) != len(event_ids) or any(row.state != "pending_merge" for row in rows):
                await self._set_event_state(event_ids, state="needs_review", reason="merge_source_changed")
                return
            content = "\n".join(row.normalized_text or "" for row in rows).strip()
            if not content or len(content) > 12000:
                await self._set_event_state(event_ids, state="needs_review", reason="merged_message_invalid")
                return

            try:
                await self.validate_delivery_target(
                    {
                        "account_id": account_id,
                        "recipient_id": sender_id,
                        "person_id": person_id,
                        "delivery_mode": "qq",
                    }
                )
                async with self.session_factory() as session:
                    async with session.begin():
                        batch = await self._active_batch_for_session(
                            session,
                            sender_id,
                            batch_id=batch_id,
                            version=rows[0].batch_version,
                        )
                        if batch is None:
                            raise QqError("trial_batch_unavailable", "QQ试用批次已停止、过期或超出范围。", 409)
                        condition = batch.stop_condition or {}
                        current_actions = int(batch.action_count)
                        if condition.get("kind") == "after_n" and current_actions >= int(condition.get("max_actions", 0)):
                            raise QqError("trial_batch_limit_reached", "QQ试用批次已达到本次动作上限。", 409)
                        claimed = await session.execute(
                            update(TrialBatch)
                            .where(
                                TrialBatch.id == batch.id,
                                TrialBatch.state == "running",
                                TrialBatch.version == batch.version,
                                TrialBatch.action_count == current_actions,
                                TrialBatch.expires_at > utc_now(),
                            )
                            .values(action_count=TrialBatch.action_count + 1)
                            .execution_options(synchronize_session=False)
                        )
                        if claimed.rowcount != 1:
                            raise QqError("trial_batch_race", "QQ试用批次状态已变化，请重新评估。", 409)

                conversation = await self._get_or_create_conversation(
                    account_id=account_id,
                    person_id=person_id,
                    sender_id=sender_id,
                    batch_id=batch_id,
                )
                if conversation["state"] == "busy":
                    conversation = await self.chat_service.wait_until_turn_available(conversation["id"])
                if conversation["state"] != "active":
                    await self._set_event_state(
                        event_ids,
                        state="needs_review",
                        reason="conversation_not_active",
                        conversation_id=conversation["id"],
                    )
                    return
                digest = hashlib.sha256("|".join(sorted(event_ids)).encode("utf-8")).hexdigest()
                result = await self.chat_service.send_turn(
                    conversation["id"],
                    content=content,
                    idempotency_key=f"qq-turn:{digest}",
                    source_event_id=f"qq-turn:{digest}",
                )
                request = result.get("request") or {}
                request_state = request.get("state")
                final_state = "processed" if request_state == "completed" else "needs_review"
                final_reason = None if final_state == "processed" else str(request.get("error_code") or request_state or "turn_unverified")
                await self._set_event_state(
                    event_ids,
                    state=final_state,
                    reason=final_reason,
                    conversation_id=conversation["id"],
                    chat_request_id=request.get("local_request_id"),
                )
            except Exception as exc:
                details = getattr(exc, "details", None)
                request_id = details.get("local_request_id") if isinstance(details, dict) else None
                reason = str(getattr(exc, "code", "turn_failed"))[:128]
                state = "needs_review" if request_id or reason in {
                    "provider_outcome_unknown", "conversation_stale", "conversation_unavailable",
                    "persona_config_mismatch", "trial_batch_unavailable", "trial_batch_limit_reached",
                } else "needs_input"
                await self._set_event_state(
                    event_ids,
                    state=state,
                    reason=reason,
                    chat_request_id=request_id,
                )

    async def _get_or_create_conversation(self, *, account_id: str, person_id: str, sender_id: str, batch_id: str) -> dict[str, Any]:
        if self.chat_service is None:
            raise QqError("chat_service_unavailable", "聊天服务未接入；入站记录已保留。", 503)
        async with self.session_factory() as session:
            rows = (
                await session.scalars(
                    select(Conversation)
                    .where(
                        Conversation.person_id == person_id,
                        Conversation.account_id == account_id,
                        Conversation.recipient_id == sender_id,
                        Conversation.channel == "qq",
                        Conversation.scope == "production",
                    )
                    .order_by(Conversation.updated_at.desc())
                )
            ).all()
            latest = rows[0] if rows else None
            if latest is not None and latest.state in {"active", "busy"}:
                return self.chat_service.serialize_conversation(latest)
            if latest is not None:
                raise QqError("conversation_requires_owner_review", "已有QQ会话已停止或待核对；需要管理员在本机重新评估。", 409)
        return await self.chat_service.create_conversation(
            person_id=person_id,
            scope="production",
            purpose="production",
            channel="qq",
            delivery_mode="qq",
            account_id=account_id,
            recipient_id=sender_id,
            batch_id=batch_id,
        )

    async def _set_event_state(
        self,
        event_ids: list[str],
        *,
        state: str,
        reason: str | None,
        conversation_id: str | None = None,
        chat_request_id: str | None = None,
    ) -> None:
        async with self.session_factory() as session:
            async with session.begin():
                rows = (await session.scalars(select(QqInboundEvent).where(QqInboundEvent.id.in_(event_ids)))).all()
                for row in rows:
                    row.state = state
                    row.reason = reason
                    row.conversation_id = conversation_id or row.conversation_id
                    row.chat_request_id = chat_request_id or row.chat_request_id
                    row.updated_at = utc_now()

    async def bind_contact(self, account_id: str, *, person_id: str, sender_id: str) -> dict[str, Any]:
        sender = _validate_sender_id(sender_id)
        try:
            person = await self.memory_service.get_person(person_id)
        except MemoryError as exc:
            raise QqError(exc.code, exc.message, exc.status_code) from exc
        if person.role != "contact" or not person.chat_only or person.state != "active":
            raise QqError("contact_binding_not_allowed", "QQ普通身份只能绑定到有效的chat-only联系人。", 403)
        async with self.session_factory() as session:
            async with session.begin():
                account = await session.get(QqAccount, account_id)
                if account is None:
                    raise QqError("account_not_found", "QQ账号不存在。", 404)
                existing = await session.scalar(
                    select(QqContactIdentity).where(
                        QqContactIdentity.account_id == account_id,
                        QqContactIdentity.sender_id == sender,
                    )
                )
                if existing is not None and existing.person_id != person_id:
                    raise QqError("sender_already_bound", "这个QQ发送身份已经绑定到其他人物。", 409)
                if existing is None:
                    existing = QqContactIdentity(
                        id=str(uuid.uuid4()),
                        account_id=account_id,
                        person_id=person_id,
                        sender_id=sender,
                        is_owner=False,
                        is_group=False,
                    )
                    session.add(existing)
                existing.is_owner = False
                existing.is_group = False
                await session.flush()
                return {
                    "id": existing.id,
                    "account_id": existing.account_id,
                    "person_id": existing.person_id,
                    "sender_id": existing.sender_id,
                    "is_owner": False,
                    "is_group": False,
                }

    async def list_identities(self, account_id: str) -> list[dict[str, Any]]:
        async with self.session_factory() as session:
            account = await session.get(QqAccount, account_id)
            if account is None:
                raise QqError("account_not_found", "QQ账号不存在。", 404)
            rows = (
                await session.scalars(
                    select(QqContactIdentity)
                    .where(QqContactIdentity.account_id == account_id)
                    .order_by(QqContactIdentity.created_at)
                )
            ).all()
            return [
                {
                    "id": row.id,
                    "account_id": row.account_id,
                    "person_id": row.person_id,
                    "sender_id": row.sender_id,
                    "is_owner": bool(row.is_owner),
                    "is_group": bool(row.is_group),
                }
                for row in rows
            ]

    async def _has_secret(self, secret_name: str) -> bool:
        try:
            return bool(await self.secrets.get(secret_name))
        except Exception:
            return False

    @staticmethod
    def _account_snapshot(row: QqAccount) -> dict[str, Any]:
        return {
            "account_id": row.account_id,
            "display_name": row.display_name,
            "service_url": row.service_url,
            "secret_name": row.secret_name,
            "webhook_secret_name": row.webhook_secret_name,
            "owner_sender_id": row.owner_sender_id,
            "enabled": bool(row.enabled),
            "group_chat_enabled": bool(row.group_chat_enabled),
            "state": row.state,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }

    @staticmethod
    def serialize_account(row: dict[str, Any] | QqAccount, *, needs_credentials: bool) -> dict[str, Any]:
        data = QqService._account_snapshot(row) if isinstance(row, QqAccount) else row
        enabled = bool(data["enabled"])
        needs_connection = enabled and not needs_credentials
        status = "needs_credentials" if needs_credentials else "needs_connection" if enabled else "disabled"
        return {
            "account_id": data["account_id"],
            "display_name": data["display_name"],
            "status": status,
            "needs_credentials": bool(needs_credentials),
            "needs_connection": needs_connection,
            "owner_configured": bool(data["owner_sender_id"]),
            "enabled": enabled,
            "group_chat_enabled": bool(data["group_chat_enabled"]),
            "service_url": data["service_url"],
            "owner_sender_id": data["owner_sender_id"],
        }


def _validate_local_service_url(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    url = value.strip()
    try:
        parsed = urlsplit(url)
    except ValueError as exc:
        raise QqError("invalid_service_url", "NapCat服务地址格式无效。") from exc
    if parsed.scheme not in {"http", "https", "ws", "wss"} or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise QqError("service_url_must_be_local", "NapCat地址必须指向本机loopback服务。", 403)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise QqError("invalid_service_url", "NapCat地址不能包含凭据、查询参数或片段。")
    return url


def _validate_sender_id(value: str) -> str:
    if not isinstance(value, str):
        raise QqError("invalid_sender_id", "QQ sender_id必须是文本标识。")
    sender = value.strip()
    if not sender or len(sender) > 128 or any(not char.isprintable() for char in sender):
        raise QqError("invalid_sender_id", "QQ sender_id不能为空、不能含控制字符且最多128个字符。")
    return sender


def _utc(value):
    """Compare SQLite's naive timestamps and aware timestamps consistently."""
    from datetime import datetime

    if not isinstance(value, datetime):
        raise TypeError("expected a datetime")
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _safe_external_id(value: Any) -> str | None:
    """Normalize OneBot IDs without accepting booleans or lossy coercions."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        if value < 0:
            return None
        result = str(value)
    elif isinstance(value, str):
        result = value.strip()
    else:
        return None
    if not result or len(result) > 128 or any(not char.isprintable() or char.isspace() for char in result):
        return None
    return result


def _event_label(value: Any) -> str:
    if not isinstance(value, str):
        return "unknown"
    label = value.strip()
    if not label or len(label) > 24 or any(not char.isprintable() or char.isspace() for char in label):
        return "unknown"
    return label


def _normalize_message_text(value: Any) -> str | None:
    """Accept plain OneBot text only; unsupported segments stay reviewable on disk."""
    if isinstance(value, str):
        if "[CQ:" in value.upper():
            return None
        text = value
    elif isinstance(value, list):
        fragments: list[str] = []
        for segment in value:
            if not isinstance(segment, dict) or segment.get("type") != "text":
                return None
            data = segment.get("data")
            if not isinstance(data, dict) or not isinstance(data.get("text"), str):
                return None
            fragments.append(data["text"])
        text = "".join(fragments)
    else:
        return None
    return text.strip() or None
