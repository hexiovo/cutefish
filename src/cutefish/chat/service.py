"""Conversation persistence, one-shot DeepSeek turns, and delivery guards."""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from cutefish.core.contracts import ProviderUsageEvidence
from cutefish.core.models import utc_now
from cutefish.memory.service import MemoryError, MemoryService, OWNER_PERSON_ID

from .models import (
    ChatDraft,
    ChatMessage,
    ChatRequest,
    Conversation,
    InterviewSession,
    PersonaApproval,
    PersonaTrialFeedback,
    PersonaVersion,
)
from .prompt import PromptBuilder, PromptTooLarge
from .provider import (
    DEEPSEEK_MODEL,
    DEEPSEEK_PRICE_VERSION,
    DEEPSEEK_PROVIDER,
    DeepSeekAdapter,
    ProviderError,
    ProviderResult,
)


SCOPES = frozenset({"production", "calibration"})
PURPOSES = frozenset({"production", "persona_interview", "persona_trial"})
CONVERSATION_STATES = frozenset({"active", "busy", "stopped", "taken_over", "needs_reconciliation"})


class ChatError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 422, *, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details


@dataclass(frozen=True, slots=True)
class TurnSnapshot:
    conversation: dict[str, Any]
    person_role: str
    persona: dict[str, Any] | None
    interview_id: str | None
    memories: list[dict[str, Any]]
    history: list[dict[str, str]]


