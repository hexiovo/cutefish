"""Non-secret NapCat account configuration and explicit owner identity."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from cutefish.core.models import Base, utc_now


class QqAccount(Base):
    __tablename__ = "qq_account"

    account_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    service_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    secret_name: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    webhook_secret_name: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    owner_sender_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    group_chat_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False, default="needs_credentials")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)


class QqContactIdentity(Base):
    __tablename__ = "qq_contact_identity"
    __table_args__ = (UniqueConstraint("account_id", "sender_id", name="uq_qq_account_sender"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("qq_account.account_id", ondelete="RESTRICT"), nullable=False, index=True)
    person_id: Mapped[str] = mapped_column(ForeignKey("memory_person.person_id", ondelete="RESTRICT"), nullable=False, index=True)
    sender_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    is_owner: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_group: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class QqInboundEvent(Base):
    __tablename__ = "qq_inbound_event"
    __table_args__ = (
        UniqueConstraint("account_id", "message_id", "event_type", name="uq_qq_inbound_event_source"),
        Index("ix_qq_inbound_person_state_created", "person_id", "state", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("qq_account.account_id", ondelete="RESTRICT"), nullable=False)
    message_id: Mapped[str] = mapped_column(String(128), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    message_type: Mapped[str] = mapped_column(String(24), nullable=False)
    sender_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    person_id: Mapped[str | None] = mapped_column(ForeignKey("memory_person.person_id", ondelete="RESTRICT"), nullable=True)
    conversation_id: Mapped[str | None] = mapped_column(ForeignKey("conversation.id", ondelete="RESTRICT"), nullable=True)
    chat_request_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    batch_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    batch_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    normalized_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="received", index=True)
    reason: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)
