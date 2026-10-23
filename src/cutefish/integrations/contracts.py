"""Serializable source and action-preview contracts for integrations."""

from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Literal
from urllib.parse import parse_qsl, urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_SECRET_KEY_PARTS = frozenset(
    {"authorization", "cookie", "credential", "csrf", "key", "password", "secret", "session", "token"}
)


class ContractError(ValueError):
    """Raised when untrusted data cannot be represented by an integration contract."""


class SourceTimeRange(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)

    @model_validator(mode="after")
    def _ordered(self) -> "SourceTimeRange":
        if self.end_ms <= self.start_ms:
            raise ValueError("end_ms must be greater than start_ms")
        return self


class SourceEvidence(BaseModel):
    """Hash and provenance metadata for bytes actually read by an adapter."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    site_id: str
    capability_id: str
    source_url: str
    object_id: str
    content_type: str
    fetched_at: datetime
    sha256: str
    byte_length: int = Field(ge=0)
    read_scope: Literal["metadata", "content", "subtitle", "frame", "range"]
    time_range: SourceTimeRange | None = None

    @field_validator("site_id", "capability_id")
    @classmethod
    def _valid_ids(cls, value: str) -> str:
        if not _ID.fullmatch(value):
            raise ValueError("identifier has an invalid format")
        return value

    @field_validator("object_id")
    @classmethod
    def _valid_object_id(cls, value: str) -> str:
        if not value or len(value) > 512 or any(ord(char) < 32 for char in value):
            raise ValueError("object_id must be a non-empty, printable identifier")
        return value

    @field_validator("source_url")
    @classmethod
    def _safe_source_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or len(value) > 4096
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            raise ValueError("source_url must be an HTTPS URL without credentials or a fragment")
        try:
            query = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True, max_num_fields=16)
        except ValueError as exc:
            raise ValueError("source_url query is invalid") from exc
        for key, query_value in query:
            if _is_secret_field(key) or len(query_value) > 512:
                raise ValueError("source_url query cannot contain credential-like fields")
        return value

    @field_validator("content_type")
    @classmethod
    def _normal_content_type(cls, value: str) -> str:
        normalized = value.split(";", 1)[0].strip().lower()
        if not normalized or len(normalized) > 128 or any(ord(char) < 33 for char in normalized):
            raise ValueError("content_type must be a bounded media type")
        return normalized

    @field_validator("fetched_at")
    @classmethod
    def _aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("fetched_at must include a timezone")
        return value.astimezone(timezone.utc)

    @field_validator("sha256")
    @classmethod
    def _valid_hash(cls, value: str) -> str:
        if not _SHA256.fullmatch(value):
            raise ValueError("sha256 must be a lowercase SHA-256 digest")
        return value

    @model_validator(mode="after")
    def _range_scope(self) -> "SourceEvidence":
        if self.read_scope == "range" and self.time_range is None:
            raise ValueError("range reads must record their actual media time range")
        if self.time_range is not None and self.read_scope not in {"range", "frame"}:
            raise ValueError("time_range is only valid for frame or range reads")
        return self


class ActionScope(BaseModel):
    """The only supported scope shape is one exact site object."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["single_object"] = "single_object"
    site_id: str
    object_id: str
    action: str

    @field_validator("site_id", "action")
    @classmethod
    def _valid_scope_ids(cls, value: str) -> str:
        if not _ID.fullmatch(value):
            raise ValueError("scope identifier has an invalid format")
        return value

    @field_validator("object_id")
    @classmethod
    def _valid_scope_object_id(cls, value: str) -> str:
        if not value or len(value) > 512 or any(ord(char) < 32 for char in value):
            raise ValueError("scope object_id must be a non-empty, printable identifier")
        return value


