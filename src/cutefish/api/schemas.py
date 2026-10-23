"""Strict request DTOs for the local API."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator, model_validator


class RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PasswordRequest(RequestModel):
    password: str = Field(min_length=1, max_length=256)


class DeepSeekCredentialPut(RequestModel):
    api_key: StrictStr = Field(min_length=1, max_length=4096)

    @field_validator("api_key")
    @classmethod
    def reject_blank_api_key(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("DeepSeek API Key不能为空。")
        return value


class DeepSeekCredentialStatus(RequestModel):
    provider: Literal["deepseek"]
    configured: bool


class UiSettingsPatch(RequestModel):
    theme: Literal["light", "dark", "system"]
    locale: Literal["zh-CN"]
    expected_version: int = Field(ge=1)


class ModuleConfigPut(RequestModel):
    values: dict[str, object]
    expected_version: int = Field(ge=1)


class BatchTarget(RequestModel):
    module_id: str = Field(min_length=1, max_length=128)
    capability_id: str | None = Field(default=None, max_length=128)
    object_ids: list[str] = Field(default_factory=list, max_length=100)


class ManualStopCondition(RequestModel):
    kind: Literal["manual"]


class CountStopCondition(RequestModel):
    kind: Literal["after_n"]
    max_actions: int = Field(ge=1, le=100)


class TrialBatchCreate(RequestModel):
    label: str = Field(min_length=1, max_length=120)
    target: BatchTarget
    expires_at: datetime
    stop_condition: ManualStopCondition | CountStopCondition


class OutboxPrepare(RequestModel):
    idempotency_key: str = Field(min_length=1, max_length=256)
    conversation_id: str = Field(min_length=1, max_length=36)
    expected_revision: int = Field(ge=0)
    batch_id: str = Field(min_length=1, max_length=36)
    action_id: Literal["qq.message.send", "qq.voice.send"] = "qq.message.send"
    artifact_id: str | None = Field(default=None, min_length=1, max_length=36)
    authorization_id: str | None = Field(default=None, max_length=36)
    dispatch_reason: Literal["owner_explicit"] = "owner_explicit"
    group_id: str | None = Field(default=None, min_length=1, max_length=128)
    segment_index: int = Field(default=0, ge=0, le=99)
    segment_count: int = Field(default=1, ge=1, le=100)
    request_id: str | None = Field(default=None, max_length=128)
    payload: str | None = Field(default=None, min_length=1, max_length=4000)

    @model_validator(mode="after")
    def validate_action_payload(self):
        if self.action_id == "qq.message.send":
            if self.payload is None or self.artifact_id is not None:
                raise ValueError("文本外发必须提供消息内容，且不能提供语音产物标识。")
        elif self.artifact_id is None or self.payload is not None:
            raise ValueError("语音外发必须提供语音产物标识，且不能提交媒体或文本内容。")
        if self.action_id == "qq.voice.send" and (
            self.group_id is not None or self.segment_index != 0 or self.segment_count != 1
        ):
            raise ValueError("语音外发只支持一个单目标片段。")
        return self


class OutboxAuthorizationCreate(RequestModel):
    conversation_id: str = Field(min_length=1, max_length=36)
    expected_revision: int = Field(ge=0)
    batch_id: str = Field(min_length=1, max_length=36)
    action_id: Literal["qq.message.send", "qq.voice.send"] = "qq.message.send"
    artifact_id: str | None = Field(default=None, min_length=1, max_length=36)
    payload: str | None = Field(default=None, min_length=1, max_length=4000)
    expires_in_seconds: int = Field(default=600, ge=1, le=1800)

    @model_validator(mode="after")
    def validate_action_payload(self):
        if self.action_id == "qq.message.send":
            if self.payload is None or self.artifact_id is not None:
                raise ValueError("文本外发必须提供消息内容，且不能提供语音产物标识。")
        elif self.artifact_id is None or self.payload is not None:
            raise ValueError("语音外发必须提供语音产物标识，且不能提交媒体或文本内容。")
        return self
