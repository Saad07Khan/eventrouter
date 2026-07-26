"""The delivery worker.

A separate process from the API. Its loop is small:

    forever:
        claim a batch of due deliveries   (SKIP LOCKED)
        for each: load it, POST it, record the outcome
        sleep only when there was nothing to do

The API accepts events; this drains them. They share one Postgres and nothing
else, so you can run, restart, or crash either independently.
"""

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import Row, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.backoff import compute_delay
from app.config import settings
from app.db import SessionLocal
from app.destinations import DELIVERERS, DeliveryResult
from app.models import Delivery, Destination, Event
from app.transform import apply_transform

logging.basicConfig(level=logging.INFO, format="%(asctime)s worker %(message)s")
log = logging.getLogger("worker")


# The heart of the worker. Two workers must never grab the same row, and neither
# should block on the other:
#   FOR UPDATE OF d  -> lock the delivery rows we pick
#   SKIP LOCKED      -> if another worker already locked a row, skip past it
# We only claim rows for immediate destinations (batch_size = 1); batched ones
# (Step 6) are the batcher's job. We also reclaim rows stuck in 'delivering'
# past claim_timeout — those belong to a worker that died mid-delivery.
CLAIM_SQL = text(
    """
    UPDATE deliveries
    SET status = 'delivering', claimed_at = now()
    WHERE id IN (
        SELECT d.id
        FROM deliveries d
        JOIN destinations dst ON dst.id = d.destination_id
        WHERE dst.batch_size = 1
          AND (
                (d.status = 'pending' AND d.next_attempt_at <= now())
             OR (d.status = 'delivering'
                 AND d.claimed_at < now() - make_interval(secs => :claim_timeout))
          )
        ORDER BY d.next_attempt_at
        FOR UPDATE OF d SKIP LOCKED
        LIMIT :limit
    )
    RETURNING id
    """
)


async def claim(db: AsyncSession) -> list[str]:
    """Mark a batch of due deliveries as 'delivering' and return their ids.

    Committed immediately so the claim is durable and the lock is released
    before we do any slow network work.
    """
    result = await db.execute(
        CLAIM_SQL,
        {"limit": settings.claim_batch, "claim_timeout": settings.claim_timeout_seconds},
    )
    ids = [row[0] for row in result]
    await db.commit()
    return ids


async def load(db: AsyncSession, delivery_id: str) -> Row | None:
    """Fetch the scalar values needed to deliver one row (no ORM objects, so the
    data is safe to use after the session closes)."""
    return (
        await db.execute(
            select(
                Delivery.id,
                Delivery.attempts,
                Destination.type,
                Destination.config,
                Destination.transform,
                Event.payload,
            )
            .join(Event, Delivery.event_id == Event.id)
            .join(Destination, Delivery.destination_id == Destination.id)
            .where(Delivery.id == delivery_id)
        )
    ).first()


async def deliver(row: Row) -> DeliveryResult:
    """Transform the payload and hand it to the right destination function.
    Any unexpected exception becomes a retryable failure, never a crash."""
    deliverer = DELIVERERS.get(row.type)
    if deliverer is None:
        return DeliveryResult(ok=False, error=f"no deliverer for type '{row.type}'")
    try:
        payload = apply_transform(row.transform, row.payload)
        return await deliverer(payload, row.config)
    except Exception as exc:  # never let one bad delivery kill the worker
        return DeliveryResult(ok=False, error=f"deliverer raised: {type(exc).__name__}: {exc}")


async def record_result(db: AsyncSession, delivery_id: str, attempts: int, result: DeliveryResult):
    """Write the outcome: delivered, scheduled for retry, or dead."""
    attempts += 1
    if result.ok:
        values = {
            "status": "delivered",
            "attempts": attempts,
            "delivered_at": datetime.now(UTC),
            "last_error": None,
        }
    elif attempts >= settings.retry_max_attempts:
        # Out of retries. Some destinations are simply gone; stop trying and let
        # a human replay it later (Step 8) rather than pile up doomed work.
        values = {"status": "dead", "attempts": attempts, "last_error": result.error}
    else:
        delay = compute_delay(attempts, result.retry_after)
        values = {
            "status": "pending",
            "attempts": attempts,
            "next_attempt_at": datetime.now(UTC) + timedelta(seconds=delay),
            "last_error": result.error,
        }
    await db.execute(update(Delivery).where(Delivery.id == delivery_id).values(**values))
    await db.commit()


async def process_one(delivery_id: str) -> None:
    """Load -> deliver -> record, each in its own short transaction so no DB
    transaction is held open during the (slow) network call."""
    async with SessionLocal() as db:
        row = await load(db, delivery_id)
    if row is None:
        return
    result = await deliver(row)
    async with SessionLocal() as db:
        await record_result(db, row.id, row.attempts, result)
    log.info("delivery %s -> %s%s", delivery_id, "ok" if result.ok else "fail",
             "" if result.ok else f" ({result.error})")


async def run_once() -> int:
    """One tick: claim a batch and process it. Returns how many were claimed."""
    async with SessionLocal() as db:
        ids = await claim(db)
    for delivery_id in ids:
        await process_one(delivery_id)
    return len(ids)


async def main() -> None:
    log.info("started (poll=%ss, max_attempts=%s)",
             settings.poll_interval_seconds, settings.retry_max_attempts)
    while True:
        processed = await run_once()
        # Drain immediately while there's work; back off when idle to spare the DB
        # (Neon suspends when idle, so we don't want to poll aggressively).
        if processed == 0:
            await asyncio.sleep(settings.poll_interval_seconds)


if __name__ == "__main__":
    asyncio.run(main())