class ActionPreview(BaseModel):
    """A preview-only, content-addressed proposal; it does not authorize execution."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    preview_id: str
    site_id: str
    account_ref: str | None = None
    object_id: str
    action: str
    payload_json: str
    payload_hash: str
    source_refs: tuple[SourceEvidence, ...]
    scope: ActionScope
    created_at: datetime
    preview_hash: str
    state: Literal["preview_only"] = "preview_only"

    @field_validator("preview_id")
    @classmethod
    def _uuid_preview_id(cls, value: str) -> str:
        try:
            return str(uuid.UUID(value))
        except (ValueError, AttributeError) as exc:
            raise ValueError("preview_id must be a UUID") from exc

    @field_validator("site_id", "action")
    @classmethod
    def _valid_preview_ids(cls, value: str) -> str:
        if not _ID.fullmatch(value):
            raise ValueError("preview identifier has an invalid format")
        return value

    @field_validator("account_ref")
    @classmethod
    def _safe_account_ref(cls, value: str | None) -> str | None:
        if value is not None and (not _ID.fullmatch(value) or len(value) > 128):
            raise ValueError("account_ref must be a non-secret local alias")
        return value

    @field_validator("object_id")
    @classmethod
    def _valid_preview_object_id(cls, value: str) -> str:
        if not value or len(value) > 512 or any(ord(char) < 32 for char in value):
            raise ValueError("object_id must be a non-empty, printable identifier")
        return value

    @field_validator("created_at")
    @classmethod
    def _aware_created_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at must include a timezone")
        return value.astimezone(timezone.utc)

    @field_validator("payload_hash", "preview_hash")
    @classmethod
    def _valid_preview_hashes(cls, value: str) -> str:
        if not _SHA256.fullmatch(value):
            raise ValueError("preview digests must be lowercase SHA-256 values")
        return value

    @model_validator(mode="after")
    def _bound_scope_and_hashes(self) -> "ActionPreview":
        if (self.scope.site_id, self.scope.object_id, self.scope.action) != (
            self.site_id,
            self.object_id,
            self.action,
        ):
            raise ValueError("scope must bind this preview to its one site, object, and action")
        try:
            payload = json.loads(self.payload_json)
        except json.JSONDecodeError as exc:
            raise ValueError("payload_json must be valid JSON") from exc
        canonical = canonical_json(payload)
        if canonical != self.payload_json:
            raise ValueError("payload_json is not in canonical form")
        if sha256_text(canonical) != self.payload_hash:
            raise ValueError("payload_hash does not match payload_json")
        binding = _preview_binding(
            site_id=self.site_id,
            account_ref=self.account_ref,
            object_id=self.object_id,
            action=self.action,
            payload_json=self.payload_json,
            payload_hash=self.payload_hash,
            source_refs=self.source_refs,
            scope=self.scope,
        )
        if sha256_text(canonical_json(binding)) != self.preview_hash:
            raise ValueError("preview_hash does not match this preview binding")
        return self


def canonical_json(value: Any) -> str:
    """Serialize JSON deterministically while rejecting ambiguous values/secrets."""

    _validate_json_value(value)
    try:
        serialized = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ContractError("payload cannot be represented as canonical JSON") from exc
    if len(serialized.encode("utf-8")) > 1_000_000:
        raise ContractError("canonical payload exceeds the 1 MB limit")
    return serialized


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def build_action_preview(
    *,
    site_id: str,
    account_ref: str | None,
    object_id: str,
    action: str,
    payload: dict[str, Any],
    source_refs: list[SourceEvidence] | tuple[SourceEvidence, ...],
    scope: ActionScope,
    created_at: datetime | None = None,
    preview_id: str | None = None,
) -> ActionPreview:
    """Build a stable exact-target preview. The returned object carries no grant."""

    if not isinstance(payload, dict):
        raise ContractError("action payload must be a JSON object")
    payload_json = canonical_json(payload)
    payload_hash = sha256_text(payload_json)
    refs = tuple(source_refs)
    bound_at = created_at or datetime.now(timezone.utc)
    binding = _preview_binding(
        site_id=site_id,
        account_ref=account_ref,
        object_id=object_id,
        action=action,
        payload_json=payload_json,
        payload_hash=payload_hash,
        source_refs=refs,
        scope=scope,
    )
    return ActionPreview(
        preview_id=preview_id or str(uuid.uuid4()),
        site_id=site_id,
        account_ref=account_ref,
        object_id=object_id,
        action=action,
        payload_json=payload_json,
        payload_hash=payload_hash,
        source_refs=refs,
        scope=scope,
        created_at=bound_at,
        preview_hash=sha256_text(canonical_json(binding)),
    )


def _preview_binding(
    *,
    site_id: str,
    account_ref: str | None,
    object_id: str,
    action: str,
    payload_json: str,
    payload_hash: str,
    source_refs: tuple[SourceEvidence, ...],
    scope: ActionScope,
) -> dict[str, Any]:
    return {
        "site_id": site_id,
        "account_ref": account_ref,
        "object_id": object_id,
        "action": action,
        "payload_json": payload_json,
        "payload_hash": payload_hash,
        "source_refs": [item.model_dump(mode="json") for item in source_refs],
        "scope": scope.model_dump(mode="json"),
    }


def _validate_json_value(value: Any, *, path: str = "payload") -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ContractError(f"{path} contains a non-finite number")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_json_value(item, path=f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ContractError(f"{path} contains a non-string object key")
            if _is_secret_field(key):
                raise ContractError(f"{path} contains a credential-like field name")
            _validate_json_value(item, path=f"{path}.{key}")
        return
    raise ContractError(f"{path} contains a value outside the JSON data model")


def _is_secret_field(value: str) -> bool:
    lowered = value.casefold().replace("-", "_")
    return any(part in _SECRET_KEY_PARTS for part in lowered.split("_"))
