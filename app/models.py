import secrets
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def _id(prefix: str) -> str:
    """Human-readable, sortable-ish ids like src_a1b2c3..., evt_9f8e7d..."""
    return f"{prefix}_{secrets.token_hex(8)}"


class Source(Base):
    """A customer/app that sends us events. Identified by a write key."""

    __tablename__ = "sources"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: _id("src"))
    name: Mapped[str] = mapped_column(String, nullable=False)
    # We store ONLY the sha256 of the write key, never the key itself — same
    # reasoning as password hashing. A stolen DB yields no usable keys.
    write_key_hash: Mapped[str] = mapped_column(String, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Event(Base):
    """One thing that happened, as reported by a source."""

    __tablename__ = "events"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: _id("evt"))
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"), nullable=False, index=True)
    type: Mapped[str] = mapped_column(String, nullable=False)  # e.g. "user.signed_up"
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)  # arbitrary shape
    idempotency_key: Mapped[str | None] = mapped_column(String, nullable=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    # The database enforces "one event per (source, idempotency_key)".
    # This is what makes dedup correct even when two requests race — the DB,
    # not our Python, arbitrates the tie. NULL keys are exempt (Postgres treats
    # NULLs as distinct), so events sent without a key are never deduped.
    __table_args__ = (
        UniqueConstraint("source_id", "idempotency_key", name="uq_events_source_idem"),
    )
