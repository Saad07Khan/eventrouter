import secrets
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
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
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # The database enforces "one event per (source, idempotency_key)".
    # This is what makes dedup correct even when two requests race — the DB,
    # not our Python, arbitrates the tie. NULL keys are exempt (Postgres treats
    # NULLs as distinct), so events sent without a key are never deduped.
    __table_args__ = (
        UniqueConstraint("source_id", "idempotency_key", name="uq_events_source_idem"),
    )


class Destination(Base):
    """A place an event should be delivered to, with its own delivery rules."""

    __tablename__ = "destinations"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: _id("dst"))
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"), nullable=False, index=True)
    type: Mapped[str] = mapped_column(String, nullable=False)  # "http" | "slack" | "warehouse"
    config: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)  # url, token, ...
    filter: Mapped[str] = mapped_column(String, nullable=False, default="*")  # e.g. "user.*"
    transform: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)  # Step 4
    batch_size: Mapped[int] = mapped_column(Integer, nullable=False, default=1)  # 1 = immediate
    batch_window_s: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Delivery(Base):
    """One event's journey to one destination. This row IS the queue item.

    Crucially there is one row PER (event, destination), each with its own
    status and retry state — so a failure to one destination never affects
    the others. Partial failure is the normal case, and this shape makes it
    natural instead of painful.
    """

    __tablename__ = "deliveries"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: _id("dlv"))
    event_id: Mapped[str] = mapped_column(ForeignKey("events.id"), nullable=False, index=True)
    destination_id: Mapped[str] = mapped_column(
        ForeignKey("destinations.id"), nullable=False, index=True
    )
    # pending -> delivering -> delivered | failed(->pending on retry) -> dead
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    # When a worker claimed this row (set to 'delivering'). Used to reclaim rows
    # orphaned by a worker that crashed mid-delivery: a 'delivering' row older
    # than claim_timeout is fair game again.
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(String, nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    # The worker's claim query filters on status and next_attempt_at, ordered by
    # next_attempt_at. This composite index is that query's access path.
    __table_args__ = (Index("ix_deliveries_claim", "status", "next_attempt_at"),)


class WarehouseEvent(Base):
    """The 'data warehouse' sink. Batched destinations write here in bulk.

    Stands in for an external warehouse / S3 — same DB, different table. Keyed by
    delivery_id so re-writing a batch is idempotent (ON CONFLICT DO NOTHING),
    which is what lets us retry a whole failed batch without double-inserting.
    """

    __tablename__ = "warehouse_events"

    delivery_id: Mapped[str] = mapped_column(String, primary_key=True)
    event_id: Mapped[str] = mapped_column(String, nullable=False)
    destination_id: Mapped[str] = mapped_column(String, nullable=False)
    type: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    written_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
