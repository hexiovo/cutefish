"""Shared, versionable contracts used by the local application modules."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum
from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr


class OperationStatus(StrEnum):
    SUCCESS = "success"
    FAILED = "failed"
    UNKNOWN = "unknown"
    NOT_CONNECTED = "not_connected"
    NEEDS_LOGIN = "needs_login"
    NEEDS_INPUT = "needs_input"
    NEEDS_VERIFICATION = "needs_verification"
    PARTIAL = "partial"
    UNSUPPORTED = "unsupported"


class ModuleRuntimeState(StrEnum):
    DISABLED = "disabled"
    NOT_CONNECTED = "not_connected"
    NEEDS_LOGIN = "needs_login"
    STARTING = "starting"
    READY = "ready"
    LAZY_IDLE = "lazy_idle"
    FAILED = "failed"


class ModuleLifecycle(StrEnum):
    ON_DEMAND = "on_demand"
    SESSION = "session"
    BACKGROUND = "background"


class ResourceClass(StrEnum):
    LIGHT = "light"
    BROWSER = "browser"
    HIGH_MEMORY = "high_memory"


@dataclass(frozen=True, slots=True)
class ActorContext:
    """Trusted business identity, kept separate from the local admin session."""

    person_id: str
    role: Literal["owner", "contact", "unknown"]
    external_actor_id: str | None
    channel: str
    account_id: str | None
    trusted: bool


@dataclass(frozen=True, slots=True)
class ProviderUsageEvidence:
    """A usage receipt bound by a provider adapter to one reservation."""

    reservation_id: str
    provider: str
    model: str
    provider_request_id: str
    input_tokens: int
    output_tokens: int
    actual_cost: Decimal
    evidence_ref: str
    cache_tokens: int | None = None


class _ToolActionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ToolActionPreview(_ToolActionModel):
    """Immutable, side-effect-free preview returned by an integration adapter."""

    preview_id: StrictStr = Field(min_length=1, max_length=36)
    site: StrictStr = Field(min_length=1, max_length=64)
    action_id: StrictStr = Field(min_length=1, max_length=128)
    capability_id: StrictStr = Field(min_length=1, max_length=128)
    handler_version: StrictStr = Field(min_length=1, max_length=64)
    account_id: StrictStr = Field(min_length=1, max_length=128)
    resource_id: StrictStr = Field(min_length=1, max_length=256)
    action: StrictStr = Field(min_length=1, max_length=64)
    payload_json: StrictStr = Field(min_length=2, max_length=65_536)
    payload_hash: StrictStr = Field(pattern=r"^[a-f0-9]{64}$")
    expected_revision: StrictInt = Field(ge=0)
    actor_id: StrictStr = Field(min_length=1, max_length=128)
    scope: dict[str, Any]
    source_refs: list[dict[str, Any]] = Field(max_length=64)
    expires_in_seconds: StrictInt = Field(default=600, ge=1, le=1800)


class ToolActionEnvelope(_ToolActionModel):
    """The only payload shape accepted by the non-QQ Outbox action branch."""

    action_id: StrictStr = Field(min_length=1, max_length=128)
    capability_id: StrictStr = Field(min_length=1, max_length=128)
    handler_version: StrictStr = Field(min_length=1, max_length=64)
    site: StrictStr = Field(min_length=1, max_length=64)
    account_id: StrictStr = Field(min_length=1, max_length=128)
    resource_id: StrictStr = Field(min_length=1, max_length=256)
    action: StrictStr = Field(min_length=1, max_length=64)
    payload_json: StrictStr = Field(min_length=2, max_length=65_536)
    payload_hash: StrictStr = Field(pattern=r"^[a-f0-9]{64}$")
    preview_id: StrictStr = Field(min_length=1, max_length=36)
    grant_id: StrictStr = Field(min_length=1, max_length=36)
    batch_id: StrictStr = Field(min_length=1, max_length=36)
    expected_revision: StrictInt = Field(ge=0)
    actor_id: StrictStr = Field(min_length=1, max_length=128)
    idempotency_key: StrictStr = Field(min_length=1, max_length=256)
    scope: dict[str, Any]
    source_refs: list[dict[str, Any]] = Field(max_length=64)


@dataclass(frozen=True, slots=True)
class ToolActionReceipt:
    """One adapter's durable evidence after a single typed action attempt."""

    outcome: Literal["confirmed", "rejected"]
    evidence_ref: str
    remote_receipt_id: str | None = None
    result_code: str | None = None
    no_side_effect_proven: bool = False


class VoiceOutboxBinding(BaseModel):
    """Trusted, re-resolved metadata for one approved voice artifact."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    artifact_id: StrictStr = Field(min_length=1, max_length=36)
    sha256: StrictStr = Field(pattern=r"^[a-f0-9]{64}$")
    profile_id: StrictStr = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    profile_version: StrictInt = Field(ge=1)
    approval_id: StrictStr = Field(min_length=1, max_length=36)
    media_format: Literal["audio/wav"]
    duration_ms: StrictInt = Field(ge=1)


class SideEffect(StrEnum):
    NONE = "none"
    LOCAL_WRITE = "local_write"
    EXTERNAL_WRITE = "external_write"


class ErrorInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    retryable: bool = False
    details: dict[str, Any] | None = None


class EvidenceItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str
    source: str
    description: str
    reference: str | None = None


class UsageInfo(BaseModel):
    model_config = ConfigDict(extra="allow")

    provider: str | None = None
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    estimated_cost: Decimal | None = None
    actual_cost: Decimal | None = None
    currency: str | None = None
    reservation_id: str | None = None
    state: str | None = None


T = TypeVar("T")


class ResultEnvelope(BaseModel, Generic[T]):
    """One response shape for API calls, capabilities, and background work."""

    model_config = ConfigDict(extra="forbid")

    status: OperationStatus
    data: T | None = None
    evidence: list[EvidenceItem] = Field(default_factory=list)
    error: ErrorInfo | None = None
    usage: UsageInfo | None = None
    request_id: str
    time: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

