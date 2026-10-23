"""Owner-controlled local voice profiles and hash-verified generated WAV assets."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import math
import os
import uuid
import wave
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import func, select

from cutefish.chat.models import ChatMessage, Conversation
from cutefish.core.contracts import VoiceOutboxBinding
from cutefish.core.models import TrialBatch, utc_now
from cutefish.core.paths import HostPathResolver, PathPolicyError
from cutefish.memory.service import OWNER_PERSON_ID
from cutefish.qq.sender import ControlledRecordMedia
from cutefish.qq.service import QqError
from cutefish.local_audio.errors import LocalAudioError

from .models import ManagedMediaRecord, VoiceApproval, VoiceProfileState, VoiceProfileVersion
from .tts import SpeechError, TtsCapabilityProbe, _validate_wave
from .voice_candidates import VOICE_ID_PREFIX, VoiceCandidateService


VOICE_ENGINE_ID = "windows.system_speech"
QWEN_ENGINE_ID = "qwen3.base"
VOICE_PROFILE_ID = "local-sapi"
VOICE_ARTIFACT_ROOT = "voice-artifacts"
VOICE_ARTIFACT_PREFIX = f"{VOICE_ARTIFACT_ROOT}/"
MAX_VOICE_ARTIFACT_BYTES = 8 * 1024 * 1024
MAX_REPLY_CHARS = 2_000
MAX_REPLY_SECONDS = 120
_CAPABILITIES = {
    "supports_custom_voice": False,
    "supports_instruction_expression": False,
    "supports_semantic_emphasis": False,
    "supports_pause_control": False,
}


class VoiceError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 422):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


class VoiceService:
    """Manage explicit profile approvals and controlled voice artifacts."""

    def __init__(
        self,
        session_factory,
        paths: HostPathResolver,
        tts: TtsCapabilityProbe,
        *,
        deployment_profile=None,
        qq_service=None,
        voice_candidates: VoiceCandidateService | None = None,
        qwen_tts_runtime=None,
        local_audio_lane=None,
    ):
        self.session_factory = session_factory
        self.paths = paths
        self.tts = tts
        self.get_deployment_profile = deployment_profile or (lambda: "local_trial")
        self.qq_service = qq_service
        self.voice_candidates = voice_candidates
        self.qwen_tts_runtime = qwen_tts_runtime
        self.local_audio_lane = local_audio_lane

    async def status(self) -> dict[str, Any]:
        async with self.session_factory() as session:
            state = await session.get(VoiceProfileState, "default")
            profile = None
            approval = None
            audition = None
            if state is not None and state.active_profile_id and state.active_profile_version:
                profile = await session.get(
                    VoiceProfileVersion, (state.active_profile_id, state.active_profile_version)
                )
            if state is not None and state.active_approval_id:
                approval = await session.get(VoiceApproval, state.active_approval_id)
                if approval is not None:
                    audition = await session.get(ManagedMediaRecord, approval.preview_artifact_id)

        revision = int(state.revision) if state is not None else 1
        runtime_enabled = bool(state.runtime_enabled) if state is not None else False
        voice_outbox_enabled = bool(state.voice_outbox_enabled) if state is not None else False
        approval_is_current = bool(
            state is not None
            and profile is not None
            and approval is not None
            and approval.profile_id == profile.profile_id
            and approval.profile_version == profile.version
            and approval.settings_hash == profile.settings_hash
            and profile.settings_hash == _profile_settings_hash(
                profile.engine_id,
                profile.voice_id,
                profile.voice_name,
                profile.culture,
                profile.engine_config_json,
            )
            and audition is not None
            and audition.asset_class == "generated_voice"
            and audition.artifact_purpose == "approval_preview"
            and audition.artifact_state == "ready"
            and audition.profile_id == profile.profile_id
            and audition.profile_version == profile.version
            and audition.profile_settings_hash == profile.settings_hash
            and audition.sha256 == approval.preview_sha256
        )
        if approval_is_current:
            try:
                self._read_asset_sync(audition)
            except VoiceError:
                approval_is_current = False
        deployment_profile = str(self.get_deployment_profile())
        if profile is None:
            readiness = "needs_profile"
        elif not approval_is_current:
            readiness = "needs_approval"
        elif not runtime_enabled:
            readiness = "runtime_disabled"
        elif not voice_outbox_enabled:
            readiness = "voice_outbox_disabled"
        elif deployment_profile != "local_trial":
            readiness = "target_staging"
        else:
            readiness = "ready"

        profile_data = None
        if profile is not None:
            profile_data = {
                "profile_id": profile.profile_id,
                "profile_version": profile.version,
                "voice_version": _voice_version(profile.profile_id, profile.version),
                "engine_id": profile.engine_id,
                "voice_id": profile.voice_id,
                "voice_name": profile.voice_name,
                "culture": profile.culture,
                "settings_hash": profile.settings_hash,
                "capability": "reference_conditioned_clone"
                if profile.engine_id == QWEN_ENGINE_ID
                else "plain_reading",
                "engine_config": _safe_engine_config(profile.engine_config_json),
            }
        approval_data = None
        if approval_is_current and approval is not None:
            approval_data = {
                "approval_id": approval.id,
                "preview_artifact_id": approval.preview_artifact_id,
                "profile_id": approval.profile_id,
                "profile_version": approval.profile_version,
                "approved_at": _iso(approval.approved_at),
            }
        return {
            "revision": revision,
            "source_mode": (
                "qwen_voice_design"
                if profile is not None and profile.engine_id == QWEN_ENGINE_ID
                else "windows_sapi"
            ),
            "deployment_profile": deployment_profile,
            "production_enabled": False,
            "runtime_enabled": runtime_enabled,
            "voice_outbox_enabled": voice_outbox_enabled,
            "runtime_available": None,
            "readiness": readiness,
            "profile": profile_data,
            "approval": approval_data,
            "capabilities": dict(_CAPABILITIES),
        }

    async def set_profile(self, *, expected_revision: int, voice_id: str, actor_id: str) -> dict[str, Any]:
        engine_config_json = None
        if isinstance(voice_id, str) and voice_id.startswith(VOICE_ID_PREFIX):
            if self.voice_candidates is None:
                raise VoiceError("qwen_runtime_unavailable", "本机声线候选服务当前不可用。", 409)
            try:
                binding = await self.voice_candidates.profile_binding(voice_id)
            except SpeechError as exc:
                raise VoiceError(exc.code, exc.message, exc.status_code) from exc
            engine_id = QWEN_ENGINE_ID
            voice_name = f"Qwen VoiceDesign {binding['candidate_id'][:8]}"
            culture = "zh-CN"
            engine_config_json = _canonical_json(binding)
        else:
            voices = self.tts.inspect().get("voices", [])
            voice = next(
                (item for item in voices if isinstance(item, dict) and item.get("voice_id") == voice_id),
                None,
            )
            if voice is None:
                raise VoiceError("voice_unavailable", "所选声线当前不可用；请先刷新本机声线列表。", 409)
            voice_name, culture = voice.get("name"), voice.get("culture")
            if not all(isinstance(value, str) and value.strip() for value in (voice_name, culture)):
                raise VoiceError("voice_unavailable", "所选声线资料不完整，请刷新本机声线列表。", 409)
            engine_id = VOICE_ENGINE_ID
            voice_name = voice_name.strip()
            culture = culture.strip()
        settings_hash = _profile_settings_hash(
            engine_id, voice_id, voice_name, culture, engine_config_json
        )
        now = utc_now()
        async with self.session_factory() as session:
            async with session.begin():
                state = await session.get(VoiceProfileState, "default")
                current_revision = int(state.revision) if state is not None else 1
                if expected_revision != current_revision:
                    raise VoiceError("voice_revision_conflict", "声线配置已变化，请刷新后重试。", 409)
                latest_version = await session.scalar(
                    select(func.max(VoiceProfileVersion.version)).where(
                        VoiceProfileVersion.profile_id == VOICE_PROFILE_ID
                    )
                )
                profile_version = int(latest_version or 0) + 1
                session.add(
                    VoiceProfileVersion(
                        profile_id=VOICE_PROFILE_ID,
                        version=profile_version,
                        engine_id=engine_id,
                        voice_id=voice_id,
                        voice_name=voice_name,
                        culture=culture,
                        engine_config_json=engine_config_json,
                        settings_hash=settings_hash,
                        created_by=actor_id,
                        created_at=now,
                    )
                )
                if state is None:
                    state = VoiceProfileState(
                        id="default",
                        revision=current_revision + 1,
                        active_profile_id=VOICE_PROFILE_ID,
                        active_profile_version=profile_version,
                        active_approval_id=None,
                        runtime_enabled=False,
                        voice_outbox_enabled=False,
                        updated_by=actor_id,
                        updated_at=now,
                    )
                    session.add(state)
                else:
                    state.revision += 1
                    state.active_profile_id = VOICE_PROFILE_ID
                    state.active_profile_version = profile_version
                    state.active_approval_id = None
                    state.runtime_enabled = False
                    state.voice_outbox_enabled = False
                    state.updated_by = actor_id
                    state.updated_at = now
                await session.flush()
        return await self.status()

    async def set_settings(
        self,
        *,
        expected_revision: int,
        runtime_enabled: bool,
        voice_outbox_enabled: bool,
        actor_id: str,
    ) -> dict[str, Any]:
        if type(runtime_enabled) is not bool or type(voice_outbox_enabled) is not bool:
            raise VoiceError("invalid_voice_settings", "语音开关必须使用布尔值。")
        if voice_outbox_enabled and not runtime_enabled:
            raise VoiceError("voice_runtime_required", "启用本次语音外发前，必须同时启用语音运行时。", 409)
        now = utc_now()
        async with self.session_factory() as session:
            async with session.begin():
                state = await session.get(VoiceProfileState, "default")
                current_revision = int(state.revision) if state is not None else 1
                if expected_revision != current_revision:
                    raise VoiceError("voice_revision_conflict", "声线配置已变化，请刷新后重试。", 409)
                if voice_outbox_enabled:
                    if state is None or not state.active_profile_id or not state.active_profile_version:
                        raise VoiceError("voice_profile_required", "尚未选择本机声线。", 409)
                    approval = (
                        await session.get(VoiceApproval, state.active_approval_id)
                        if state.active_approval_id
                        else None
                    )
                    profile = await session.get(
                        VoiceProfileVersion, (state.active_profile_id, state.active_profile_version)
                    )
                    if (
                        approval is None
                        or profile is None
                    ):
                        raise VoiceError("voice_approval_required", "当前声线版本尚未通过试听批准。", 409)
                    await self._validate_approval_in_session(session, profile, approval)
                if state is None:
                    state = VoiceProfileState(
                        id="default",
                        revision=current_revision + 1,
                        runtime_enabled=runtime_enabled,
                        voice_outbox_enabled=voice_outbox_enabled,
                        updated_by=actor_id,
                        updated_at=now,
                    )
                    session.add(state)
                else:
                    state.revision += 1
                    state.runtime_enabled = runtime_enabled
                    state.voice_outbox_enabled = voice_outbox_enabled
                    state.updated_by = actor_id
                    state.updated_at = now
                await session.flush()
        return await self.status()

    async def create_profile_preview(
        self, *, expected_profile_version: int, text: str, actor_id: str
    ) -> dict[str, Any]:
        content = text.strip()
        if not content or len(content) > 240:
            raise VoiceError("invalid_voice_preview_text", "试听文本必须为1到240个字符。")
        snapshot = await self._profile_snapshot(expected_profile_version)
        if snapshot["engine_id"] == QWEN_ENGINE_ID:
            audio = await self._synthesize_qwen(
                content, snapshot, max_text_chars=240
            )
        else:
            try:
                audio, _voice = await asyncio.to_thread(
                    self.tts.synthesize_preview, content, snapshot["voice_id"]
                )
            except SpeechError as exc:
                raise VoiceError(exc.code, exc.message, exc.status_code) from exc
        duration_ms = self._validate_audio(audio, max_seconds=MAX_REPLY_SECONDS)
        artifact_id = str(uuid.uuid4())
        row = self._new_artifact(
            artifact_id=artifact_id,
            profile_id=snapshot["profile_id"],
            profile_version=snapshot["profile_version"],
            profile_settings_hash=snapshot["settings_hash"],
            approval_ref=None,
            purpose="approval_preview",
            state="ready",
            sha256=hashlib.sha256(audio).hexdigest(),
            duration_ms=duration_ms,
            byte_length=len(audio),
            actor_id=actor_id,
            source_refs=[{"kind": "owner_supplied", "id": "voice-profile-audition"}],
        )
        if not await self._is_profile_current(snapshot):
            row.artifact_state = "cancelled"
            await self._insert_artifact(row)
            raise VoiceError("voice_profile_changed", "试听期间声线版本已变化，产物已作废，请刷新后重试。", 409)
        path = self._write_audio(row, audio)
        try:
            if not await self._is_profile_current(snapshot):
                row.artifact_state = "cancelled"
                await self._insert_artifact(row)
                path.unlink(missing_ok=True)
                raise VoiceError("voice_profile_changed", "试听期间声线版本已变化，产物已作废，请刷新后重试。", 409)
            await self._insert_artifact(row)
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return self._artifact_dto(row)

    async def approve_profile_preview(
        self,
        *,
        expected_profile_version: int,
        preview_artifact_id: str,
        actor_id: str,
    ) -> dict[str, Any]:
        now = utc_now()
        async with self.session_factory() as session:
            async with session.begin():
                state = await session.get(VoiceProfileState, "default")
                if (
                    state is None
                    or state.active_profile_id != VOICE_PROFILE_ID
                    or state.active_profile_version != expected_profile_version
                ):
                    raise VoiceError("voice_profile_changed", "声线版本已变化，请刷新后试听当前版本。", 409)
                profile = await session.get(VoiceProfileVersion, (VOICE_PROFILE_ID, expected_profile_version))
                artifact = await session.get(ManagedMediaRecord, preview_artifact_id)
                if profile is None or artifact is None:
                    raise VoiceError("voice_preview_not_found", "试听产物或声线版本不存在。", 404)
                if (
                    artifact.asset_class != "generated_voice"
                    or artifact.artifact_purpose != "approval_preview"
                    or artifact.artifact_state != "ready"
                    or artifact.profile_id != profile.profile_id
                    or artifact.profile_version != profile.version
                    or artifact.profile_settings_hash != profile.settings_hash
                    or profile.settings_hash != _profile_settings_hash(
                        profile.engine_id,
                        profile.voice_id,
                        profile.voice_name,
                        profile.culture,
                        profile.engine_config_json,
                    )
                ):
                    raise VoiceError("voice_preview_unavailable", "试听产物与当前声线版本不匹配。", 409)
                raw = self._read_asset_sync(artifact)
                duration_ms = self._validate_audio(raw, max_seconds=MAX_REPLY_SECONDS)
                if duration_ms != artifact.duration_ms:
                    raise VoiceError("voice_preview_integrity_error", "试听产物时长与登记记录不一致。", 409)
                approval = await session.scalar(
                    select(VoiceApproval).where(VoiceApproval.preview_artifact_id == artifact.id)
                )
                if approval is None:
                    approval = VoiceApproval(
                        id=str(uuid.uuid4()),
                        profile_id=profile.profile_id,
                        profile_version=profile.version,
                        preview_artifact_id=artifact.id,
                        preview_sha256=artifact.sha256,
                        settings_hash=profile.settings_hash,
                        approved_by=actor_id,
                        approved_at=now,
                    )
                    session.add(approval)
                elif (
                    approval.profile_id != profile.profile_id
                    or approval.profile_version != profile.version
                    or approval.settings_hash != profile.settings_hash
                    or approval.preview_sha256 != artifact.sha256
                ):
                    raise VoiceError("voice_approval_conflict", "该试听记录绑定的批准信息不一致。", 409)
                state.active_approval_id = approval.id
                state.revision += 1
                state.updated_by = actor_id
                state.updated_at = now
                await session.flush()
                approval_data = {
                    "approval_id": approval.id,
                    "profile_id": profile.profile_id,
                    "profile_version": profile.version,
                    "preview_artifact_id": artifact.id,
                    "approved_at": _iso(approval.approved_at),
                }
        return approval_data

    async def prepare_voice_reply(
        self,
        *,
        conversation_id: str,
        expected_conversation_revision: int,
        source_message_id: str,
        batch_id: str,
        expected_batch_version: int,
        actor_id: str,
    ) -> dict[str, Any]:
        self._require_local_trial("生成 QQ 语音回复")
        snapshot = await self._reply_snapshot(
            conversation_id=conversation_id,
            expected_revision=expected_conversation_revision,
            source_message_id=source_message_id,
            batch_id=batch_id,
            expected_batch_version=expected_batch_version,
        )
        await self._validate_qq_guard(snapshot["guard"])
        if snapshot["engine_id"] == QWEN_ENGINE_ID:
            audio = await self._synthesize_qwen(
                snapshot["content"], snapshot, max_text_chars=MAX_REPLY_CHARS
            )
        else:
            try:
                audio, _voice = await asyncio.to_thread(
                    self.tts.synthesize_preview,
                    snapshot["content"],
                    snapshot["voice_id"],
                    max_text_chars=MAX_REPLY_CHARS,
                )
            except SpeechError as exc:
                raise VoiceError(exc.code, exc.message, exc.status_code) from exc
        duration_ms = self._validate_audio(audio, max_seconds=MAX_REPLY_SECONDS)
        artifact_id = str(uuid.uuid4())
        row = self._new_artifact(
            artifact_id=artifact_id,
            profile_id=snapshot["profile_id"],
            profile_version=snapshot["profile_version"],
            profile_settings_hash=snapshot["settings_hash"],
            approval_ref=snapshot["approval_id"],
            purpose="reply",
            state="ready",
            sha256=hashlib.sha256(audio).hexdigest(),
            duration_ms=duration_ms,
            byte_length=len(audio),
            actor_id=actor_id,
            source_refs=[
                {
                    "kind": "chat_event",
                    "id": snapshot["source_message_id"],
                    "person_id": OWNER_PERSON_ID,
                    "scope": "production",
                }
            ],
            source_person_id=OWNER_PERSON_ID,
            conversation_id=snapshot["conversation_id"],
            account_id=snapshot["account_id"],
            recipient_id=snapshot["recipient_id"],
            batch_id=snapshot["batch_id"],
            batch_version=snapshot["batch_version"],
            source_batch_id=snapshot["source_batch_id"],
            source_message_id=snapshot["source_message_id"],
            source_revision=snapshot["source_revision"],
        )
        current = await self._reply_snapshot(
            conversation_id=conversation_id,
            expected_revision=expected_conversation_revision,
            source_message_id=source_message_id,
            batch_id=batch_id,
            expected_batch_version=expected_batch_version,
        )
        if not self._same_reply_snapshot(snapshot, current):
            row.artifact_state = "cancelled"
            await self._insert_artifact(row)
            raise VoiceError("voice_reply_stale", "合成期间会话、批准或批次已变化，语音产物已取消。", 409)
        await self._validate_qq_guard(current["guard"])

        path = self._write_audio(row, audio)
        try:
            async with self.session_factory() as session:
                async with session.begin():
                    latest = await self._reply_snapshot_in_session(
                        session,
                        conversation_id=conversation_id,
                        expected_revision=expected_conversation_revision,
                        source_message_id=source_message_id,
                        batch_id=batch_id,
                        expected_batch_version=expected_batch_version,
                    )
                    if not self._same_reply_snapshot(snapshot, latest):
                        row.artifact_state = "cancelled"
                    session.add(row)
                    await session.flush()
            if row.artifact_state != "ready":
                path.unlink(missing_ok=True)
                raise VoiceError("voice_reply_stale", "保存期间会话、批准或批次已变化，语音产物已取消。", 409)
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        try:
            after = await self._reply_snapshot(
                conversation_id=conversation_id,
                expected_revision=expected_conversation_revision,
                source_message_id=source_message_id,
                batch_id=batch_id,
                expected_batch_version=expected_batch_version,
            )
            if not self._same_reply_snapshot(snapshot, after):
                raise VoiceError("voice_reply_stale", "会话在保存语音产物后发生变化，产物已取消。", 409)
        except VoiceError:
            await self._cancel_artifact(artifact_id)
            raise
        return self._artifact_dto(row)

    async def get_artifact_content(self, artifact_id: str) -> bytes:
        return await self._get_artifact_content(artifact_id)

    async def _get_artifact_content(
        self,
        artifact_id: str,
        *,
        expected: dict[str, Any] | None = None,
        allow_claimed_action: bool = False,
    ) -> bytes:
        try:
            async with self.session_factory() as session:
                artifact = await session.get(ManagedMediaRecord, artifact_id)
                if artifact is None:
                    raise VoiceError("voice_artifact_not_found", "语音产物不存在。", 404)
                if artifact.artifact_state != "ready":
                    raise VoiceError("voice_artifact_unavailable", "语音产物已取消或不可读取。", 409)
                if artifact.artifact_purpose == "reply":
                    await self._validate_reply_artifact_in_session(
                        session,
                        artifact,
                        expected=expected,
                        allow_claimed_action=allow_claimed_action,
                    )
                elif artifact.artifact_purpose != "approval_preview":
                    raise VoiceError("voice_artifact_unavailable", "语音产物用途不受支持。", 403)
                return self._read_asset_sync(artifact)
        except VoiceError as exc:
            if _should_cancel_artifact(exc):
                await self._cancel_artifact(artifact_id)
            raise

    async def resolve_voice_outbox_binding(
        self,
        artifact_id: str,
        *,
        conversation_id: str,
        expected_revision: int,
        batch_id: str,
        account_id: str,
        recipient_id: str,
        person_id: str,
        allow_claimed_action: bool = False,
    ) -> VoiceOutboxBinding:
        """Resolve and revalidate the exact reply artifact before any Outbox stage."""
        self._require_local_trial("语音授权、准备或派发")
        requested = {
            "conversation_id": conversation_id,
            "expected_revision": expected_revision,
            "batch_id": batch_id,
            "account_id": account_id,
            "recipient_id": recipient_id,
            "person_id": person_id,
        }
        try:
            async with self.session_factory() as session:
                artifact = await session.get(ManagedMediaRecord, artifact_id)
                if artifact is None:
                    raise VoiceError("voice_artifact_not_found", "语音产物不存在。", 404)
                if artifact.artifact_state != "ready" or artifact.artifact_purpose != "reply":
                    raise VoiceError("voice_artifact_unavailable", "只有可用的语音回复产物能进入受控外发流程。", 409)
                await self._validate_reply_artifact_in_session(
                    session,
                    artifact,
                    expected=requested,
                    allow_claimed_action=allow_claimed_action,
                )
                self._read_asset_sync(artifact)
                return VoiceOutboxBinding(
                    artifact_id=artifact.id,
                    sha256=artifact.sha256,
                    profile_id=artifact.profile_id,
                    profile_version=artifact.profile_version,
                    approval_id=artifact.approval_ref,
                    media_format="audio/wav",
                    duration_ms=artifact.duration_ms,
                )
        except VoiceError as exc:
            if _should_cancel_artifact(exc):
                await self._cancel_artifact(artifact_id)
            raise

    async def voice_validator(self, binding: dict[str, Any]) -> None:
        if not isinstance(binding, dict):
            raise VoiceError("invalid_voice_binding", "语音外发绑定格式无效。", 422)
        artifact_id = binding.get("artifact_id")
        if not isinstance(artifact_id, str):
            raise VoiceError("invalid_voice_binding", "语音外发绑定缺少产物 ID。", 422)
        lineage_fields = {
            "conversation_id", "expected_revision", "batch_id", "account_id", "recipient_id", "person_id"
        }
        binding_fields = {
            "artifact_id", "sha256", "profile_id", "profile_version", "approval_id", "media_format", "duration_ms"
        }
        if set(binding) != lineage_fields | binding_fields:
            raise VoiceError("invalid_voice_binding", "语音外发绑定缺少会话、版本、批次或 QQ 目标。", 422)
        fields = (
            "artifact_id",
            "sha256",
            "profile_id",
            "profile_version",
            "approval_id",
            "media_format",
            "duration_ms",
        )
        expected = {key: binding.get(key) for key in fields}
        resolved = await self.resolve_voice_outbox_binding(
            artifact_id,
            conversation_id=binding["conversation_id"],
            expected_revision=binding["expected_revision"],
            batch_id=binding["batch_id"],
            account_id=binding["account_id"],
            recipient_id=binding["recipient_id"],
            person_id=binding["person_id"],
        )
        if resolved.model_dump() != expected:
            raise VoiceError("voice_binding_changed", "语音产物、批准版本或媒体 hash 已变化。", 409)

    async def read_controlled_record(
        self,
        record_ref: dict[str, str],
        *,
        conversation_id: str,
        expected_revision: int,
        batch_id: str,
        account_id: str,
        recipient_id: str,
        person_id: str,
    ) -> ControlledRecordMedia:
        fields = {"artifact_id", "sha256", "voice_version", "approval_ref"}
        if not isinstance(record_ref, dict) or set(record_ref) != fields:
            raise VoiceError("invalid_voice_record_ref", "语音读取只接受登记产物及批准版本引用。", 422)
        binding = await self.resolve_voice_outbox_binding(
            record_ref["artifact_id"],
            conversation_id=conversation_id,
            expected_revision=expected_revision,
            batch_id=batch_id,
            account_id=account_id,
            recipient_id=recipient_id,
            person_id=person_id,
            allow_claimed_action=True,
        )
        expected_ref = {
            "artifact_id": binding.artifact_id,
            "sha256": binding.sha256,
            "voice_version": _voice_version(binding.profile_id, binding.profile_version),
            "approval_ref": binding.approval_id,
        }
        if record_ref != expected_ref:
            raise VoiceError("voice_record_ref_changed", "语音引用与当前批准的声线版本不匹配。", 409)
        content = await self._get_artifact_content(
            record_ref["artifact_id"],
            expected={
                "conversation_id": conversation_id,
                "expected_revision": expected_revision,
                "batch_id": batch_id,
                "account_id": account_id,
                "recipient_id": recipient_id,
                "person_id": person_id,
            },
            allow_claimed_action=True,
        )
        return ControlledRecordMedia(content=content, media_type="audio/wav")

    async def _profile_snapshot(self, expected_version: int) -> dict[str, Any]:
        async with self.session_factory() as session:
            state = await session.get(VoiceProfileState, "default")
            if (
                state is None
                or state.active_profile_id != VOICE_PROFILE_ID
                or state.active_profile_version != expected_version
            ):
                raise VoiceError("voice_profile_changed", "当前声线版本已变化，请刷新后重试。", 409)
            profile = await session.get(VoiceProfileVersion, (VOICE_PROFILE_ID, expected_version))
            if profile is None:
                raise VoiceError("voice_profile_not_found", "声线版本不存在。", 404)
            if profile.settings_hash != _profile_settings_hash(
                profile.engine_id,
                profile.voice_id,
                profile.voice_name,
                profile.culture,
                profile.engine_config_json,
            ):
                raise VoiceError("voice_profile_integrity_error", "声线设置 hash 与当前 profile 字段不匹配。", 409)
            if profile.engine_id == QWEN_ENGINE_ID and not _qwen_profile_binding_matches(
                profile.voice_id, profile.engine_config_json
            ):
                raise VoiceError("voice_profile_integrity_error", "Qwen profile 的候选绑定不完整。", 409)
            return {
                "profile_id": profile.profile_id,
                "profile_version": profile.version,
                "engine_id": profile.engine_id,
                "voice_id": profile.voice_id,
                "settings_hash": profile.settings_hash,
                "engine_config_json": profile.engine_config_json,
            }

    async def _synthesize_qwen(
        self, text: str, snapshot: dict[str, Any], *, max_text_chars: int
    ) -> bytes:
        content = text.strip()
        if not content or len(content) > max_text_chars:
            raise VoiceError(
                "invalid_voice_preview_text",
                f"语音文本必须为1到{max_text_chars}个字符。",
            )
        if self.voice_candidates is None or self.qwen_tts_runtime is None or self.local_audio_lane is None:
            raise VoiceError("qwen_runtime_unavailable", "本机 Qwen 语音 runtime 当前不可用。", 409)
        binding = _safe_engine_config(snapshot.get("engine_config_json"))
        if not isinstance(binding, dict) or not binding.get("candidate_id"):
            raise VoiceError("voice_profile_integrity_error", "Qwen profile 缺少候选绑定。", 409)
        try:
            current_binding, reference_audio = await self.voice_candidates.profile_reference(binding)
            base_snapshot = current_binding["base"]
            runtime_binding = current_binding["runtime"]
            async with self.local_audio_lane.job("tts") as job:
                reference_path = Path(job.workspace) / "reference.wav"
                with reference_path.open("xb") as stream:
                    stream.write(reference_audio)
                    stream.flush()
                    os.fsync(stream.fileno())
                verified_reference = reference_path.read_bytes()
                if (
                    hashlib.sha256(verified_reference).hexdigest()
                    != current_binding["reference_sha256"]
                ):
                    raise VoiceError("qwen_reference_mismatch", "声线候选参考 WAV hash 不匹配。", 409)
                result = await self.qwen_tts_runtime.synthesize_clone(
                    job,
                    text=content,
                    reference_wav=verified_reference,
                    reference_sha256=current_binding["reference_sha256"],
                    reference_text=current_binding["reference_text"],
                    expected_model_id=base_snapshot["model_id"],
                    expected_model_revision=base_snapshot["revision"],
                    expected_resource_fingerprint=base_snapshot["resource_fingerprint"],
                    max_new_tokens=runtime_binding["generation"]["initial_max_new_tokens"],
                    seed=current_binding["seed"],
                )
                _validate_qwen_base_result(result, current_binding)
                audio = _read_qwen_preview_result(result, job.workspace)
            return audio
        except VoiceError:
            raise
        except SpeechError as exc:
            raise VoiceError(exc.code, exc.message, exc.status_code) from exc
        except LocalAudioError as exc:
            raise VoiceError(exc.code, exc.message, exc.status_code) from exc
        except Exception as exc:
            code = getattr(exc, "code", None)
            message = getattr(exc, "message", None)
            status_code = getattr(exc, "status_code", None)
            if isinstance(code, str) and code.startswith("qwen_"):
                raise VoiceError(
                    code, str(message or "本机 Qwen 语音处理失败。"), int(status_code or 409)
                ) from exc
            raise

    async def _is_profile_current(self, snapshot: dict[str, Any]) -> bool:
        async with self.session_factory() as session:
            state = await session.get(VoiceProfileState, "default")
            profile = await session.get(
                VoiceProfileVersion, (snapshot["profile_id"], snapshot["profile_version"])
            )
            return bool(
                state is not None
                and profile is not None
                and state.active_profile_id == snapshot["profile_id"]
                and state.active_profile_version == snapshot["profile_version"]
                and profile.settings_hash == snapshot["settings_hash"]
                and profile.settings_hash == _profile_settings_hash(
                    profile.engine_id,
                    profile.voice_id,
                    profile.voice_name,
                    profile.culture,
                    profile.engine_config_json,
                )
            )

    async def _validate_approval_in_session(self, session, profile, approval) -> None:
        if (
            profile.settings_hash != _profile_settings_hash(
                profile.engine_id,
                profile.voice_id,
                profile.voice_name,
                profile.culture,
                profile.engine_config_json,
            )
            or approval.profile_id != profile.profile_id
            or approval.profile_version != profile.version
            or approval.settings_hash != profile.settings_hash
            or not approval.approved_by
            or not approval.preview_sha256
        ):
            raise VoiceError("voice_approval_changed", "语音批准已失效或与当前 profile 设置 hash 不一致。", 409)
        audition = await session.get(ManagedMediaRecord, approval.preview_artifact_id)
        if (
            audition is None
            or audition.asset_class != "generated_voice"
            or audition.artifact_purpose != "approval_preview"
            or audition.artifact_state != "ready"
            or audition.source_scope != "production"
            or audition.profile_id != profile.profile_id
            or audition.profile_version != profile.version
            or audition.profile_settings_hash != profile.settings_hash
            or audition.sha256 != approval.preview_sha256
            or audition.byte_length is None
        ):
            raise VoiceError("voice_approval_changed", "声线批准所依据的试听产物不可用。", 409)
        audio = self._read_asset_sync(audition)
        if hashlib.sha256(audio).hexdigest() != approval.preview_sha256:
            raise VoiceError("voice_approval_changed", "声线批准试听素材 hash 与文件内容不一致。", 409)

    async def _validate_qq_guard(self, guard: dict[str, Any]) -> None:
        if self.qq_service is None:
            raise VoiceError("qq_identity_unavailable", "当前 QQ 身份验证器不可用，语音回复未生成。", 409)
        try:
            await self.qq_service.validate_delivery_target(guard)
        except QqError as exc:
            raise VoiceError(exc.code, exc.message, exc.status_code) from exc

    async def _reply_snapshot(
        self,
        *,
        conversation_id: str,
        expected_revision: int,
        source_message_id: str,
        batch_id: str,
        expected_batch_version: int,
    ) -> dict[str, Any]:
        async with self.session_factory() as session:
            return await self._reply_snapshot_in_session(
                session,
                conversation_id=conversation_id,
                expected_revision=expected_revision,
                source_message_id=source_message_id,
                batch_id=batch_id,
                expected_batch_version=expected_batch_version,
            )

    async def _reply_snapshot_in_session(
        self,
        session,
        *,
        conversation_id: str,
        expected_revision: int,
        source_message_id: str,
        batch_id: str,
        expected_batch_version: int,
    ) -> dict[str, Any]:
        self._require_local_trial("生成 QQ 语音回复")
        state = await session.get(VoiceProfileState, "default")
        if state is None or not state.runtime_enabled:
            raise VoiceError("voice_runtime_disabled", "本机语音运行时尚未由管理员启用。", 409)
        if not state.voice_outbox_enabled:
            raise VoiceError("voice_outbox_disabled", "本次语音回复功能尚未由管理员启用。", 409)
        if not state.active_profile_id or not state.active_profile_version:
            raise VoiceError("voice_profile_required", "尚未选择本机声线。", 409)
        profile = await session.get(
            VoiceProfileVersion, (state.active_profile_id, state.active_profile_version)
        )
        approval = (
            await session.get(VoiceApproval, state.active_approval_id)
            if state.active_approval_id
            else None
        )
        if profile is None or approval is None:
            raise VoiceError("voice_approval_required", "当前声线版本尚未通过试听批准。", 409)
        if profile.engine_id == QWEN_ENGINE_ID and not _qwen_profile_binding_matches(
            profile.voice_id, profile.engine_config_json
        ):
            raise VoiceError("voice_profile_integrity_error", "Qwen profile 的候选绑定不完整。", 409)
        await self._validate_approval_in_session(session, profile, approval)

        conversation = await session.get(Conversation, conversation_id)
        if conversation is None:
            raise VoiceError("conversation_not_found", "会话不存在。", 404)
        if conversation.revision != expected_revision:
            raise VoiceError("conversation_revision_conflict", "会话已变化，请刷新后重试。", 409)
        if (
            conversation.person_id != OWNER_PERSON_ID
            or conversation.scope != "production"
            or conversation.purpose != "production"
            or conversation.channel != "qq"
            or conversation.delivery_mode != "qq"
            or not conversation.account_id
            or not conversation.recipient_id
        ):
            raise VoiceError("voice_conversation_unavailable", "语音回复只支持已绑定管理员身份的正式 QQ 私聊。", 403)
        if conversation.state != "active" or conversation.stop_requested or conversation.human_takeover:
            raise VoiceError("conversation_unavailable", "会话已停止、接管或忙碌，语音回复未生成。", 409)

        latest = await session.scalar(
            select(ChatMessage)
            .where(ChatMessage.conversation_id == conversation_id)
            .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
            .limit(1)
        )
        message = await session.get(ChatMessage, source_message_id)
        if (
            latest is None
            or message is None
            or latest.id != source_message_id
            or message.role != "assistant"
            or message.channel != "qq"
            or message.scope != "production"
            or message.person_id != OWNER_PERSON_ID
            or not message.content.strip()
            or (message.token_usage_json or {}).get("turn_state") == "stale"
        ):
            raise VoiceError("voice_source_message_unavailable", "只能为当前会话最新的正式助手回复生成语音。", 409)
        if batch_id == conversation.batch_id:
            raise VoiceError("voice_trial_batch_unavailable", "语音回复必须使用独立于会话来源批次的 qq.voice.send 批次。", 409)
        batch = await session.get(TrialBatch, batch_id)
        if (
            batch is None
            or batch.module_id != "qq"
            or batch.capability_id != "qq.voice.send"
            or list(batch.object_ids or []) != [conversation.recipient_id]
            or batch.state != "running"
            or batch.version != expected_batch_version
            or _utc(batch.expires_at) <= utc_now()
            or not _is_single_action_batch(batch.stop_condition)
            or batch.action_count >= int(batch.stop_condition.get("max_actions", 0))
        ):
            raise VoiceError("voice_trial_batch_unavailable", "需要一个尚未过期、正在运行且只绑定当前收件人的单动作 qq.voice.send 批次。", 409)

        return {
            "conversation_id": conversation.id,
            "source_revision": conversation.revision,
            "source_message_id": message.id,
            "content": message.content,
            "account_id": conversation.account_id,
            "recipient_id": conversation.recipient_id,
            "person_id": conversation.person_id,
            "source_batch_id": conversation.batch_id,
            "batch_id": batch.id,
            "batch_version": batch.version,
            "profile_id": profile.profile_id,
            "profile_version": profile.version,
            "settings_hash": profile.settings_hash,
            "approval_id": approval.id,
            "guard": {
                "account_id": conversation.account_id,
                "recipient_id": conversation.recipient_id,
                "person_id": conversation.person_id,
                "conversation_id": conversation.id,
                "delivery_mode": conversation.delivery_mode,
                "scope": conversation.scope,
                "channel": conversation.channel,
                "revision": conversation.revision,
                "state": conversation.state,
                "stop_requested": bool(conversation.stop_requested),
                "human_takeover": bool(conversation.human_takeover),
                "batch_id": conversation.batch_id,
            },
            "voice_id": profile.voice_id,
            "engine_id": profile.engine_id,
            "engine_config_json": profile.engine_config_json,
        }

    async def _validate_reply_artifact_in_session(
        self,
        session,
        artifact: ManagedMediaRecord,
        *,
        expected: dict[str, Any] | None = None,
        allow_claimed_action: bool = False,
    ) -> None:
        required_values = (
            artifact.profile_id,
            artifact.profile_version,
            artifact.profile_settings_hash,
            artifact.approval_ref,
            artifact.conversation_id,
            artifact.source_message_id,
            artifact.source_revision,
            artifact.account_id,
            artifact.recipient_id,
            artifact.batch_id,
            artifact.batch_version,
            artifact.sha256,
            artifact.duration_ms,
        )
        if (
            artifact.asset_class != "generated_voice"
            or artifact.artifact_purpose != "reply"
            or artifact.artifact_state != "ready"
            or not artifact.role_use_approved
            or artifact.source_scope != "production"
            or artifact.source_person_id != OWNER_PERSON_ID
            or any(value is None for value in required_values)
        ):
            raise VoiceError("voice_artifact_unavailable", "语音产物缺少完整登记信息或批准版本。", 409)
        state = await session.get(VoiceProfileState, "default")
        if (
            state is None
            or not state.runtime_enabled
            or not state.voice_outbox_enabled
            or state.active_profile_id != artifact.profile_id
            or state.active_profile_version != artifact.profile_version
            or state.active_approval_id != artifact.approval_ref
        ):
            raise VoiceError("voice_approval_changed", "语音运行开关或已批准声线版本已变化。", 409)
        profile = await session.get(
            VoiceProfileVersion, (artifact.profile_id, artifact.profile_version)
        )
        approval = await session.get(VoiceApproval, artifact.approval_ref)
        if (
            profile is None
            or approval is None
            or profile.settings_hash != artifact.profile_settings_hash
            or profile.settings_hash != _profile_settings_hash(
                profile.engine_id,
                profile.voice_id,
                profile.voice_name,
                profile.culture,
                profile.engine_config_json,
            )
            or approval.profile_id != profile.profile_id
            or approval.profile_version != profile.version
            or approval.settings_hash != profile.settings_hash
            or approval.preview_sha256 == ""
        ):
            raise VoiceError("voice_approval_changed", "语音批准已失效或与当前 profile 版本不一致。", 409)
        audition = await session.get(ManagedMediaRecord, approval.preview_artifact_id)
        if (
            audition is None
            or audition.asset_class != "generated_voice"
            or audition.artifact_purpose != "approval_preview"
            or audition.artifact_state != "ready"
            or audition.profile_id != profile.profile_id
            or audition.profile_version != profile.version
            or audition.profile_settings_hash != profile.settings_hash
            or audition.sha256 != approval.preview_sha256
            or audition.byte_length is None
            or audition.sha256 != approval.preview_sha256
        ):
            raise VoiceError("voice_approval_changed", "声线批准所依据的试听产物不可用。", 409)
        self._read_asset_sync(audition)

        conversation = await session.get(Conversation, artifact.conversation_id)
        if (
            conversation is None
            or conversation.revision != artifact.source_revision
            or conversation.person_id != OWNER_PERSON_ID
            or conversation.scope != "production"
            or conversation.purpose != "production"
            or conversation.channel != "qq"
            or conversation.delivery_mode != "qq"
            or conversation.state != "active"
            or conversation.stop_requested
            or conversation.human_takeover
            or conversation.account_id != artifact.account_id
            or conversation.recipient_id != artifact.recipient_id
            or conversation.batch_id != artifact.source_batch_id
        ):
            raise VoiceError("voice_reply_stale", "语音来源会话或准确 QQ 目标已变化，产物已取消。", 409)
        source = await session.get(ChatMessage, artifact.source_message_id)
        latest = await session.scalar(
            select(ChatMessage)
            .where(ChatMessage.conversation_id == conversation.id)
            .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
            .limit(1)
        )
        if (
            source is None
            or latest is None
            or latest.id != source.id
            or source.role != "assistant"
            or source.scope != "production"
            or source.channel != "qq"
            or source.person_id != OWNER_PERSON_ID
            or (source.token_usage_json or {}).get("turn_state") == "stale"
        ):
            raise VoiceError("voice_reply_stale", "语音源消息不再是当前会话最新的助手回复，产物已取消。", 409)
        batch = await session.get(TrialBatch, artifact.batch_id)
        if (
            batch is None
            or batch.module_id != "qq"
            or batch.capability_id != "qq.voice.send"
            or list(batch.object_ids or []) != [artifact.recipient_id]
            or batch.state != "running"
            or batch.version != artifact.batch_version
            or _utc(batch.expires_at) <= utc_now()
            or not _is_single_action_batch(batch.stop_condition)
            or batch.action_count > int(batch.stop_condition["max_actions"])
            or (not allow_claimed_action and batch.action_count >= int(batch.stop_condition["max_actions"]))
        ):
            raise VoiceError("voice_trial_batch_unavailable", "语音产物绑定的单动作 qq.voice.send 批次已变化或不可用。", 409)

        requested = expected or {}
        comparisons = {
            "conversation_id": artifact.conversation_id,
            "expected_revision": artifact.source_revision,
            "batch_id": artifact.batch_id,
            "account_id": artifact.account_id,
            "recipient_id": artifact.recipient_id,
            "person_id": OWNER_PERSON_ID,
        }
        for key, actual in comparisons.items():
            value = requested.get(key)
            if value != actual:
                raise VoiceError("voice_target_mismatch", "语音产物与本次授权的会话、批次或 QQ 目标不匹配。", 403)

    async def _cancel_artifact(self, artifact_id: str) -> None:
        path = None
        async with self.session_factory() as session:
            async with session.begin():
                artifact = await session.get(ManagedMediaRecord, artifact_id)
                if artifact is not None and artifact.artifact_state != "cancelled":
                    artifact.artifact_state = "cancelled"
                    artifact.legacy_state = "cancelled"
                    try:
                        path = self._artifact_path(artifact, must_exist=False)
                    except (OSError, VoiceError):
                        path = None
                    await session.flush()
        if path is not None:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

    async def _insert_artifact(self, artifact: ManagedMediaRecord) -> None:
        async with self.session_factory() as session:
            async with session.begin():
                session.add(artifact)
                await session.flush()

    def _new_artifact(
        self,
        *,
        artifact_id: str,
        profile_id: str,
        profile_version: int,
        profile_settings_hash: str,
        approval_ref: str | None,
        purpose: str,
        state: str,
        sha256: str,
        duration_ms: int,
        byte_length: int,
        actor_id: str,
        source_refs: list[dict[str, Any]],
        source_person_id: str | None = None,
        conversation_id: str | None = None,
        account_id: str | None = None,
        recipient_id: str | None = None,
        batch_id: str | None = None,
        batch_version: int | None = None,
        source_batch_id: str | None = None,
        source_message_id: str | None = None,
        source_revision: int | None = None,
    ) -> ManagedMediaRecord:
        now = utc_now()
        relative_path = f"{VOICE_ARTIFACT_ROOT}/{artifact_id}.wav"
        return ManagedMediaRecord(
            id=artifact_id,
            root_key="data_root",
            relative_path=relative_path,
            sha256=sha256,
            mime_type="audio/wav",
            byte_length=byte_length,
            source_refs=source_refs,
            source_scope="production",
            source_person_id=source_person_id,
            role_use_approved=purpose == "reply",
            share_allowed=False,
            registered_by=actor_id,
            version=1,
            asset_class="generated_voice",
            generation_request_id=artifact_id,
            profile_id=profile_id,
            profile_version=profile_version,
            profile_settings_hash=profile_settings_hash,
            approval_ref=approval_ref,
            artifact_purpose=purpose,
            artifact_state=state,
            duration_ms=duration_ms,
            conversation_id=conversation_id,
            account_id=account_id,
            recipient_id=recipient_id,
            batch_id=batch_id,
            batch_version=batch_version,
            source_batch_id=source_batch_id,
            source_message_id=source_message_id,
            source_revision=source_revision,
            created_at=now,
            legacy_state="registered" if state == "ready" else "cancelled",
            legacy_media_type="audio/wav",
            legacy_size_bytes=byte_length,
            legacy_updated_at=now,
        )

    def _write_audio(self, artifact: ManagedMediaRecord, audio: bytes):
        if len(audio) != artifact.byte_length or hashlib.sha256(audio).hexdigest() != artifact.sha256:
            raise VoiceError("voice_artifact_integrity_error", "生成语音与登记 hash 不匹配。", 409)
        try:
            path = self._artifact_path(artifact, must_exist=False, for_write=True)
            path.parent.mkdir(parents=True, exist_ok=True)
            path = self._artifact_path(artifact, must_exist=False, for_write=True)
            with path.open("xb") as stream:
                stream.write(audio)
                stream.flush()
                os.fsync(stream.fileno())
            return path
        except VoiceError:
            raise
        except (PathPolicyError, OSError, ValueError) as exc:
            raise VoiceError("voice_artifact_storage_unavailable", "本机受控语音目录当前不可写。", 409) from exc

    def _artifact_path(
        self,
        artifact: ManagedMediaRecord,
        *,
        must_exist: bool,
        for_write: bool = False,
    ):
        if (
            artifact.asset_class != "generated_voice"
            or artifact.root_key != "data_root"
            or artifact.relative_path != f"{VOICE_ARTIFACT_ROOT}/{artifact.id}.wav"
            or not artifact.relative_path.startswith(VOICE_ARTIFACT_PREFIX)
        ):
            raise VoiceError("invalid_voice_artifact_path", "语音产物未登记在本机专用语音目录。", 403)
        try:
            return self.paths.resolve(
                "data_root",
                artifact.relative_path,
                must_exist=must_exist,
                for_write=for_write,
            )
        except (PathPolicyError, FileNotFoundError, OSError, ValueError) as exc:
            raise VoiceError("voice_artifact_unavailable", "本机受控语音文件当前不可读取。", 409) from exc

    def _read_asset_sync(self, artifact: ManagedMediaRecord) -> bytes:
        path = self._artifact_path(artifact, must_exist=True)
        if artifact.mime_type != "audio/wav" or artifact.byte_length is None or artifact.byte_length <= 0:
            raise VoiceError("voice_artifact_integrity_error", "语音产物清单字段无效。", 409)
        if artifact.byte_length > MAX_VOICE_ARTIFACT_BYTES:
            raise VoiceError("voice_artifact_too_large", "语音产物超过本机受控素材大小上限。", 413)
        try:
            before = path.stat()
            if not path.is_file() or before.st_size != artifact.byte_length:
                raise VoiceError("voice_artifact_integrity_error", "语音文件长度与登记清单不一致。", 409)
            with path.open("rb") as stream:
                raw = stream.read(MAX_VOICE_ARTIFACT_BYTES + 1)
            after = path.stat()
        except VoiceError:
            raise
        except OSError as exc:
            raise VoiceError("voice_artifact_unavailable", "语音产物当前无法读取。", 409) from exc
        if (
            len(raw) != artifact.byte_length
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
            or hashlib.sha256(raw).hexdigest() != artifact.sha256
        ):
            raise VoiceError("voice_artifact_integrity_error", "语音文件长度、时间戳或 SHA-256 校验不一致。", 409)
        duration_ms = self._validate_audio(raw, max_seconds=MAX_REPLY_SECONDS)
        if duration_ms != artifact.duration_ms:
            raise VoiceError("voice_artifact_integrity_error", "语音文件 WAV 时长与登记清单不一致。", 409)
        return raw

    @staticmethod
    def _validate_audio(audio: bytes, *, max_seconds: int) -> int:
        if not isinstance(audio, bytes) or not audio or len(audio) > MAX_VOICE_ARTIFACT_BYTES:
            raise VoiceError("voice_artifact_integrity_error", "生成结果不是有效的本机 WAV 语音产物。", 502)
        try:
            duration = _validate_wave(audio, max_seconds=max_seconds)
        except (wave.Error, EOFError, OSError, ValueError) as exc:
            raise VoiceError("voice_artifact_integrity_error", "生成结果没有通过 WAV 完整性校验。", 502) from exc
        return int(round(duration * 1000))

    @staticmethod
    def _artifact_dto(artifact: ManagedMediaRecord) -> dict[str, Any]:
        return {
            "artifact_id": artifact.id,
            "profile_id": artifact.profile_id,
            "profile_version": artifact.profile_version,
            "voice_version": _voice_version(artifact.profile_id, artifact.profile_version),
            "sha256": artifact.sha256,
            "media_format": "audio/wav",
            "duration_ms": artifact.duration_ms,
            "purpose": artifact.artifact_purpose,
            "artifact_state": artifact.artifact_state,
        }

    @staticmethod
    def _same_reply_snapshot(first: dict[str, Any], second: dict[str, Any]) -> bool:
        keys = (
            "conversation_id",
            "source_revision",
            "source_message_id",
            "account_id",
            "recipient_id",
            "person_id",
            "source_batch_id",
            "batch_id",
            "batch_version",
            "profile_id",
            "profile_version",
            "settings_hash",
            "approval_id",
        )
        return all(first.get(key) == second.get(key) for key in keys)

    def _require_local_trial(self, operation: str) -> None:
        if self.get_deployment_profile() != "local_trial":
            raise VoiceError("target_staging", f"target_staging 禁止{operation}。", 409)


def _is_single_action_batch(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and value.get("kind") == "after_n"
        and type(value.get("max_actions")) is int
        and value["max_actions"] == 1
    )


def _profile_settings_hash(
    engine_id: str,
    voice_id: str,
    voice_name: str,
    culture: str,
    engine_config_json: str | None = None,
) -> str:
    profile_data = {
        "engine_id": engine_id,
        "voice_id": voice_id,
        "voice_name": voice_name,
        "culture": culture,
    }
    if engine_config_json is not None:
        try:
            profile_data["engine_config_json"] = json.loads(engine_config_json)
        except (TypeError, json.JSONDecodeError):
            profile_data["engine_config_json"] = {"invalid": str(engine_config_json)}
    return hashlib.sha256(
        json.dumps(profile_data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _canonical_json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _safe_engine_config(value: str | None) -> dict[str, Any] | None:
    if value is None:
        return None
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _qwen_profile_binding_matches(voice_id: str, engine_config_json: str | None) -> bool:
    binding = _safe_engine_config(engine_config_json)
    candidate_id = binding.get("candidate_id") if isinstance(binding, dict) else None
    return isinstance(candidate_id, str) and voice_id == f"{VOICE_ID_PREFIX}{candidate_id}"


def _read_qwen_preview_result(result: Any, workspace: Path) -> bytes:
    raw_path = _qwen_result_value(result, "wav_path")
    if raw_path is None:
        raise VoiceError("qwen_output_invalid", "Qwen Base worker 没有返回 WAV 文件。", 502)
    try:
        workspace_path = Path(workspace).resolve(strict=True)
        output_path = Path(raw_path).resolve(strict=True)
        output_path.relative_to(workspace_path)
        before = output_path.stat()
        if not output_path.is_file() or not 0 < before.st_size <= MAX_VOICE_ARTIFACT_BYTES:
            raise VoiceError("qwen_output_invalid", "Qwen Base WAV 超过8 MiB或为空。", 502)
        with output_path.open("rb") as stream:
            audio = stream.read(MAX_VOICE_ARTIFACT_BYTES + 1)
        after = output_path.stat()
    except VoiceError:
        raise
    except (OSError, ValueError, TypeError) as exc:
        raise VoiceError("qwen_output_invalid", "Qwen Base WAV 未保留在当前临时工作区。", 502) from exc
    if len(audio) != before.st_size or before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns:
        raise VoiceError("qwen_output_invalid", "Qwen Base WAV 在校验期间发生变化。", 502)
    duration_ms = VoiceService._validate_audio(audio, max_seconds=MAX_REPLY_SECONDS)
    reported_size = _qwen_result_value(result, "byte_length")
    reported_hash = _qwen_result_value(result, "sha256")
    reported_duration = _qwen_result_value(result, "duration_seconds")
    reported_rate = _qwen_result_value(result, "sample_rate")
    try:
        with wave.open(io.BytesIO(audio), "rb") as wav:
            sample_rate = wav.getframerate()
    except (wave.Error, EOFError, OSError) as exc:
        raise VoiceError("qwen_output_invalid", "Qwen Base WAV 头无效。", 502) from exc
    if (
        reported_size != len(audio)
        or reported_hash != hashlib.sha256(audio).hexdigest()
        or not isinstance(reported_duration, (int, float))
        or abs(float(reported_duration) - duration_ms / 1000) > 0.002
        or reported_rate != sample_rate
    ):
        raise VoiceError("qwen_output_invalid", "Qwen Base WAV 与 worker 返回的元数据不匹配。", 502)
    return audio


def _validate_qwen_base_result(result: Any, binding: dict[str, Any]) -> None:
    base = binding.get("base")
    runtime = binding.get("runtime")
    generation = runtime.get("generation") if isinstance(runtime, dict) else None
    if not isinstance(base, dict) or not isinstance(generation, dict):
        raise VoiceError("voice_profile_integrity_error", "Qwen profile 缺少 Base 运行绑定。", 409)
    if any(
        _qwen_result_value(result, result_key) != base.get(binding_key)
        for result_key, binding_key in (
            ("model_id", "model_id"),
            ("model_revision", "revision"),
            ("resource_fingerprint", "resource_fingerprint"),
        )
    ):
        raise VoiceError("qwen_resource_mismatch", "Qwen Base 返回的模型信息与 profile 绑定不一致。", 409)

    settings = _qwen_result_value(result, "settings")
    retried = settings.get("retry_after_cuda_oom") if isinstance(settings, dict) else None
    initial_tokens = generation.get("initial_max_new_tokens")
    retry_tokens = generation.get("oom_retry_max_new_tokens")
    expected_tokens = retry_tokens if retried is True else initial_tokens
    expected_settings = {
        "device": runtime.get("device"),
        "dtype": runtime.get("dtype"),
        "attn_implementation": runtime.get("attn_implementation"),
        "trust_remote_code": runtime.get("trust_remote_code"),
        "local_files_only": runtime.get("downloads_enabled") is False,
        "seed": binding.get("seed"),
        "initial_max_new_tokens": initial_tokens,
        "max_new_tokens": expected_tokens,
        "attempt_count": 2 if retried is True else 1,
        "retry_policy": generation.get("oom_retry_policy"),
        "language": "Chinese",
        "retry_after_cuda_oom": retried,
    }
    if (
        not isinstance(settings, dict)
        or type(retried) is not bool
        or any(settings.get(key) != value for key, value in expected_settings.items())
        or type(settings.get("seed")) is not int
        or type(settings.get("initial_max_new_tokens")) is not int
        or type(settings.get("max_new_tokens")) is not int
        or type(settings.get("attempt_count")) is not int
    ):
        raise VoiceError("qwen_worker_failed", "Qwen Base 实际生成设置与 profile 绑定策略不一致。", 502)

    byte_length = _qwen_result_value(result, "byte_length")
    duration = _qwen_result_value(result, "duration_seconds")
    if (
        type(byte_length) is not int
        or not 0 < byte_length <= generation.get("max_output_bytes", 0)
        or isinstance(duration, bool)
        or not isinstance(duration, (int, float))
        or not math.isfinite(float(duration))
        or not 0 < duration <= generation.get("max_output_seconds", 0)
    ):
        raise VoiceError("qwen_output_invalid", "Qwen Base 输出超出 profile 绑定的大小或时长上限。", 502)


def _qwen_result_value(result: Any, key: str):
    if isinstance(result, dict):
        return result.get(key)
    return getattr(result, key, None)


def _should_cancel_artifact(exc: VoiceError) -> bool:
    return exc.code in {
        "voice_artifact_unavailable",
        "voice_artifact_integrity_error",
        "voice_artifact_too_large",
        "voice_approval_changed",
        "voice_reply_stale",
        "voice_trial_batch_unavailable",
    }


def _voice_version(profile_id: str | None, version: int | None) -> str | None:
    if profile_id is None or version is None:
        return None
    return f"{profile_id}@{version}"


def _iso(value: datetime | None) -> str | None:
    return _utc(value).isoformat() if value is not None else None


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
