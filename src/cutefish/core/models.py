"""Core SQLite schema. Feature modules share this DeclarativeBase."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import Boolean, DateTime, Index, Integer, JSON, LargeBinary, Numeric, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class SchemaVersion(Base):
    __tablename__ = "schema_version"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class AdminAccount(Base):
    __tablename__ = "admin_account"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    username: Mapped[str] = mapped_column(String(32), unique=True, nullable=False, default="owner")
    password_hash: Mapped[str] = mapped_column(String(512), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)


class AuthSession(Base):
    __tablename__ = "auth_session"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    csrf_token: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AppSetting(Base):
    __tablename__ = "app_setting"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)


class ModuleConfiguration(Base):
    __tablename__ = "module_configuration"

    module_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    value: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)


class FeatureState(Base):
    __tablename__ = "feature_state"

    feature_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    requirement_status: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
    delivery_status: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
    module_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    evidence_refs: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)


class TrialBatch(Base):
    __tablename__ = "trial_batch"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    label: Mapped[str] = mapped_column(String(120), nullable=False)
    module_id: Mapped[str] = mapped_column(String(128), nullable=False)
    capability_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    object_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    stop_condition: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    state: Mapped[str] = mapped_column(String(24), nullable=False, default="draft", index=True)
    action_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    stop_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class SecretRecord(Base):
    """Machine-protected secret blob; export code must always exclude this table."""

    __tablename__ = "secret_record"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    encrypted_value: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)


class BudgetPolicy(Base):
    __tablename__ = "budget_policy"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    daily_limit: Mapped[Any] = mapped_column(Numeric(12, 4), nullable=False, default=8)
    background_soft_limit: Mapped[Any] = mapped_column(Numeric(12, 4), nullable=False, default=3)
    background_borrow_limit: Mapped[Any] = mapped_column(Numeric(12, 4), nullable=False, default=3)
    background_hard_limit: Mapped[Any] = mapped_column(Numeric(12, 4), nullable=False, default=6)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)


class BudgetEntry(Base):
    """One durable estimate/reservation and its eventual settlement state."""

    __tablename__ = "budget_entry"
    __table_args__ = (
        Index("ix_budget_entry_day_state", "local_date", "state"),
        Index("ix_budget_entry_day_background_state", "local_date", "is_background", "state"),
    )

    reservation_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(256), nullable=False, unique=True)
    provider: Mapped[str | None] = mapped_column(String(128), nullable=True)
    model: Mapped[str | None] = mapped_column(String(256), nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    provider_request_id: Mapped[str | None] = mapped_column(String(256), nullable=True, index=True)
    purpose: Mapped[str] = mapped_column(String(128), nullable=False)
    person_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    account_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    local_date: Mapped[str] = mapped_column(String(10), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="CNY")
    estimated_cost: Mapped[Any] = mapped_column(Numeric(14, 6), nullable=False)
    actual_cost: Mapped[Any | None] = mapped_column(Numeric(14, 6), nullable=True)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="reserved", index=True)
    is_background: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cache_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    input_price_per_million: Mapped[Any | None] = mapped_column(Numeric(14, 6), nullable=True)
    output_price_per_million: Mapped[Any | None] = mapped_column(Numeric(14, 6), nullable=True)
    price_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    settlement_evidence_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ActionAuthorization(Base):
    """A short-lived, exact-target grant issued by an authenticated owner action."""

    __tablename__ = "action_authorization"
    __table_args__ = (
        Index("ix_action_auth_scope", "action_id", "recipient_id", "batch_id", "expires_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    action_id: Mapped[str] = mapped_column(String(128), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    person_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    conversation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    conversation_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    delivery_mode: Mapped[str | None] = mapped_column(String(32), nullable=True)
    recipient_id: Mapped[str] = mapped_column(String(128), nullable=False)
    account_id: Mapped[str] = mapped_column(String(128), nullable=False)
    batch_id: Mapped[str] = mapped_column(String(36), nullable=False)
    batch_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    max_uses: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    used_uses: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="owner_explicit")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class OutboxItem(Base):
    """A prepared external message segment; sending is always an explicit later step."""

    __tablename__ = "outbox_item"
    __table_args__ = (
        Index("ix_outbox_state_created", "state", "created_at"),
        Index("ix_outbox_group_segment", "conversation_id", "segment_index"),
        Index(
            "uq_outbox_group_segment_slot",
            "conversation_id",
            "batch_id",
            "group_id",
            "segment_index",
            unique=True,
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(256), nullable=False, unique=True)
    request_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    conversation_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    expected_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    batch_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    recipient_id: Mapped[str] = mapped_column(String(128), nullable=False)
    account_id: Mapped[str] = mapped_column(String(128), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    person_id: Mapped[str] = mapped_column(String(128), nullable=False)
    action_id: Mapped[str] = mapped_column(String(128), nullable=False, default="qq.message.send")
    message_type: Mapped[str] = mapped_column(String(16), nullable=False, default="text")
    record_ref: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    authorization_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    delivery_mode: Mapped[str] = mapped_column(String(32), nullable=False, default="qq")
    batch_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    dispatch_reason: Mapped[str] = mapped_column(String(24), nullable=False)
    group_id: Mapped[str] = mapped_column(String(128), nullable=False)
    segment_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    segment_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="prepared", index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    attempt_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    remote_receipt_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    dispatch_evidence_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)
    result_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    sending_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ToolActionPreviewRecord(Base):
    """Immutable, content-addressed preview for one non-QQ target action."""

    __tablename__ = "tool_action_preview"
    __table_args__ = (Index("ix_tool_action_preview_state_expires", "state", "expires_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    site: Mapped[str] = mapped_column(String(64), nullable=False)
    capability_id: Mapped[str] = mapped_column(String(128), nullable=False)
    handler_version: Mapped[str] = mapped_column(String(64), nullable=False)
    account_id: Mapped[str] = mapped_column(String(128), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(256), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expected_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    scope: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    source_refs: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    state: Mapped[str] = mapped_column(String(24), nullable=False, default="preview_only", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ToolActionGrant(Base):
    """One-use owner grant bound to one preview and one running trial batch."""

    __tablename__ = "tool_action_grant"
    __table_args__ = (Index("ix_tool_action_grant_scope", "preview_id", "batch_id", "expires_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    preview_id: Mapped[str] = mapped_column(String(36), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    batch_id: Mapped[str] = mapped_column(String(36), nullable=False)
    batch_version: Mapped[int] = mapped_column(Integer, nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    scope_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ToolActionOutboxItem(Base):
    """Durable non-QQ Outbox entry; an adapter attempt is never replayed."""

    __tablename__ = "tool_action_outbox"
    __table_args__ = (
        Index("ix_tool_action_outbox_state_created", "state", "created_at"),
        Index("ix_tool_action_outbox_batch", "batch_id", "state"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(256), nullable=False, unique=True)
    preview_id: Mapped[str] = mapped_column(String(36), nullable=False)
    grant_id: Mapped[str] = mapped_column(String(36), nullable=False)
    batch_id: Mapped[str] = mapped_column(String(36), nullable=False)
    batch_version: Mapped[int] = mapped_column(Integer, nullable=False)
    site: Mapped[str] = mapped_column(String(64), nullable=False)
    capability_id: Mapped[str] = mapped_column(String(128), nullable=False)
    handler_version: Mapped[str] = mapped_column(String(64), nullable=False)
    account_id: Mapped[str] = mapped_column(String(128), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(256), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expected_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    scope: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    source_refs: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    state: Mapped[str] = mapped_column(String(24), nullable=False, default="prepared", index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    attempt_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    evidence_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)
    remote_receipt_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    result_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    sending_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

