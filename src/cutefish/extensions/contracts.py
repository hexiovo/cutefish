"""Strict API DTOs for extension-local task and room operations."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, model_validator


class RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TaskCreate(RequestModel):
    title: str = Field(min_length=1, max_length=256)
    intent: str = Field(min_length=1, max_length=8192)
    progress: str | None = Field(default=None, max_length=8192)
    next_step: str | None = Field(default=None, max_length=2048)
    source_refs: list[str] = Field(default_factory=list, max_length=200)
    capability_refs: list[str] = Field(default_factory=list, max_length=100)
    budget_refs: list[str] = Field(default_factory=list, max_length=100)


class RevisionAction(RequestModel):
    expected_revision: int = Field(ge=1)
    reason: str | None = Field(default=None, max_length=1024)
    release_handover: bool = False


class CandidateAction(RequestModel):
    expected_revision: int = Field(ge=1)
    reason: str | None = Field(default=None, max_length=1024)


class CandidateCreate(RequestModel):
    title: str = Field(min_length=1, max_length=256)
    task_id: str | None = None
    target_ref: str | None = Field(default=None, max_length=512)
    source_event_refs: list[str] = Field(default_factory=list, max_length=200)
    experience_ref: str | None = Field(default=None, max_length=512)
    reason: str = Field(default="", max_length=4096)
    context: dict[str, object] = Field(default_factory=dict)
    not_before: datetime | None = None
    expires_at: datetime | None = None
    source_revision: int = Field(default=1, ge=1)
    permission_revision: int = Field(default=1, ge=1)
    budget_estimate: float | None = Field(default=None, ge=0)
    defer_reason: str | None = Field(default=None, max_length=1024)


RoomKind = Literal["collection", "note", "interest", "plan", "work", "album", "share_candidate"]
RoomRoot = Literal["room_root", "gallery_root"]
RoomAction = Literal["read", "create", "update", "rename", "soft_delete", "restore", "export", "register_media"]


class SourceReference(RequestModel):
    kind: Literal["room_item", "managed_media", "chat_event", "owner_supplied"]
    id: str = Field(min_length=1, max_length=512)
    person_id: str | None = Field(default=None, min_length=1, max_length=128)
    scope: Literal["production", "calibration"] | None = None

    @model_validator(mode="after")
    def owner_source_has_no_claimed_person(self):
        if self.kind == "owner_supplied" and self.person_id is not None:
            raise ValueError("owner_supplied 来源不能声明 person_id")
        return self


class RoomCreate(RequestModel):
    kind: RoomKind
    title: str = Field(min_length=1, max_length=256)
    relative_path: str | None = Field(default=None, max_length=1024)
    content_text: str | None = Field(default=None, max_length=262_144)
    managed_media_id: str | None = Field(default=None, min_length=1, max_length=128)
    source_refs: list[SourceReference] = Field(min_length=1, max_length=200)
    source_scope: Literal["production", "calibration"]
    tags: list[str] = Field(default_factory=list, max_length=100)
    share_allowed: bool = False

    @model_validator(mode="after")
    def one_content_source(self):
        if (self.content_text is None) == (self.managed_media_id is None):
            raise ValueError("content_text 与 managed_media_id 必须且只能提供一个")
        return self


class RoomPatch(RequestModel):
    expected_version: int = Field(ge=1)
    title: str | None = Field(default=None, min_length=1, max_length=256)
    tags: list[str] | None = Field(default=None, max_length=100)
    share_allowed: bool | None = None
    content_text: str | None = Field(default=None, max_length=262_144)
    new_relative_path: str | None = Field(default=None, max_length=1024)

    @model_validator(mode="after")
    def require_change(self):
        if all(
            value is None
            for value in (self.title, self.tags, self.share_allowed, self.content_text, self.new_relative_path)
        ):
            raise ValueError("至少提供一个需要更新的字段")
        return self


class RoomVersionAction(RequestModel):
    expected_version: int = Field(ge=1)


class RoomAuthorizationPut(RequestModel):
    root_key: RoomRoot
    relative_prefix: str = Field(max_length=1024)
    allowed_actions: list[RoomAction] = Field(min_length=1, max_length=8)
    expected_revision: int = Field(ge=0)


class RoomExportRequest(RequestModel):
    expected_version: int = Field(ge=1)


class ManagedMediaRegister(RequestModel):
    root_key: RoomRoot
    relative_path: str = Field(min_length=1, max_length=1024)
    source_refs: list[SourceReference] = Field(min_length=1, max_length=200)
    source_scope: Literal["production", "calibration"]
    source_person_id: str | None = Field(default=None, min_length=1, max_length=128)
    mime_type: str | None = Field(default=None, min_length=1, max_length=128)
    role_use_approved: bool = False
    share_allowed: bool = False


class TtsPreviewRequest(RequestModel):
    text: str = Field(min_length=1, max_length=240)
    voice_id: str = Field(min_length=1, max_length=512)
    task_id: str | None = Field(default=None, min_length=1, max_length=36)
    expected_task_revision: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def task_revision_pair(self):
        if (self.task_id is None) != (self.expected_task_revision is None):
            raise ValueError("task_id 与 expected_task_revision 必须同时提供")
        return self


class VoiceCandidateCreate(RequestModel):
    """Create one locally generated, unapproved VoiceDesign reference."""

    model_config = ConfigDict(extra="forbid", strict=True)

    text: StrictStr = Field(min_length=1, max_length=160)
    style_prompt: StrictStr = Field(min_length=1, max_length=500)
    seed: StrictInt = Field(ge=0, le=4_294_967_295)

    @model_validator(mode="after")
    def require_nonblank_inputs(self):
        if not self.text.strip() or not self.style_prompt.strip():
            raise ValueError("参考文本和风格提示不能为空")
        return self


class VoiceProfilePut(RequestModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    expected_revision: StrictInt = Field(ge=1)
    voice_id: StrictStr = Field(min_length=1, max_length=512)


class VoiceSettingsPut(RequestModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    expected_revision: StrictInt = Field(ge=1)
    runtime_enabled: StrictBool
    voice_outbox_enabled: StrictBool


class VoicePreviewCreate(RequestModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    expected_profile_version: StrictInt = Field(ge=1)
    text: StrictStr = Field(min_length=1, max_length=240)


class VoicePreviewApproval(RequestModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    expected_profile_version: StrictInt = Field(ge=1)
    preview_artifact_id: StrictStr = Field(min_length=1, max_length=36)


class VoiceReplyCreate(RequestModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    conversation_id: StrictStr = Field(min_length=1, max_length=36)
    expected_conversation_revision: StrictInt = Field(ge=0)
    source_message_id: StrictStr = Field(min_length=1, max_length=36)
    batch_id: StrictStr = Field(min_length=1, max_length=36)
    expected_batch_version: StrictInt = Field(ge=1)


class AsrTranscriptionRequest(RequestModel):
    room_item_id: str = Field(min_length=1, max_length=36)
    expected_room_version: int = Field(ge=1)
    task_id: str | None = Field(default=None, min_length=1, max_length=36)
    expected_task_revision: int | None = Field(default=None, ge=1)
    submit_to_chat: bool = False
    conversation_id: str | None = Field(default=None, min_length=1, max_length=36)
    expected_conversation_revision: int | None = Field(default=None, ge=0)
    chat_idempotency_key: str | None = Field(default=None, min_length=1, max_length=256)

    @model_validator(mode="after")
    def task_revision_pair(self):
        if (self.task_id is None) != (self.expected_task_revision is None):
            raise ValueError("task_id 与 expected_task_revision 必须同时提供")
        chat_fields = (
            self.conversation_id,
            self.expected_conversation_revision,
            self.chat_idempotency_key,
        )
        if self.submit_to_chat and any(value is None for value in chat_fields):
            raise ValueError("submit_to_chat 需要会话 ID、预期版本和聊天幂等键")
        if not self.submit_to_chat and any(value is not None for value in chat_fields):
            raise ValueError("只有明确提交到 ChatService 时才能提供聊天字段")
        if self.submit_to_chat and self.task_id is not None:
            raise ValueError("带任务锁的 ASR 不能在同一请求中派发模型聊天")
        return self