class ChatService:
    """Owns stored turns; external delivery remains a separate explicit path."""

    def __init__(
        self,
        session_factory,
        budget,
        secrets,
        memory_service: MemoryService | None = None,
        provider_adapter: DeepSeekAdapter | None = None,
        prompt_builder: PromptBuilder | None = None,
    ):
        self.session_factory = session_factory
        self.budget = budget
        self.secrets = secrets
        self.memory_service = memory_service or MemoryService(session_factory)
        self.provider = provider_adapter or DeepSeekAdapter()
        self.prompt_builder = prompt_builder or PromptBuilder()

    def configuration_fingerprint(self, persona_content_hash: str | None = None) -> str:
        payload = {
            "provider": self.provider.config_fingerprint,
            "prompt_builder": self.prompt_builder.fingerprint(),
            "persona_content_hash": persona_content_hash,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    async def startup(self) -> None:
        await self.memory_service.ensure_owner()

    async def create_conversation(
        self,
        *,
        person_id: str | None = None,
        scope: str = "production",
        purpose: str = "production",
        channel: str = "ui",
        delivery_mode: str = "ui",
        account_id: str | None = None,
        recipient_id: str | None = None,
        batch_id: str | None = None,
        title: str | None = None,
        persona_version_id: str | None = None,
    ) -> dict[str, Any]:
        if scope not in SCOPES or purpose not in PURPOSES:
            raise ChatError("invalid_conversation_scope", "聊天范围或用途不受支持。")
        if (purpose == "production") != (scope == "production"):
            raise ChatError("invalid_conversation_scope", "正式对话使用production范围，访谈/试聊使用calibration范围。")
        if channel not in {"ui", "qq"} or delivery_mode != channel:
            raise ChatError("invalid_delivery_mode", "对话通道与投递模式不匹配。")
        if channel == "qq" and (not account_id or not recipient_id):
            raise ChatError("delivery_target_unavailable", "QQ对话必须绑定账号和已核验收件身份。", 403)
        if purpose != "production" and channel != "ui":
            raise ChatError("calibration_channel_unavailable", "人格访谈和试聊只能在本机界面进行。", 403)
        if title is not None and (not title.strip() or len(title.strip()) > 256):
            raise ChatError("invalid_title", "会话标题不能为空且最多256个字符。")

        await self.memory_service.ensure_owner()
        target_person_id = person_id or OWNER_PERSON_ID
        try:
            person = await self.memory_service.get_person(target_person_id)
        except MemoryError as exc:
            raise ChatError(exc.code, exc.message, exc.status_code) from exc
        if person.state != "active":
            raise ChatError("person_not_available", "此人物当前不可用于聊天。", 404)
        if channel == "ui" and target_person_id != OWNER_PERSON_ID:
            raise ChatError("ui_actor_mismatch", "本机界面聊天固定使用已认证管理员身份。", 403)
        if channel == "qq" and person.role == "owner" and not recipient_id:
            raise ChatError("owner_identity_required", "QQ管理员对话缺少已核验的发送身份。", 403)

        persona: PersonaVersion | None = None
        async with self.session_factory() as session:
            if purpose == "production":
                if persona_version_id:
                    persona = await session.get(PersonaVersion, persona_version_id)
                else:
                    persona = await session.scalar(
                        select(PersonaVersion)
                        .where(PersonaVersion.state == "approved")
                        .order_by(PersonaVersion.version_number.desc())
                        .limit(1)
                    )
                if persona is None or persona.state != "approved":
                    raise ChatError("persona_unapproved", "尚无获管理员批准的人格版本，先完成一次人格访谈和试聊。", 409)
                if persona.config_fingerprint != self.configuration_fingerprint(persona.content_hash):
                    raise ChatError("persona_config_mismatch", "人格版本绑定的模型配置已变化，需要在本机重新试聊并批准。", 409)
            elif purpose == "persona_trial":
                if not persona_version_id:
                    raise ChatError("persona_version_required", "人格试聊必须指定一个已保存的人格草案。")
                persona = await session.get(PersonaVersion, persona_version_id)
                if persona is None or persona.state != "draft":
                    raise ChatError("persona_draft_unavailable", "只能试聊尚未批准的人格草案。", 409)
                if persona.config_fingerprint != self.configuration_fingerprint(persona.content_hash):
                    raise ChatError("persona_config_mismatch", "人格草案绑定的模型配置已变化，请新建草案后试聊。", 409)

        conversation_id = str(uuid.uuid4())
        row = Conversation(
            id=conversation_id,
            person_id=target_person_id,
            scope=scope,
            channel=channel,
            purpose=purpose,
            delivery_mode=delivery_mode,
            revision=0,
            state="active",
            account_id=account_id,
            recipient_id=recipient_id,
            batch_id=batch_id,
            persona_version_id=persona.id if persona is not None else None,
            config_fingerprint=self.configuration_fingerprint(persona.content_hash if persona is not None else None),
            title=title.strip() if title else None,
            created_at=utc_now(),
            updated_at=utc_now(),
        )
        async with self.session_factory() as session:
            try:
                async with session.begin():
                    session.add(row)
                    if purpose == "persona_interview":
                        session.add(
                            InterviewSession(
                                id=str(uuid.uuid4()),
                                person_id=target_person_id,
                                persona_version_id=None,
                                conversation_id=conversation_id,
                                state="active",
                                title=title.strip() if title else "人格访谈",
                                created_at=utc_now(),
                            )
                        )
                    await session.flush()
            except IntegrityError as exc:
                raise ChatError("conversation_create_failed", "会话未能保存，请刷新后重试。", 409) from exc
        return self.serialize_conversation(row)

    async def list_conversations(
        self,
        *,
        person_id: str | None = None,
        scope: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        if scope is not None and scope not in SCOPES:
            raise ChatError("invalid_scope", "scope只能是production或calibration。")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ChatError("invalid_limit", "limit必须在1到100之间。")
        target_person_id = person_id or OWNER_PERSON_ID
        async with self.session_factory() as session:
            statement = select(Conversation).where(Conversation.person_id == target_person_id)
            if scope:
                statement = statement.where(Conversation.scope == scope)
            rows = (
                await session.scalars(statement.order_by(Conversation.updated_at.desc()).limit(limit))
            ).all()
            return [self.serialize_conversation(row) for row in rows]

    async def get_conversation(self, conversation_id: str) -> dict[str, Any]:
        async with self.session_factory() as session:
            row = await session.get(Conversation, conversation_id)
            if row is None:
                raise ChatError("conversation_not_found", "会话不存在。", 404)
            return self.serialize_conversation(row)

    async def wait_until_turn_available(
        self,
        conversation_id: str,
        *,
        timeout_seconds: float = 30.0,
    ) -> dict[str, Any]:
        """Wait a bounded time for an in-flight turn, then return its latest state."""
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or not 0 <= timeout_seconds <= 30:
            raise ChatError("invalid_wait_timeout", "会话等待时间必须在0到30秒之间。", 422)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + float(timeout_seconds)
        while True:
            conversation = await self.get_conversation(conversation_id)
            state = conversation["state"]
            if state == "active" and not conversation.get("stop_requested") and not conversation.get("human_takeover"):
                return conversation
            if state != "busy" or conversation.get("stop_requested") or conversation.get("human_takeover"):
                raise ChatError("conversation_unavailable", "会话已停止、接管或需要核对。", 409)
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise ChatError("conversation_busy_timeout", "会话在等待时限内仍未空闲；入站消息保留待复核。", 409)
            await asyncio.sleep(min(0.1, remaining))

    async def list_messages(self, conversation_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ChatError("invalid_limit", "消息每页需为1到200条。")
        async with self.session_factory() as session:
            conversation = await session.get(Conversation, conversation_id)
            if conversation is None:
                raise ChatError("conversation_not_found", "会话不存在。", 404)
            rows = (
                await session.scalars(
                    select(ChatMessage)
                    .where(ChatMessage.conversation_id == conversation_id)
                    .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
                    .limit(limit)
                )
            ).all()
            return [self.serialize_message(row) for row in reversed(rows)]

    async def send_turn(
        self,
        conversation_id: str,
        *,
        content: str,
        idempotency_key: str,
        request_id: str | None = None,
        source_event_id: str | None = None,
        is_background: bool = False,
        pre_dispatch_check: Callable[[], Awaitable[None]] | None = None,
    ) -> dict[str, Any]:
        user_content = content.strip()
        if not user_content or len(user_content) > 12000:
            raise ChatError("invalid_message", "消息不能为空且最多12000个字符。")
        if not idempotency_key or len(idempotency_key) > 256:
            raise ChatError("invalid_idempotency_key", "必须提供不超过256字符的请求幂等键。")
        if source_event_id is not None and (not source_event_id or len(source_event_id) > 256):
            raise ChatError("invalid_source_event", "来源事件ID无效。")
        if type(is_background) is not bool:
            raise ChatError("invalid_request", "后台预算标记必须是布尔值。")

        existing = await self._find_request_by_key(idempotency_key)
        if existing is not None:
            if (
                existing["conversation"]["id"] != conversation_id
                or existing["inbound_message"]["content"] != user_content
            ):
                raise ChatError("idempotency_conflict", "同一幂等键已用于不同会话或不同消息。", 409)
            return existing

        api_key = await self.secrets.get("deepseek_api_key")
        if not api_key:
            raise ChatError("needs_credentials", "请先在本机凭据设置中配置DeepSeek API Key；本次没有调用模型。", 409)

        snapshot = await self._prepare_turn(conversation_id, user_content)
        try:
            built = self.prompt_builder.build(
                person_role=snapshot.person_role,
                purpose=snapshot.conversation["purpose"],
                history=[*snapshot.history, {"role": "user", "content": user_content}],
                persona_content=snapshot.persona["content"] if snapshot.persona else None,
                memories=snapshot.memories,
            )
        except PromptTooLarge as exc:
            raise ChatError("prompt_too_large", str(exc), 413) from exc

        local_request_id = str(uuid.uuid4())
        budget_key = "chat:" + hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
        reservation: dict[str, Any] | None = None
        try:
            reservation = await self.budget.reserve(
                idempotency_key=budget_key,
                max_output_tokens=self.provider.config.max_tokens,
                purpose=f"chat.{snapshot.conversation['purpose']}",
                provider=DEEPSEEK_PROVIDER,
                model=DEEPSEEK_MODEL,
                price_version=DEEPSEEK_PRICE_VERSION,
                request_id=request_id,
                person_id=snapshot.conversation["person_id"],
                account_id=snapshot.conversation.get("account_id"),
                is_background=is_background,
            )
        except Exception as exc:
            code = getattr(exc, "code", "budget_unavailable")
            message = getattr(exc, "message", "预算预留失败，本次没有调用模型。")
            raise ChatError(code, message, getattr(exc, "status_code", 409)) from exc

        inbound_id = str(uuid.uuid4())
        current_revision = int(snapshot.conversation["revision"])
        try:
            async with self.session_factory() as session:
                async with session.begin():
                    duplicate = await session.scalar(
                        select(ChatRequest).where(ChatRequest.idempotency_key == idempotency_key)
                    )
                    if duplicate is not None:
                        duplicate_data = await self._serialize_request_in_session(session, duplicate)
                        if (
                            duplicate_data["conversation"]["id"] != conversation_id
                            or duplicate_data["inbound_message"]["content"] != user_content
                        ):
                            raise ChatError("idempotency_conflict", "同一幂等键已用于不同会话或不同消息。", 409)
                        return duplicate_data
                    conversation = await session.get(Conversation, conversation_id)
                    if conversation is None:
                        raise ChatError("conversation_not_found", "会话不存在。", 404)
                    if conversation.revision != current_revision or conversation.state != "active":
                        raise ChatError("conversation_stale", "会话已变化或忙碌，请刷新后再提交。", 409)
                    claimed = await session.execute(
                        update(Conversation)
                        .where(
                            Conversation.id == conversation_id,
                            Conversation.revision == current_revision,
                            Conversation.state == "active",
                            Conversation.stop_requested.is_(False),
                            Conversation.human_takeover.is_(False),
                        )
                        .values(
                            state="busy",
                            revision=Conversation.revision + 1,
                            updated_at=utc_now(),
                        )
                    )
                    if claimed.rowcount != 1:
                        raise ChatError("conversation_stale", "另一个请求已占用或更新此会话，请刷新后再提交。", 409)
                    await session.refresh(conversation)
                    inbound = ChatMessage(
                        id=inbound_id,
                        conversation_id=conversation.id,
                        person_id=conversation.person_id,
                        scope=conversation.scope,
                        role="user",
                        content=user_content,
                        channel=conversation.channel,
                        source_event_id=source_event_id,
                        config_fingerprint=conversation.config_fingerprint,
                        persona_version_id=conversation.persona_version_id,
                        created_at=utc_now(),
                    )
                    session.add(inbound)
                    request_row = ChatRequest(
                        local_request_id=local_request_id,
                        idempotency_key=idempotency_key,
                        conversation_id=conversation.id,
                        inbound_message_id=inbound_id,
                        reservation_id=reservation["reservation_id"],
                        config_fingerprint=conversation.config_fingerprint,
                        persona_version_id=conversation.persona_version_id,
                        source_revision=conversation.revision,
                        state="dispatching",
                        created_at=utc_now(),
                        updated_at=utc_now(),
                    )
                    session.add(request_row)
                    await session.flush()
        except IntegrityError as exc:
            duplicate = await self._find_request_by_key(idempotency_key)
            if duplicate is not None:
                return duplicate
            await self._release_if_reserved(reservation["reservation_id"])
            raise ChatError("idempotency_conflict", "请求幂等键或消息事件已被使用。", 409) from exc
        except ChatError:
            await self._release_if_reserved(reservation["reservation_id"])
            raise
        except Exception:
            await self._release_if_reserved(reservation["reservation_id"])
            raise

        current_fingerprint = self.configuration_fingerprint(
            snapshot.persona["content_hash"] if snapshot.persona else None
        )
        if snapshot.conversation["config_fingerprint"] != current_fingerprint:
            await self._mark_pre_dispatch_failure(local_request_id, "persona_config_mismatch")
            await self._release_if_reserved(reservation["reservation_id"])
            raise ChatError("persona_config_mismatch", "模型或提示词配置在请求派发前发生变化；请刷新后重试。", 409)

        if pre_dispatch_check is not None:
            try:
                await pre_dispatch_check()
            except ChatError as exc:
                await self._mark_pre_dispatch_failure(local_request_id, exc.code)
                await self._release_if_reserved(reservation["reservation_id"])
                raise
            except Exception as exc:
                await self._mark_pre_dispatch_failure(local_request_id, "pre_dispatch_guard_failed")
                await self._release_if_reserved(reservation["reservation_id"])
                raise ChatError(
                    "pre_dispatch_guard_failed",
                    "派发前安全检查未通过；本次没有调用模型。",
                    409,
                ) from exc

        try:
            await self.budget.mark_dispatched(reservation["reservation_id"])
        except Exception as exc:
            await self._mark_pre_dispatch_failure(local_request_id, str(getattr(exc, "code", "budget_dispatch_failed")))
            await self._release_if_reserved(reservation["reservation_id"])
            raise ChatError("budget_dispatch_failed", "费用预留无法安全派发，本次没有调用模型。", 409) from exc

        try:
            result: ProviderResult = await self.provider.complete(api_key=api_key, messages=built.messages)
        except ProviderError as exc:
            if exc.uncertain:
                await self.budget.mark_unknown(reservation["reservation_id"])
                await self._mark_provider_unknown(local_request_id, exc.code)
            else:
                await self._release_if_reserved(reservation["reservation_id"])
                await self._mark_pre_dispatch_failure(local_request_id, exc.code)
            raise ChatError(
                exc.code,
                exc.message,
                502 if exc.uncertain else 409,
                details={"local_request_id": local_request_id} if exc.uncertain else None,
            ) from exc
        except Exception as exc:
            await self.budget.mark_unknown(reservation["reservation_id"])
            await self._mark_provider_unknown(local_request_id, "provider_outcome_unknown")
            raise ChatError(
                "provider_outcome_unknown",
                "模型请求结果不确定，费用预留已保留；请先查询请求状态，不要重新提交同一轮。",
                502,
                details={"local_request_id": local_request_id},
            ) from exc

        usage = result.usage
        evidence = ProviderUsageEvidence(
            reservation_id=reservation["reservation_id"],
            provider=DEEPSEEK_PROVIDER,
            model=DEEPSEEK_MODEL,
            provider_request_id=result.provider_response_id,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            actual_cost=usage.conservative_cost(),
            evidence_ref=f"deepseek://chat/completions/{result.provider_response_id}",
            cache_tokens=usage.cache_hit_tokens,
        )
        try:
            settled = await self.budget.settle_verified(evidence)
        except Exception:
            await self.budget.mark_unknown(reservation["reservation_id"])
            settled = {"state": "unknown", "reservation_id": reservation["reservation_id"]}

        return await self._save_provider_result(
            local_request_id,
            result=result,
            budget_state=settled["state"],
            actual_cost=evidence.actual_cost,
            usage=usage.as_dict(),
            dropped_history=built.dropped_history_count,
            prompt_bytes=built.utf8_bytes,
            prompt_tokens_estimate=built.estimated_input_tokens,
            prompt_token_estimate_status=built.token_estimate_status,
            input_message_id=inbound_id,
        )

    async def _prepare_turn(self, conversation_id: str, user_content: str) -> TurnSnapshot:
        async with self.session_factory() as session:
            conversation = await session.get(Conversation, conversation_id)
            if conversation is None:
                raise ChatError("conversation_not_found", "会话不存在。", 404)
            if conversation.state != "active" or conversation.stop_requested or conversation.human_takeover:
                raise ChatError("conversation_unavailable", "会话已停止、忙碌或处于人工接管状态。", 409)
            if conversation.channel not in {"ui", "qq"} or conversation.delivery_mode != conversation.channel:
                raise ChatError("invalid_delivery_mode", "会话通道配置无效。", 409)
            # The memory service performs the active-person check, without broadening scope.
            try:
                person_row = await self.memory_service.get_person(conversation.person_id)
            except MemoryError as exc:
                raise ChatError(exc.code, exc.message, exc.status_code) from exc
            if person_row.state != "active":
                raise ChatError("person_not_available", "聊天对象当前不可用。", 409)

            persona: PersonaVersion | None = None
            if conversation.persona_version_id:
                persona = await session.get(PersonaVersion, conversation.persona_version_id)
                if persona is None:
                    raise ChatError("persona_version_not_found", "会话绑定的人格版本不存在。", 409)
                expected_states = {"approved", "superseded"} if conversation.scope == "production" else {"draft"}
                if persona.state not in expected_states:
                    raise ChatError("persona_version_unavailable", "会话绑定的人格版本状态已变化。", 409)
                if persona.content_hash != hashlib.sha256(persona.content.encode("utf-8")).hexdigest():
                    raise ChatError("persona_hash_mismatch", "人格版本内容校验失败。", 409)
                if conversation.config_fingerprint != persona.config_fingerprint:
                    raise ChatError("persona_config_mismatch", "会话与人格版本的模型配置指纹不一致。", 409)
            elif conversation.purpose == "production" or conversation.purpose == "persona_trial":
                raise ChatError("persona_version_required", "此对话用途需要绑定的人格版本。", 409)

            interview = None
            if conversation.purpose in {"persona_interview", "persona_trial"}:
                interview = await session.scalar(
                    select(InterviewSession).where(InterviewSession.conversation_id == conversation.id)
                )
                if interview is None or interview.state != "active":
                    raise ChatError("interview_unavailable", "人格访谈记录不存在或已完成。", 409)
                if conversation.purpose == "persona_trial" and interview.persona_version_id != conversation.persona_version_id:
                    raise ChatError("interview_version_mismatch", "访谈与试聊人格版本不一致。", 409)

            history_rows = (
                await session.scalars(
                    select(ChatMessage)
                    .where(ChatMessage.conversation_id == conversation.id)
                    .order_by(ChatMessage.created_at, ChatMessage.id)
                )
            ).all()
            history = [
                {"role": row.role, "content": row.content}
                for row in history_rows
                if row.role in {"user", "assistant"}
                and not (isinstance(row.token_usage_json, dict) and row.token_usage_json.get("turn_state") == "stale")
            ]
            conversation_data = self.serialize_conversation(conversation)
            persona_data = {
                "id": persona.id,
                "content": persona.content,
                "content_hash": persona.content_hash,
                "config_fingerprint": persona.config_fingerprint,
            } if persona is not None else None
            current_fingerprint = self.configuration_fingerprint(persona.content_hash if persona is not None else None)
            if conversation.config_fingerprint != current_fingerprint:
                raise ChatError("persona_config_mismatch", "会话绑定的模型或提示词配置已变化，需要在当前配置下重新试聊。", 409)
            interview_id = interview.id if interview is not None else None

        try:
            memories = await self.memory_service.list_items(
                person_id=conversation_data["person_id"],
                scope=conversation_data["scope"],
                include_history=False,
                limit=self.prompt_builder.MAX_MEMORY_ITEMS,
            )
        except MemoryError as exc:
            raise ChatError(exc.code, exc.message, exc.status_code) from exc
        return TurnSnapshot(
            conversation=conversation_data,
            person_role=person_row.role,
            persona=persona_data,
            interview_id=interview_id,
            memories=memories,
            history=history,
        )

    async def _find_request_by_key(self, idempotency_key: str) -> dict[str, Any] | None:
        async with self.session_factory() as session:
            request_row = await session.scalar(
                select(ChatRequest).where(ChatRequest.idempotency_key == idempotency_key)
            )
            if request_row is None:
                return None
            return await self._serialize_request_in_session(session, request_row)

    async def _serialize_request_in_session(self, session, row: ChatRequest) -> dict[str, Any]:
        inbound = await session.get(ChatMessage, row.inbound_message_id)
        response = await session.get(ChatMessage, row.response_message_id) if row.response_message_id else None
        conversation = await session.get(Conversation, row.conversation_id)
        if inbound is None or conversation is None:
            raise ChatError("request_corrupt", "聊天请求引用的数据不完整。", 500)
        return {
            "conversation": self.serialize_conversation(conversation),
            "message": self.serialize_message(response) if response else None,
            "inbound_message": self.serialize_message(inbound),
            "request": self.serialize_request(row),
            "usage": response.token_usage_json if response else None,
        }

    async def _mark_pre_dispatch_failure(self, local_request_id: str, code: str) -> None:
        async with self.session_factory() as session:
            async with session.begin():
                row = await session.get(ChatRequest, local_request_id)
                if row is None:
                    return
                row.state = "failed"
                row.error_code = code
                row.updated_at = utc_now()
                conversation = await session.get(Conversation, row.conversation_id)
                if conversation is not None and conversation.state == "busy":
                    conversation.state = "active"
                    conversation.revision += 1
                    conversation.updated_at = utc_now()

    async def _mark_provider_unknown(self, local_request_id: str, code: str) -> None:
        async with self.session_factory() as session:
            async with session.begin():
                row = await session.get(ChatRequest, local_request_id)
                if row is None:
                    return
                row.state = "unknown"
                row.error_code = code
                row.updated_at = utc_now()
                conversation = await session.get(Conversation, row.conversation_id)
                if conversation is not None:
                    conversation.state = "needs_reconciliation"
                    conversation.revision += 1
                    conversation.updated_at = utc_now()

    async def _save_provider_result(
        self,
        local_request_id: str,
        *,
        result: ProviderResult,
        budget_state: str,
        actual_cost: Decimal,
        usage: dict[str, Any],
        dropped_history: int,
        prompt_bytes: int,
        prompt_tokens_estimate: int,
        prompt_token_estimate_status: str,
        input_message_id: str,
    ) -> dict[str, Any]:
        response_id = str(uuid.uuid4())
        now = utc_now()
        async with self.session_factory() as session:
            async with session.begin():
                request_row = await session.get(ChatRequest, local_request_id)
                if request_row is None:
                    raise ChatError("request_not_found", "聊天请求不存在。", 404)
                conversation = await session.get(Conversation, request_row.conversation_id)
                if conversation is None:
                    raise ChatError("conversation_not_found", "会话不存在。", 404)
                stale = (
                    conversation.revision != request_row.source_revision
                    or conversation.state != "busy"
                    or conversation.stop_requested
                    or conversation.human_takeover
                )
                inbound = await session.get(ChatMessage, request_row.inbound_message_id)
                if stale and inbound is not None:
                    inbound.token_usage_json = {**(inbound.token_usage_json or {}), "turn_state": "stale"}
                response = ChatMessage(
                    id=response_id,
                    conversation_id=conversation.id,
                    person_id=conversation.person_id,
                    scope=conversation.scope,
                    role="assistant",
                    content=result.text,
                    channel=conversation.channel,
                    provider=DEEPSEEK_PROVIDER,
                    model=result.returned_model,
                    configured_model=result.configured_model,
                    provider_response_id=result.provider_response_id,
                    config_fingerprint=request_row.config_fingerprint,
                    persona_version_id=conversation.persona_version_id,
                    finish_reason=result.finish_reason,
                    token_usage_json={
                        **usage,
                        "actual_cost": str(actual_cost),
                        "currency": "CNY",
                        "reservation_id": request_row.reservation_id,
                        "budget_state": budget_state,
                        "dropped_history_messages": dropped_history,
                        "prompt_utf8_bytes": prompt_bytes,
                        "prompt_tokens_estimate": prompt_tokens_estimate,
                        "prompt_token_estimate_status": prompt_token_estimate_status,
                        "turn_state": "stale" if stale else "current",
                    },
                    created_at=now,
                )
                session.add(response)
                request_row.response_message_id = response_id
                request_row.provider_response_id = result.provider_response_id
                request_row.provider = DEEPSEEK_PROVIDER
                request_row.model = result.returned_model
                request_row.state = (
                    "unknown" if budget_state != "settled" else "completed_stale" if stale else "completed"
                )
                request_row.error_code = (
                    "settlement_unknown" if budget_state != "settled" else "conversation_changed" if stale else None
                )
                request_row.updated_at = now
                if not stale:
                    conversation.revision += 1
                if conversation.stop_requested:
                    conversation.state = "stopped"
                elif conversation.human_takeover:
                    conversation.state = "taken_over"
                elif budget_state != "settled":
                    conversation.state = "needs_reconciliation"
                elif stale and conversation.state == "busy":
                    conversation.state = "active"
                elif not stale:
                    conversation.state = "active"
                conversation.updated_at = now

                if conversation.channel == "qq" and not stale and budget_state == "settled":
                    session.add(
                        ChatDraft(
                            id=str(uuid.uuid4()),
                            conversation_id=conversation.id,
                            person_id=conversation.person_id,
                            scope=conversation.scope,
                            source_revision=conversation.revision,
                            revision=1,
                            source_message_id=response_id,
                            content=result.text,
                            state="review",
                            created_at=now,
                            updated_at=now,
                        )
                    )
                await session.flush()
                return {
                    "conversation": self.serialize_conversation(conversation),
                    "message": self.serialize_message(response),
                    "inbound_message": self.serialize_message(await session.get(ChatMessage, input_message_id)),
                    "request": self.serialize_request(request_row),
                    "usage": response.token_usage_json,
                }

    async def _release_if_reserved(self, reservation_id: str) -> None:
        try:
            await self.budget.release(reservation_id)
        except Exception:
            # A dispatched reservation must remain open/unknown rather than be released.
            pass

    async def stop_conversation(self, conversation_id: str) -> dict[str, Any]:
        return await self._change_conversation_state(conversation_id, "stopped")

    async def takeover_conversation(self, conversation_id: str) -> dict[str, Any]:
        return await self._change_conversation_state(conversation_id, "taken_over")

    async def _change_conversation_state(self, conversation_id: str, state: str) -> dict[str, Any]:
        async with self.session_factory() as session:
            async with session.begin():
                row = await session.get(Conversation, conversation_id)
                if row is None:
                    raise ChatError("conversation_not_found", "会话不存在。", 404)
                if row.state in {"stopped", "taken_over"}:
                    if row.state != state:
                        raise ChatError("conversation_already_closed", "会话已停止或由人工接管。", 409)
                    return self.serialize_conversation(row)
                if state == "stopped":
                    row.stop_requested = True
                    row.stopped_at = utc_now()
                else:
                    row.human_takeover = True
                    row.taken_over_at = utc_now()
                row.state = state
                row.revision += 1
                row.updated_at = utc_now()
                await session.flush()
                return self.serialize_conversation(row)

    async def get_delivery_guard(self, conversation_id: str) -> dict[str, Any] | None:
        async with self.session_factory() as session:
            row = await session.get(Conversation, conversation_id)
            if row is None:
                return None
            return {
                "conversation_id": row.id,
                "person_id": row.person_id,
                "scope": row.scope,
                "channel": row.channel,
                "delivery_mode": row.delivery_mode,
                "revision": row.revision,
                "state": row.state,
                "stop_requested": bool(row.stop_requested),
                "human_takeover": bool(row.human_takeover),
                "account_id": row.account_id,
                "recipient_id": row.recipient_id,
                "batch_id": row.batch_id,
            }

    async def conversation_revision_matches(self, conversation_id: str, expected_revision: int) -> bool:
        if type(expected_revision) is not int or expected_revision < 0:
            return False
        async with self.session_factory() as session:
            row = await session.get(Conversation, conversation_id)
            return bool(row is not None and row.revision == expected_revision)

    async def get_request(self, local_request_id: str) -> dict[str, Any]:
        async with self.session_factory() as session:
            row = await session.get(ChatRequest, local_request_id)
            if row is None:
                raise ChatError("request_not_found", "聊天请求不存在。", 404)
            return await self._serialize_request_in_session(session, row)

    async def list_qq_drafts(self, *, state: str = "review", limit: int = 100) -> list[dict[str, Any]]:
        if state not in {"review", "stale", "approved", "sent", "cancelled"}:
            raise ChatError("invalid_draft_state", "草稿状态无效。")
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ChatError("invalid_limit", "limit必须在1到200之间。")
        async with self.session_factory() as session:
            rows = (
                await session.scalars(
                    select(ChatDraft)
                    .where(ChatDraft.state == state)
                    .order_by(ChatDraft.created_at.desc())
                    .limit(limit)
                )
            ).all()
            return [self.serialize_draft(row) for row in rows]

    @staticmethod
    def serialize_conversation(row: Conversation) -> dict[str, Any]:
        return {
            "id": row.id,
            "person_id": row.person_id,
            "scope": row.scope,
            "channel": row.channel,
            "purpose": row.purpose,
            "delivery_mode": row.delivery_mode,
            "revision": row.revision,
            "state": row.state,
            "stop_requested": bool(row.stop_requested),
            "human_takeover": bool(row.human_takeover),
            "account_id": row.account_id,
            "recipient_id": row.recipient_id,
            "batch_id": row.batch_id,
            "persona_version_id": row.persona_version_id,
            "config_fingerprint": row.config_fingerprint,
            "title": row.title,
            "created_at": _iso(row.created_at),
            "updated_at": _iso(row.updated_at),
            "stopped_at": _iso(row.stopped_at),
            "taken_over_at": _iso(row.taken_over_at),
        }

    @staticmethod
    def serialize_message(row: ChatMessage | None) -> dict[str, Any] | None:
        if row is None:
            return None
        return {
            "id": row.id,
            "conversation_id": row.conversation_id,
            "person_id": row.person_id,
            "scope": row.scope,
            "role": row.role,
            "content": row.content,
            "channel": row.channel,
            "source_event_id": row.source_event_id,
            "provider": row.provider,
            "model": row.model,
            "configured_model": row.configured_model,
            "provider_response_id": row.provider_response_id,
            "config_fingerprint": row.config_fingerprint,
            "persona_version_id": row.persona_version_id,
            "finish_reason": row.finish_reason,
            "token_usage": row.token_usage_json,
            "created_at": _iso(row.created_at),
        }

    @staticmethod
    def serialize_request(row: ChatRequest) -> dict[str, Any]:
        return {
            "local_request_id": row.local_request_id,
            "conversation_id": row.conversation_id,
            "inbound_message_id": row.inbound_message_id,
            "response_message_id": row.response_message_id,
            "reservation_id": row.reservation_id,
            "provider_response_id": row.provider_response_id,
            "config_fingerprint": row.config_fingerprint,
            "source_revision": row.source_revision,
            "persona_version_id": row.persona_version_id,
            "state": row.state,
            "error_code": row.error_code,
            "provider": row.provider,
            "model": row.model,
            "created_at": _iso(row.created_at),
            "updated_at": _iso(row.updated_at),
        }

    @staticmethod
    def serialize_draft(row: ChatDraft) -> dict[str, Any]:
        return {
            "id": row.id,
            "conversation_id": row.conversation_id,
            "person_id": row.person_id,
            "scope": row.scope,
            "source_revision": row.source_revision,
            "revision": row.revision,
            "source_message_id": row.source_message_id,
            "content": row.content,
            "state": row.state,
            "stale_reason": row.stale_reason,
            "created_at": _iso(row.created_at),
            "updated_at": _iso(row.updated_at),
        }


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    normalized = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    return normalized.isoformat()
