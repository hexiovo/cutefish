"""Durable, person-scoped memory with preserved evidence and corrections."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from cutefish.core.models import Base, utc_now


class MemoryPerson(Base):
    __tablename__ = "memory_person"

    person_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    role: Mapped[str] = mapped_column(String(24), nullable=False, default="contact")
    state: Mapped[str] = mapped_column(String(24), nullable=False, default="active")
    chat_only: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)


class MemoryItem(Base):
    __tablename__ = "memory_item"
    __table_args__ = (
        Index("ix_memory_scope_person_kind", "person_id", "scope", "kind"),
        Index("ix_memory_scope_person_effective", "person_id", "scope", "effective_from", "effective_until"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    person_id: Mapped[str] = mapped_column(ForeignKey("memory_person.person_id", ondelete="RESTRICT"), nullable=False)
    # Calibration memories are intentionally distinct from production memories.
    scope: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    source_message_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    source_event_id: Mapped[str | None] = mapped_column(String(256), nullable=True, index=True)
    source_reference: Mapped[str | None] = mapped_column(String(512), nullable=True)
    source_locator: Mapped[str | None] = mapped_column(String(512), nullable=True)
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    effective_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    supersedes_id: Mapped[str | None] = mapped_column(ForeignKey("memory_item.id", ondelete="RESTRICT"), nullable=True)
    corrected_by_id: Mapped[str | None] = mapped_column(ForeignKey("memory_item.id", ondelete="RESTRICT"), nullable=True)
    invalidation_reason: Mapped[str | None] = mapped_column(String(128), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    version: Mapped[int] = mapped_column(default=1, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)

