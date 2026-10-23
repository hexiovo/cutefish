"""Immutable persona drafts, interview feedback, and evidence-bound approval."""

from __future__ import annotations

import hashlib
import hmac
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from cutefish.core.auth import OwnerPrincipal
from cutefish.core.models import AppSetting, utc_now
from cutefish.memory.service import OWNER_PERSON_ID

from .models import (
    ChatMessage,
    ChatRequest,
    Conversation,
    InterviewSession,
    PersonaApproval,
    PersonaTrialFeedback,
    PersonaVersion,
)
from .persona_import import ConfirmedDesignBundleError, load_confirmed_design_bundle
from .service import ChatError, ChatService


FEEDBACK_ACTIONS = frozenset({"keep", "change", "reject"})
CONFIRMED_IMPORT_SETTING_PREFIX = "persona_confirmed_design_import."
CONFIRMED_IMPORT_SETTING_KEY = "persona_confirmed_design_import.{}"


class PersonaService:
    def __init__(
        self,
        session_factory,
        chat_service: ChatService,
        confirmed_design_import_root: Path | None = None,
    ):
        self.session_factory = session_factory
        self.chat_service = chat_service
        self.confirmed_design_import_root = confirmed_design_import_root

    async def confirmed_design_import_status(self) -> dict[str, Any]:
        context_capacity = {
            "target_tokens": {"owner": 131072, "contact": 32768},
            "actual_max_utf8_bytes": {"owner": 131072, "contact": 32768},
            "token_capacity_status": "partial",
        }
        if self.confirmed_design_import_root is None:
            return {
                "status": "not_configured",
                "available": False,
                "reason_code": "not_configured",
                "bundle_hash": None,
                "schema_version": None,
                "sources": [],
                "context_capacity": context_capacity,
            }
        try:
            bundle = load_confirmed_design_bundle(self.confirmed_design_import_root)
        except ConfirmedDesignBundleError as exc:
            return {
                "status": "unavailable",
                "available": False,
                "reason_code": exc.code,
                "bundle_hash": None,
                "schema_version": None,
                "sources": [],
                "context_capacity": context_capacity,
            }
        return {
            "status": "available",
            "available": True,
            "reason_code": None,
            "bundle_hash": bundle.bundle_hash,
            "schema_version": bundle.schema_version,
            "sources": [dict(source, status="verified") for source in bundle.sources],
            "context_capacity": context_capacity,
        }

    async def import_confirmed_design(self, *, bundle_hash: str) -> dict[str, Any]:
        if (
            not isinstance(bundle_hash, str)
            or len(bundle_hash) != 64
            or any(character not in "0123456789abcdef" for character in bundle_hash)
        ):
            raise ChatError("invalid_bundle_hash", "已确认设定的bundle hash无效。")
        try:
            bundle = load_confirmed_design_bundle(self.confirmed_design_import_root)
        except ConfirmedDesignBundleError as exc:
            raise ChatError("confirmed_design_unavailable", "已确认设定档案当前不可用，请检查本机配置。", 409) from exc
        if not hmac.compare_digest(bundle.bundle_hash, bundle_hash):
            raise ChatError("confirmed_design_changed", "已确认设定档案已变化，请刷新状态后重试。", 409)
        if not bundle.content.strip() or len(bundle.content) > 24000:
            raise ChatError("confirmed_design_too_large", "导入内容超过人格草案的24000字符限制。", 422)

        await self.chat_service.memory_service.ensure_owner()
        setting_key = CONFIRMED_IMPORT_SETTING_KEY.format(bundle.bundle_hash)
        try:
            async with self.session_factory() as session:
                async with session.begin():
                    existing = await session.get(AppSetting, setting_key)
                    if existing is not None:
                        return await self._confirmed_import_result(
                            session, existing.value, import_status="already_imported"
                        )
                    now = utc_now()
                    version_id = str(uuid.uuid4())
                    interview_id = str(uuid.uuid4())
                    conversation_id = str(uuid.uuid4())
                    content_hash = _sha256(bundle.content)
                    config_fingerprint = self.chat_service.configuration_fingerprint(content_hash)
                    maximum = await session.scalar(
                        select(func.max(PersonaVersion.version_number)).where(PersonaVersion.persona_key == "cutefish")
                    )
                    version = PersonaVersion(
                        id=version_id,
                        persona_key="cutefish",
                        version_number=int(maximum or 0) + 1,
                        version_label="已确认设定导入",
                        content=bundle.content,
                        content_hash=content_hash,
                        state="draft",
                        source_kind="confirmed_design",
                        source_interview_id=interview_id,
                        based_on_version_id=None,
                        config_fingerprint=config_fingerprint,
                        created_by_person_id=OWNER_PERSON_ID,
                        created_at=now,
                        state_revision=1,
                    )
                    conversation = Conversation(
                        id=conversation_id,
                        person_id=OWNER_PERSON_ID,
                        scope="calibration",
                        channel="ui",
                        purpose="persona_trial",
                        delivery_mode="ui",
                        revision=1,
                        state="active",
                        persona_version_id=None,
                        config_fingerprint=config_fingerprint,
                        title="已确认设定校准试聊",
                        created_at=now,
                        updated_at=now,
                    )
                    interview = InterviewSession(
                        id=interview_id,
                        person_id=OWNER_PERSON_ID,
                        persona_version_id=None,
                        conversation_id=conversation_id,
                        state="active",
                        title="已确认设定校准试聊",
                        created_at=now,
                    )
                    session.add(conversation)
                    await session.flush()
                    session.add(interview)
                    await session.flush()
                    session.add(version)
                    await session.flush()
                    conversation.persona_version_id = version.id
                    interview.persona_version_id = version.id
                    provenance = {
                        "schema_version": bundle.schema_version,
                        "bundle_hash": bundle.bundle_hash,
                        "persona_version_id": version.id,
                        "interview_id": interview.id,
                        "conversation_id": conversation.id,
                        "source_hashes": bundle.source_hashes,
                        "source_refs": bundle.source_refs,
                        "confirmed_refs": bundle.confirmed_refs,
                        "confirmed_constraint_refs": list(bundle.confirmed_constraint_refs),
                    }
                    session.add(AppSetting(key=setting_key, value=provenance, version=1, updated_at=now))
                    await session.flush()
                    result = {
                        "import_status": "created",
                        "source_bundle_hash": bundle.bundle_hash,
                        "sources": [dict(source, status="verified") for source in bundle.sources],
                        "version": self._version_with_provenance(version, provenance),
                        "interview": self.serialize_interview(interview),
                        "conversation": self.chat_service.serialize_conversation(conversation),
                    }
            return result
        except IntegrityError as exc:
            # A same-bundle race is idempotent; a different bundle that raced on
            # the monotonically increasing version number is a safe conflict.
            async with self.session_factory() as session:
                existing = await session.get(AppSetting, setting_key)
                if existing is not None:
                    return await self._confirmed_import_result(session, existing.value, import_status="already_imported")
            raise ChatError("confirmed_design_import_conflict", "人格版本号发生冲突，请刷新后重试。", 409) from exc

    async def _confirmed_import_result(
        self,
        session,
        provenance: Any,
        *,
        import_status: str,
    ) -> dict[str, Any]:
        if not isinstance(provenance, dict) or provenance.get("schema_version") != 1:
            raise ChatError("confirmed_design_import_record_invalid", "导入来源记录不可用。", 500)
        version = await session.get(PersonaVersion, provenance.get("persona_version_id"))
        interview = await session.get(InterviewSession, provenance.get("interview_id"))
        conversation = await session.get(Conversation, provenance.get("conversation_id"))
        if (
            version is None
            or interview is None
            or conversation is None
            or version.source_kind != "confirmed_design"
            or version.source_interview_id != interview.id
            or interview.persona_version_id != version.id
            or interview.conversation_id != conversation.id
            or conversation.persona_version_id != version.id
            or conversation.scope != "calibration"
            or conversation.purpose != "persona_trial"
        ):
            raise ChatError("confirmed_design_import_record_invalid", "导入来源记录不可用。", 500)
        sources = [
            {
                "basename": basename,
                "sha256": sha256,
                "source_ref": provenance.get("source_refs", {}).get(basename),
                "status": "verified",
            }
            for basename, sha256 in provenance.get("source_hashes", {}).items()
        ]
        return {
            "import_status": import_status,
            "source_bundle_hash": provenance.get("bundle_hash"),
            "sources": sources,
            "version": self._version_with_provenance(version, provenance),
            "interview": self.serialize_interview(interview),
            "conversation": self.chat_service.serialize_conversation(conversation),
        }

    @staticmethod
    def _version_with_provenance(version: PersonaVersion, provenance: dict[str, Any]) -> dict[str, Any]:
        data = PersonaService.serialize_version(version)
        data.update(
            {
                "source_bundle_hash": provenance.get("bundle_hash"),
                "source_hashes": dict(provenance.get("source_hashes", {})),
                "source_refs": dict(provenance.get("source_refs", {})),
                "import_schema_version": provenance.get("schema_version"),
                "contains_confirmed_constraint_excerpts": bool(provenance.get("confirmed_constraint_refs", [])),
                "confirmed_constraint_refs": list(provenance.get("confirmed_constraint_refs", [])),
            }
        )
        return data

    async def _version_import_provenance(self, session, version_ids: set[str]) -> dict[str, dict[str, Any]]:
        if not version_ids:
            return {}
        prefix = CONFIRMED_IMPORT_SETTING_PREFIX.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        rows = (
            await session.scalars(
                select(AppSetting).where(AppSetting.key.like(prefix + "%", escape="\\"))
            )
        ).all()
        return {
            record.value["persona_version_id"]: record.value
            for record in rows
            if record.key.startswith(CONFIRMED_IMPORT_SETTING_PREFIX)
            and isinstance(record.value, dict)
            and record.value.get("persona_version_id") in version_ids
        }

    async def create_interview(self, *, title: str | None = None) -> dict[str, Any]:
        conversation = await self.chat_service.create_conversation(
            person_id=OWNER_PERSON_ID,
            scope="calibration",
            purpose="persona_interview",
            channel="ui",
            delivery_mode="ui",
            title=title or "人格访谈",
        )
        async with self.session_factory() as session:
            interview = await session.scalar(
                select(InterviewSession).where(InterviewSession.conversation_id == conversation["id"])
            )
            if interview is None:
                raise ChatError("interview_create_failed", "人格访谈记录未能保存。", 500)
            return {"interview": self.serialize_interview(interview), "conversation": conversation}

    async def list_interviews(self, *, limit: int = 50) -> list[dict[str, Any]]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ChatError("invalid_limit", "limit必须在1到100之间。")
        async with self.session_factory() as session:
            rows = (
                await session.scalars(
                    select(InterviewSession)
                    .where(InterviewSession.person_id == OWNER_PERSON_ID)
                    .order_by(InterviewSession.created_at.desc())
                    .limit(limit)
                )
            ).all()
            return [self.serialize_interview(row) for row in rows]

    async def get_interview(self, interview_id: str) -> dict[str, Any]:
        async with self.session_factory() as session:
            interview = await session.get(InterviewSession, interview_id)
            if interview is None or interview.person_id != OWNER_PERSON_ID:
                raise ChatError("interview_not_found", "人格访谈不存在。", 404)
            conversation = await session.get(Conversation, interview.conversation_id)
            if conversation is None:
                raise ChatError("interview_corrupt", "人格访谈引用的会话不存在。", 500)
            conversation_data = self.chat_service.serialize_conversation(conversation)
            interview_data = self.serialize_interview(interview)
        messages = await self.chat_service.list_messages(interview_data["conversation_id"], limit=200)
        return {"interview": interview_data, "conversation": conversation_data, "messages": messages}

    async def create_draft(
        self,
        interview_id: str,
        *,
        version_label: str,
        content: str,
        based_on_version_id: str | None = None,
    ) -> dict[str, Any]:
        label = version_label.strip()
        body = content.strip()
        if not label or len(label) > 128:
            raise ChatError("invalid_version_label", "人格版本名称不能为空且最多128个字符。")
        if not body or len(body) > 24000:
            raise ChatError("invalid_persona_content", "人格草案不能为空且最多24000个字符。")

        version_id = str(uuid.uuid4())
        now = utc_now()
        async with self.session_factory() as session:
            try:
                async with session.begin():
                    interview = await session.get(InterviewSession, interview_id)
                    if interview is None or interview.person_id != OWNER_PERSON_ID:
                        raise ChatError("interview_not_found", "人格访谈不存在。", 404)
                    if interview.state != "active":
                        raise ChatError("interview_closed", "该访谈已结束；请新建访谈来修改人格。", 409)
                    conversation = await session.get(Conversation, interview.conversation_id)
                    if (
                        conversation is None
                        or conversation.state != "active"
                        or conversation.stop_requested
                        or conversation.human_takeover
                    ):
                        raise ChatError("interview_unavailable", "访谈会话忙碌或不可用，请稍后重试。", 409)
                    if conversation.channel != "ui" or conversation.scope != "calibration":
                        raise ChatError("interview_scope_mismatch", "人格草案只能来自管理员本机访谈。", 403)

                    based_on = None
                    if based_on_version_id:
                        based_on = await session.get(PersonaVersion, based_on_version_id)
                        if based_on is None or based_on.approved_at is None or based_on.state not in {"approved", "superseded"}:
                            raise ChatError("base_version_unavailable", "基础版本不存在或未批准。", 409)
                        if based_on.persona_key != "cutefish":
                            raise ChatError("base_version_mismatch", "基础版本与当前角色不匹配。", 409)

                    maximum = await session.scalar(
                        select(func.max(PersonaVersion.version_number)).where(PersonaVersion.persona_key == "cutefish")
                    )
                    number = int(maximum or 0) + 1
                    version = PersonaVersion(
                        id=version_id,
                        persona_key="cutefish",
                        version_number=number,
                        version_label=label,
                        content=body,
                        content_hash=_sha256(body),
                        state="draft",
                        source_kind="owner_interview_draft",
                        source_interview_id=interview.id,
                        based_on_version_id=based_on.id if based_on else None,
                        config_fingerprint=self.chat_service.configuration_fingerprint(_sha256(body)),
                        created_by_person_id=OWNER_PERSON_ID,
                        created_at=now,
                        state_revision=1,
                    )
                    if interview.persona_version_id and interview.persona_version_id != version.id:
                        previous_draft = await session.get(PersonaVersion, interview.persona_version_id)
                        if previous_draft is not None and previous_draft.state == "draft":
                            previous_draft.state = "superseded"
                            previous_draft.state_revision += 1
                    session.add(version)
                    # The interview row points back to this version, while the new
                    # version already points to the interview. Insert the version
                    # first so SQLite's immediate FK check can accept the update.
                    await session.flush()
                    interview.persona_version_id = version.id
                    interview.title = interview.title or "人格访谈"
                    conversation.persona_version_id = version.id
                    conversation.purpose = "persona_trial"
                    conversation.config_fingerprint = version.config_fingerprint
                    conversation.revision += 1
                    conversation.updated_at = now
                    await session.flush()
                    return {
                        "version": self.serialize_version(version),
                        "interview": self.serialize_interview(interview),
                        "conversation": self.chat_service.serialize_conversation(conversation),
                    }
            except IntegrityError as exc:
                raise ChatError("version_create_conflict", "人格版本号发生冲突，请刷新后重试。", 409) from exc

    async def add_feedback(
        self,
        interview_id: str,
        *,
        inbound_message_id: str,
        reply_message_id: str,
        action: str,
        change_points: str | None = None,
    ) -> dict[str, Any]:
        if action not in FEEDBACK_ACTIONS:
            raise ChatError("invalid_feedback_action", "反馈动作只能是keep、change或reject。")
        if change_points is not None and len(change_points) > 4000:
            raise ChatError("invalid_change_points", "调整意见最多4000个字符。")
        async with self.session_factory() as session:
            async with session.begin():
                interview = await session.get(InterviewSession, interview_id)
                if interview is None or interview.person_id != OWNER_PERSON_ID:
                    raise ChatError("interview_not_found", "人格访谈不存在。", 404)
                version_id = interview.persona_version_id
                if not version_id or interview.state != "active":
                    raise ChatError("persona_trial_required", "请先保存人格草案，并完成该版本的真实试聊。", 409)
                version = await session.get(PersonaVersion, version_id)
                if version is None or version.state != "draft":
                    raise ChatError("persona_draft_unavailable", "当前访谈没有可反馈的人格草案。", 409)
                inbound, reply = await self._validated_trial_pair(
                    session,
                    interview=interview,
                    version=version,
                    inbound_message_id=inbound_message_id,
                    reply_message_id=reply_message_id,
                )
                row = PersonaTrialFeedback(
                    id=str(uuid.uuid4()),
                    persona_version_id=version.id,
                    interview_session_id=interview.id,
                    inbound_message_id=inbound.id,
                    reply_message_id=reply.id,
                    action=action,
                    change_points=change_points.strip() if change_points else None,
                    created_at=utc_now(),
                )
                session.add(row)
                await session.flush()
                return self.serialize_feedback(row)

    async def list_versions(self, *, limit: int = 100) -> list[dict[str, Any]]:
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ChatError("invalid_limit", "limit必须在1到200之间。")
        async with self.session_factory() as session:
            rows = (
                await session.scalars(
                    select(PersonaVersion)
                    .where(PersonaVersion.persona_key == "cutefish")
                    .order_by(PersonaVersion.version_number.desc())
                    .limit(limit)
                )
            ).all()
            provenance = await self._version_import_provenance(session, {row.id for row in rows})
            return [
                self._version_with_provenance(row, provenance[row.id]) if row.id in provenance else self.serialize_version(row)
                for row in rows
            ]

    async def get_version(self, version_id: str) -> dict[str, Any]:
        async with self.session_factory() as session:
            row = await session.get(PersonaVersion, version_id)
            if row is None or row.persona_key != "cutefish":
                raise ChatError("persona_version_not_found", "人格版本不存在。", 404)
            provenance = await self._version_import_provenance(session, {row.id})
            return (
                self._version_with_provenance(row, provenance[row.id])
                if row.id in provenance
                else self.serialize_version(row)
            )

    async def approve(
        self,
        version_id: str,
        *,
        principal: OwnerPrincipal,
        interview_session_id: str,
        inbound_message_id: str,
        reply_message_id: str,
        expected_state_revision: int,
        version_hash: str,
    ) -> dict[str, Any]:
        if principal.role != "owner":
            raise ChatError("owner_required", "只有管理员可以批准人格版本。", 403)
        if type(expected_state_revision) is not int or expected_state_revision < 1:
            raise ChatError("invalid_state_revision", "人格版本状态版本号无效。")
        if len(version_hash) != 64:
            raise ChatError("invalid_version_hash", "人格版本哈希无效。")

        async with self.session_factory() as session:
            async with session.begin():
                version = await session.get(PersonaVersion, version_id)
                interview = await session.get(InterviewSession, interview_session_id)
                if version is None or version.persona_key != "cutefish":
                    raise ChatError("persona_version_not_found", "人格版本不存在。", 404)
                if interview is None or interview.person_id != OWNER_PERSON_ID:
                    raise ChatError("interview_not_found", "人格访谈不存在。", 404)
                if version.state != "draft" or version.source_interview_id != interview.id:
                    raise ChatError("persona_version_not_approvable", "只有本次访谈创建的草案可以批准。", 409)
                if version.state_revision != expected_state_revision:
                    raise ChatError("version_conflict", "人格版本状态已变化，请刷新后再批准。", 409)
                current_hash = _sha256(version.content)
                if current_hash != version.content_hash or version_hash != current_hash:
                    raise ChatError("version_hash_mismatch", "人格版本内容已变化或哈希不匹配。", 409)
                if interview.state != "active" or interview.persona_version_id != version.id:
                    raise ChatError("interview_version_mismatch", "访谈当前绑定的草案版本已变化。", 409)
                if version.config_fingerprint != self.chat_service.configuration_fingerprint(version.content_hash):
                    raise ChatError("persona_config_mismatch", "模型配置已变化，请在当前配置下重新试聊。", 409)
                inbound, reply = await self._validated_trial_pair(
                    session,
                    interview=interview,
                    version=version,
                    inbound_message_id=inbound_message_id,
                    reply_message_id=reply_message_id,
                )
                feedback = await session.scalar(
                    select(PersonaTrialFeedback).where(
                        PersonaTrialFeedback.persona_version_id == version.id,
                        PersonaTrialFeedback.interview_session_id == interview.id,
                        PersonaTrialFeedback.inbound_message_id == inbound.id,
                        PersonaTrialFeedback.reply_message_id == reply.id,
                        PersonaTrialFeedback.action == "keep",
                    )
                )
                if feedback is None:
                    raise ChatError("approval_feedback_required", "批准前需要为同一轮真实试聊记录keep反馈。", 409)

                previous_approved = (
                    await session.scalars(
                        select(PersonaVersion).where(
                            PersonaVersion.persona_key == version.persona_key,
                            PersonaVersion.state == "approved",
                        )
                    )
                ).all()
                for old in previous_approved:
                    old.state = "superseded"
                    old.state_revision += 1

                now = utc_now()
                version.state = "approved"
                version.state_revision += 1
                version.approved_at = now
                interview.state = "complete"
                interview.completed_at = now
                approval = PersonaApproval(
                    id=str(uuid.uuid4()),
                    persona_version_id=version.id,
                    interview_session_id=interview.id,
                    inbound_message_id=inbound.id,
                    reply_message_id=reply.id,
                    actor_person_id=OWNER_PERSON_ID,
                    version_hash=current_hash,
                    config_fingerprint=version.config_fingerprint,
                    approved_at=now,
                )
                session.add(approval)
                await session.flush()
                provenance = await self._version_import_provenance(session, {version.id})
                version_data = (
                    self._version_with_provenance(version, provenance[version.id])
                    if version.id in provenance
                    else self.serialize_version(version)
                )
                return {
                    "version": version_data,
                    "approval": self.serialize_approval(approval),
                    "evidence": {
                        "interview_session_id": interview.id,
                        "inbound_message": self.chat_service.serialize_message(inbound),
                        "reply_message": self.chat_service.serialize_message(reply),
                        "feedback": self.serialize_feedback(feedback),
                    },
                }

    async def _validated_trial_pair(
        self,
        session,
        *,
        interview: InterviewSession,
        version: PersonaVersion,
        inbound_message_id: str,
        reply_message_id: str,
    ) -> tuple[ChatMessage, ChatMessage]:
        inbound = await session.get(ChatMessage, inbound_message_id)
        reply = await session.get(ChatMessage, reply_message_id)
        if (
            inbound is None
            or reply is None
            or inbound.role != "user"
            or reply.role != "assistant"
            or inbound.conversation_id != interview.conversation_id
            or reply.conversation_id != interview.conversation_id
            or inbound.person_id != OWNER_PERSON_ID
            or reply.person_id != OWNER_PERSON_ID
        ):
            raise ChatError("trial_evidence_mismatch", "输入和回复必须来自这次管理员人格访谈。", 409)
        if (
            inbound.persona_version_id != version.id
            or reply.persona_version_id != version.id
            or inbound.config_fingerprint != version.config_fingerprint
            or reply.config_fingerprint != version.config_fingerprint
            or reply.provider != "deepseek"
            or not reply.provider_response_id
            or not isinstance(reply.token_usage_json, dict)
            or reply.token_usage_json.get("budget_state") != "settled"
        ):
            raise ChatError("trial_evidence_not_verified", "批准需要同一人格版本下真实DeepSeek输入/回复与已结算用量证据。", 409)
        request_row = await session.scalar(
            select(ChatRequest).where(
                ChatRequest.inbound_message_id == inbound.id,
                ChatRequest.response_message_id == reply.id,
            )
        )
        if (
            request_row is None
            or request_row.state != "completed"
            or request_row.provider != "deepseek"
            or request_row.provider_response_id != reply.provider_response_id
            or request_row.config_fingerprint != version.config_fingerprint
            or request_row.persona_version_id != version.id
        ):
            raise ChatError("trial_request_evidence_mismatch", "该消息对缺少已完成的同版模型请求证据。", 409)
        return inbound, reply

    @staticmethod
    def serialize_interview(row: InterviewSession) -> dict[str, Any]:
        return {
            "id": row.id,
            "person_id": row.person_id,
            "persona_version_id": row.persona_version_id,
            "conversation_id": row.conversation_id,
            "state": row.state,
            "title": row.title,
            "created_at": _iso(row.created_at),
            "completed_at": _iso(row.completed_at),
        }

    @staticmethod
    def serialize_version(row: PersonaVersion) -> dict[str, Any]:
        return {
            "id": row.id,
            "persona_key": row.persona_key,
            "version_number": row.version_number,
            "version_label": row.version_label,
            "content": row.content,
            "content_hash": row.content_hash,
            "state": row.state,
            "source_kind": row.source_kind,
            "source_request_id": row.source_request_id,
            "source_interview_id": row.source_interview_id,
            "based_on_version_id": row.based_on_version_id,
            "config_fingerprint": row.config_fingerprint,
            "created_by_person_id": row.created_by_person_id,
            "created_at": _iso(row.created_at),
            "state_revision": row.state_revision,
            "approved_at": _iso(row.approved_at),
        }

    @staticmethod
    def serialize_feedback(row: PersonaTrialFeedback) -> dict[str, Any]:
        return {
            "id": row.id,
            "persona_version_id": row.persona_version_id,
            "interview_session_id": row.interview_session_id,
            "inbound_message_id": row.inbound_message_id,
            "reply_message_id": row.reply_message_id,
            "action": row.action,
            "change_points": row.change_points,
            "created_at": _iso(row.created_at),
        }

    @staticmethod
    def serialize_approval(row: PersonaApproval) -> dict[str, Any]:
        return {
            "id": row.id,
            "persona_version_id": row.persona_version_id,
            "interview_session_id": row.interview_session_id,
            "inbound_message_id": row.inbound_message_id,
            "reply_message_id": row.reply_message_id,
            "actor_person_id": row.actor_person_id,
            "version_hash": row.version_hash,
            "config_fingerprint": row.config_fingerprint,
            "approved_at": _iso(row.approved_at),
        }


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    normalized = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    return normalized.isoformat()
