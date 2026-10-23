"""Stored conversations, original messages, requests, and reviewable drafts."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from cutefish.core.models import Base, utc_now


class Conversation(Base):
    __tablename__ = "conversation"
    __table_args__ = (
        Index("ix_conversation_person_scope_updated", "person_id", "scope", "updated_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    person_id: Mapped[str] = mapped_column(ForeignKey("memory_person.person_id", ondelete="RESTRICT"), nullable=False)
    scope: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    channel: Mapped[str] = mapped_column(String(24), nullable=False, default="ui")
    purpose: Mapped[str] = mapped_column(String(32), nullable=False, default="production")
    delivery_mode: Mapped[str] = mapped_column(String(24), nullable=False, default="ui")
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    state: Mapped[str] = mapped_column(String(24), nullable=False, default="active", index=True)
    stop_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    human_takeover: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    account_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    recipient_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    batch_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    persona_version_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    config_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    title: Mapped[str | None] = mapped_column(String(256), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    taken_over_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ChatMessage(Base):
    __tablename__ = "chat_message"
    __table_args__ = (
        Index("ix_chat_message_person_scope_created", "person_id", "scope", "created_at"),
        Index("ix_chat_message_conversation_created", "conversation_id", "created_at"),
        UniqueConstraint("conversation_id", "source_event_id", name="uq_chat_message_conversation_event"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversation.id", ondelete="RESTRICT"), nullable=False)
    person_id: Mapped[str] = mapped_column(ForeignKey("memory_person.person_id", ondelete="RESTRICT"), nullable=False)
    scope: Mapped[str] = mapped_column(String(24), nullable=False)
    role: Mapped[str] = mapped_column(String(24), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    channel: Mapped[str] = mapped_column(String(24), nullable=False)
    source_event_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    provider: Mapped[str | None] = mapped_column(String(128), nullable=True)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    configured_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    provider_response_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    config_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    persona_version_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    finish_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    token_usage_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class ChatRequest(Base):
    __tablename__ = "chat_request"
    __table_args__ = (
        Index("ix_chat_request_state_created", "state", "created_at"),
    )

    local_request_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(256), nullable=False, unique=True)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversation.id", ondelete="RESTRICT"), nullable=False, index=True)
    inbound_message_id: Mapped[str] = mapped_column(ForeignKey("chat_message.id", ondelete="RESTRICT"), nullable=False, unique=True)
    response_message_id: Mapped[str | None] = mapped_column(ForeignKey("chat_message.id", ondelete="RESTRICT"), nullable=True)
    reservation_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    provider_response_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    config_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    persona_version_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    source_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    state: Mapped[str] = mapped_column(String(24), nullable=False, default="queued", index=True)
    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    provider: Mapped[str | None] = mapped_column(String(128), nullable=True)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)


class ChatDraft(Base):
    __tablename__ = "chat_draft"
    __table_args__ = (
        Index("ix_chat_draft_person_scope_state", "person_id", "scope", "state"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversation.id", ondelete="RESTRICT"), nullable=False, index=True)
    person_id: Mapped[str] = mapped_column(ForeignKey("memory_person.person_id", ondelete="RESTRICT"), nullable=False)
    scope: Mapped[str] = mapped_column(String(24), nullable=False)
    source_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    source_message_id: Mapped[str] = mapped_column(ForeignKey("chat_message.id", ondelete="RESTRICT"), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False, default="review", index=True)
    stale_reason: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)


class InterviewSession(Base):
    __tablename__ = "persona_interview_session"
    __table_args__ = (Index("ix_persona_interview_state", "state", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    person_id: Mapped[str] = mapped_column(ForeignKey("memory_person.person_id", ondelete="RESTRICT"), nullable=False)
    # An interview starts before a candidate persona exists. Once the owner
    # saves a draft, this points at the immutable version being trialed.
    persona_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("persona_version.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversation.id", ondelete="RESTRICT"), nullable=False, unique=True)
    state: Mapped[str] = mapped_column(String(24), nullable=False, default="active", index=True)
    title: Mapped[str | None] = mapped_column(String(256), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PersonaVersion(Base):
    __tablename__ = "persona_version"
    __table_args__ = (
        UniqueConstraint("persona_key", "version_number", name="uq_persona_version_key_number"),
        Index("ix_persona_version_state_created", "state", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    persona_key: Mapped[str] = mapped_column(String(128), nullable=False, default="cutefish")
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    version_label: Mapped[str] = mapped_column(String(128), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    state: Mapped[str] = mapped_column(String(24), nullable=False, default="draft", index=True)
    source_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    source_request_id: Mapped[str | None] = mapped_column(ForeignKey("chat_request.local_request_id", ondelete="RESTRICT"), nullable=True)
    source_interview_id: Mapped[str | None] = mapped_column(ForeignKey("persona_interview_session.id", ondelete="RESTRICT"), nullable=True)
    based_on_version_id: Mapped[str | None] = mapped_column(ForeignKey("persona_version.id", ondelete="RESTRICT"), nullable=True)
    config_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by_person_id: Mapped[str] = mapped_column(ForeignKey("memory_person.person_id", ondelete="RESTRICT"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    state_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PersonaTrialFeedback(Base):
    __tablename__ = "persona_trial_feedback"
    __table_args__ = (Index("ix_persona_feedback_version", "persona_version_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    persona_version_id: Mapped[str] = mapped_column(ForeignKey("persona_version.id", ondelete="RESTRICT"), nullable=False)
    interview_session_id: Mapped[str] = mapped_column(ForeignKey("persona_interview_session.id", ondelete="RESTRICT"), nullable=False)
    inbound_message_id: Mapped[str] = mapped_column(ForeignKey("chat_message.id", ondelete="RESTRICT"), nullable=False)
    reply_message_id: Mapped[str] = mapped_column(ForeignKey("chat_message.id", ondelete="RESTRICT"), nullable=False)
    action: Mapped[str] = mapped_column(String(24), nullable=False)
    change_points: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class PersonaApproval(Base):
    __tablename__ = "persona_approval"
    __table_args__ = (UniqueConstraint("persona_version_id", name="uq_persona_approval_version"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    persona_version_id: Mapped[str] = mapped_column(ForeignKey("persona_version.id", ondelete="RESTRICT"), nullable=False)
    interview_session_id: Mapped[str] = mapped_column(ForeignKey("persona_interview_session.id", ondelete="RESTRICT"), nullable=False)
    inbound_message_id: Mapped[str] = mapped_column(ForeignKey("chat_message.id", ondelete="RESTRICT"), nullable=False)
    reply_message_id: Mapped[str] = mapped_column(ForeignKey("chat_message.id", ondelete="RESTRICT"), nullable=False)
    actor_person_id: Mapped[str] = mapped_column(ForeignKey("memory_person.person_id", ondelete="RESTRICT"), nullable=False)
    version_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    config_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    approved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)

