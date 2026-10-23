"""Persistent extension records, registered explicitly by the application."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, JSON, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from cutefish.core.models import Base, utc_now


class TaskRecord(Base):
    __tablename__ = "extension_task"
    __table_args__ = (Index("ix_extension_task_status_updated", "status", "updated_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    intent: Mapped[str] = mapped_column(Text, nullable=False)
    progress: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_step: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="open", index=True)
    source_refs: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    capability_refs: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    budget_refs: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    explicit_pause: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    manual_takeover: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    pause_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    effect_state: Mapped[str] = mapped_column(String(24), nullable=False, default="not_dispatched")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now)


class TaskCandidate(Base):
    __tablename__ = "extension_candidate"
    __table_args__ = (
        Index("ix_extension_candidate_state_created", "state", "created_at"),
        Index("ix_extension_candidate_task", "task_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_id: Mapped[str | None] = mapped_column(ForeignKey("extension_task.id", ondelete="SET NULL"), nullable=True)
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    target_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)
    source_event_refs: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    experience_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    context: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    not_before: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    permission_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    budget_estimate: Mapped[Decimal | None] = mapped_column(Numeric(14, 6), nullable=True)
    defer_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    state: Mapped[str] = mapped_column(String(24), nullable=False, default="proposed", index=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    outbox_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now)


class RoomAuthorization(Base):
    """A separately granted role-workspace permission, never implied by host mapping."""

    __tablename__ = "extension_room_authorization"
    __table_args__ = (Index("ix_extension_room_auth_active", "root_key", "revoked_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    root_key: Mapped[str] = mapped_column(String(64), nullable=False)
    relative_prefix: Mapped[str] = mapped_column(Text, nullable=False)
    allowed_actions: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    granted_by: Mapped[str] = mapped_column(String(128), nullable=False)
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RoomItem(Base):
    __tablename__ = "extension_room_item"
    __table_args__ = (
        Index("ix_extension_room_item_state_updated", "state", "updated_at"),
        Index("ix_extension_room_item_kind_state", "kind", "state"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    root_key: Mapped[str] = mapped_column(String(64), nullable=False, default="room_root")
    relative_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Text notes are stored in the indexed record so list/detail endpoints stay
    # database-only and never have to probe the host-bound room directory.
    content_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_refs: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    source_scope: Mapped[str] = mapped_column(String(32), nullable=False)
    source_person_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    role_owned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    managed_media_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    share_allowed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    tags: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    state: Mapped[str] = mapped_column(String(24), nullable=False, default="pending", index=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now)


class ManagedMediaRecord(Base):
    """An explicitly registered media file under an allowed logical root.

    The legacy columns are retained as nullable compatibility fields so an
    earlier local SQLite table with NOT NULL legacy columns can still accept
    newly registered records during the additive migration window.
    """

    __tablename__ = "extension_managed_media"
    __table_args__ = (
        UniqueConstraint("root_key", "relative_path", name="uq_extension_media_root_path"),
        Index("uq_extension_media_generation_request", "generation_request_id", unique=True),
        Index("ix_extension_media_scope", "source_scope", "role_use_approved"),
        Index("ix_extension_media_created", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    root_key: Mapped[str] = mapped_column(String(64), nullable=False)
    relative_path: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(128), nullable=False)
    byte_length: Mapped[int] = mapped_column(Integer, nullable=False)
    source_refs: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    source_scope: Mapped[str] = mapped_column(String(32), nullable=False)
    source_person_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    role_use_approved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    share_allowed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    registered_by: Mapped[str] = mapped_column(String(128), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # Ordinary room media remains the default. The voice service is the only
    # writer allowed to create generated_voice rows under data_root.
    asset_class: Mapped[str] = mapped_column(String(32), nullable=False, default="room")
    generation_request_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    profile_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    profile_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    profile_settings_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    approval_ref: Mapped[str | None] = mapped_column(String(36), nullable=True)
    artifact_purpose: Mapped[str | None] = mapped_column(String(32), nullable=True)
    artifact_state: Mapped[str | None] = mapped_column(String(24), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    conversation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    account_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    recipient_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    batch_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    batch_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_batch_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    source_message_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    source_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    legacy_state: Mapped[str | None] = mapped_column("state", String(24), nullable=True, default="registered")
    legacy_media_type: Mapped[str | None] = mapped_column("media_type", String(128), nullable=True)
    legacy_size_bytes: Mapped[int | None] = mapped_column("size_bytes", Integer, nullable=True)
    legacy_updated_at: Mapped[datetime | None] = mapped_column("updated_at", DateTime(timezone=True), nullable=True, default=utc_now)


class VoiceProfileVersion(Base):
    """Immutable engine settings for one explicitly selected voice version."""

    __tablename__ = "extension_voice_profile_version"
    __table_args__ = (Index("ix_extension_voice_profile_created", "created_at"),)

    profile_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    engine_id: Mapped[str] = mapped_column(String(128), nullable=False)
    voice_id: Mapped[str] = mapped_column(String(512), nullable=False)
    voice_name: Mapped[str] = mapped_column(String(256), nullable=False)
    culture: Mapped[str] = mapped_column(String(64), nullable=False)
    settings_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)


class VoiceProfileState(Base):
    """Mutable pointer and explicit runtime switches for the local voice profile."""

    __tablename__ = "extension_voice_profile_state"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default="default")
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    active_profile_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    active_profile_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    active_approval_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    runtime_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    voice_outbox_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    updated_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now)


class VoiceApproval(Base):
    """Immutable owner decision bound to an exact profile version and audition."""

    __tablename__ = "extension_voice_approval"
    __table_args__ = (
        UniqueConstraint("preview_artifact_id", name="uq_extension_voice_approval_preview"),
        Index("ix_extension_voice_approval_profile", "profile_id", "profile_version", "approved_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    profile_id: Mapped[str] = mapped_column(String(128), nullable=False)
    profile_version: Mapped[int] = mapped_column(Integer, nullable=False)
    preview_artifact_id: Mapped[str] = mapped_column(
        ForeignKey("extension_managed_media.id", ondelete="RESTRICT"), nullable=False
    )
    preview_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    settings_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    approved_by: Mapped[str] = mapped_column(String(128), nullable=False)
    approved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)


class RoomDiskState(Base):
    """Cached status only; a GET never probes or walks the bound host directory."""

    __tablename__ = "extension_room_disk_state"

    root_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    disk_state: Mapped[str] = mapped_column(String(24), nullable=False, default="unknown")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now)
