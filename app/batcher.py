"""Batching for warehouse-style destinations (batch_size > 1).

The immediate worker ignores these (it only claims batch_size = 1). Here we hold
their deliveries until a batch is worth sending, then write them in bulk.

A batch flushes when EITHER trigger fires:
  - size:   enough pending deliveries have piled up, or
  - window: the oldest pending delivery has waited long enough.
Size alone would let a dozen rows wait forever for a 500th that never comes;
window alone would send one giant batch during a spike. Both = full trucks when
busy, and the truck still leaves on schedule when quiet.
"""

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.config import settings
from app.db import SessionLocal
from app.models import Delivery, Event, WarehouseEvent

log = logging.getLogger("worker")


# Warehouse destinations that have work to do (pending, or a batch orphaned by a
# crashed flush), with the counts needed to decide whether to flush now.
DUE_SQL = text(
    """
    SELECT dst.id            AS dest_id,
           dst.batch_size    AS batch_size,
           dst.batch_window_s AS window_s,
           count(*) FILTER (WHERE d.status = 'pending')          AS n_pending,
           min(d.created_at) FILTER (WHERE d.status = 'pending') AS oldest,
           count(*) FILTER (
               WHERE d.status = 'delivering'
                 AND d.claimed_at < now() - make_interval(secs => :ct)
           ) AS n_stale
    FROM destinations dst
    JOIN deliveries d ON d.destination_id = dst.id
    WHERE dst.enabled AND dst.batch_size > 1
      AND (
            d.status = 'pending'
         OR (d.status = 'delivering' AND d.claimed_at < now() - make_interval(secs => :ct))
      )
    GROUP BY dst.id, dst.batch_size, dst.batch_window_s
    """
)

# Claim up to batch_size rows for one destination (pending or stale), locking
# only these rows and skipping any another batcher already holds.
CLAIM_BATCH_SQL = text(
    """
    UPDATE deliveries
    SET status = 'delivering', claimed_at = now()
    WHERE id IN (
        SELECT id FROM deliveries
        WHERE destination_id = :dest
          AND (
                status = 'pending'
             OR (status = 'delivering' AND claimed_at < now() - make_interval(secs => :ct))
          )
        ORDER BY created_at
        FOR UPDATE SKIP LOCKED
        LIMIT :bs
    )
    RETURNING id, event_id
    """
)


def _is_due(row) -> bool:
    if row.n_stale > 0:  # orphaned batch from a crashed flush — always recover
        return True
    if row.n_pending >= row.batch_size:  # size trigger
        return True
    if row.oldest is not None:  # window trigger
        cutoff = datetime.now(UTC) - timedelta(seconds=row.window_s)
        return row.oldest <= cutoff
    return False


async def _flush_one(dest_id: str, batch_size: int) -> int:
    """Claim a batch for one destination, write it to the sink, mark delivered.

    The sink write and the status update happen in ONE transaction, so a batch
    is either fully recorded or not at all. The sink insert is idempotent, so a
    retried batch never double-writes.
    """
    async with SessionLocal() as db:
        claimed = (
            await db.execute(
                CLAIM_BATCH_SQL,
                {"dest": dest_id, "bs": batch_size, "ct": settings.claim_timeout_seconds},
            )
        ).all()
        await db.commit()

    if not claimed:
        return 0
    delivery_ids = [r.id for r in claimed]
    event_ids = [r.event_id for r in claimed]

    async with SessionLocal() as db:
        events = {
            e.id: e
            for e in await db.execute(
                select(Event.id, Event.type, Event.payload).where(Event.id.in_(event_ids))
            )
        }

    async with SessionLocal() as db:
        async with db.begin():
            rows = [
                {
                    "delivery_id": r.id,
                    "event_id": r.event_id,
                    "destination_id": dest_id,
                    "type": events[r.event_id].type,
                    "payload": events[r.event_id].payload,
                }
                for r in claimed
            ]
            # ON CONFLICT DO NOTHING -> writing the same batch twice is harmless.
            await db.execute(
                pg_insert(WarehouseEvent).values(rows).on_conflict_do_nothing(
                    index_elements=["delivery_id"]
                )
            )
            await db.execute(
                update(Delivery)
                .where(Delivery.id.in_(delivery_ids))
                .values(status="delivered", delivered_at=datetime.now(UTC), attempts=1)
            )

    log.info("flushed batch of %d to %s", len(delivery_ids), dest_id)
    return len(delivery_ids)


async def run_batches() -> int:
    """One tick of batching across all due warehouse destinations."""
    async with SessionLocal() as db:
        due = (await db.execute(DUE_SQL, {"ct": settings.claim_timeout_seconds})).all()

    flushed = 0
    for row in due:
        if _is_due(row):
            flushed += await _flush_one(row.dest_id, row.batch_size)
    return flushed
