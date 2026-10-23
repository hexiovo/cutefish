"""Persisted owner-controlled proactivity gates; no schedule or queue state."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from cutefish.core.models import Base, utc_now


class ProactivityPolicy(Base):
    __tablename__ = "proactivity_policy"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False, default="manual")
    explicit_pause: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    dnd_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    manual_takeover: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    updated_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
