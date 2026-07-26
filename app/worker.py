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
from app.batcher import run_batches
from app.circuit import CircuitBreaker
from app.config import settings
from app.db import SessionLocal
from app.destinations import DELIVERERS, DeliveryResult
from app.models import Delivery, Destination, Event
from app.transform import apply_transform

logging.basicConfig(level=logging.INFO, format="%(asctime)s worker %(message)s")
log = logging.getLogger("worker")

# Backpressure: cap total in-flight deliveries, and in-flight per destination, so
# one slow destination can't monopolize the worker and stall everyone else.
_global_sem = asyncio.Semaphore(settings.worker_concurrency)
_dest_sems: dict[str, asyncio.Semaphore] = {}
_breaker = CircuitBreaker(settings.circuit_fail_threshold, settings.circuit_cooldown_seconds)


def _dest_sem(dest_id: str) -> asyncio.Semaphore:
    sem = _dest_sems.get(dest_id)
    if sem is None:
        sem = asyncio.Semaphore(settings.destination_concurrency)
        _dest_sems[dest_id] = sem
    return sem


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


async def claim(db: AsyncSession, limit: int) -> list[str]:
    """Mark up to `limit` due deliveries as 'delivering' and return their ids.

    Committed immediately so the claim is durable and the lock is released
    before we do any slow network work.
    """
    result = await db.execute(
        CLAIM_SQL,
        {"limit": limit, "claim_timeout": settings.claim_timeout_seconds},
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
                Delivery.destination_id,
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


async def _defer_destination(dest_id: str) -> None:
    """When a circuit opens, push this destination's still-pending deliveries out
    to the cooldown time, so the claim query stops picking them up until then."""
    async with SessionLocal() as db:
        await db.execute(
            update(Delivery)
            .where(Delivery.destination_id == dest_id, Delivery.status == "pending")
            .values(next_attempt_at=_breaker.retry_at(dest_id))
        )
        await db.commit()


async def process_one(delivery_id: str) -> None:
    """Load -> (deliver) -> record. Each DB step is its own short transaction so
    no transaction is held during the network call. Delivery itself runs under
    the concurrency caps, and is skipped entirely if the circuit is open."""
    async with SessionLocal() as db:
        row = await load(db, delivery_id)
    if row is None:
        return
    dest_id = row.destination_id

    # Circuit open: don't deliver. Release the claim back to pending, deferred to
    # the cooldown, so it is neither lost nor re-claimed every tick.
    if _breaker.is_open(dest_id):
        async with SessionLocal() as db:
            await db.execute(
                update(Delivery)
                .where(Delivery.id == row.id)
                .values(status="pending", next_attempt_at=_breaker.retry_at(dest_id))
            )
            await db.commit()
        return

    # Acquire per-destination first, THEN global: a coroutine waiting on a busy
    # destination must not sit on a global slot and block other destinations.
    async with _dest_sem(dest_id), _global_sem:
        result = await deliver(row)

    async with SessionLocal() as db:
        await record_result(db, row.id, row.attempts, result)

    if _breaker.record(dest_id, result.ok):
        await _defer_destination(dest_id)
        log.info("circuit OPEN for %s (cooldown %ss)", dest_id, settings.circuit_cooldown_seconds)
    log.info("delivery %s -> %s%s", delivery_id, "ok" if result.ok else "fail",
             "" if result.ok else f" ({result.error})")


async def main() -> None:
    """Claim work and spawn each delivery as its own task, so the loop never
    blocks on a slow delivery. A slow destination ties up its own tasks (bounded
    by its per-destination cap) while the loop keeps claiming and delivering
    everyone else. This is what makes 'one slow destination' a non-event."""
    log.info("started (poll=%ss, max_attempts=%s, concurrency=%s)",
             settings.poll_interval_seconds, settings.retry_max_attempts,
             settings.worker_concurrency)
    in_flight: set[asyncio.Task] = set()
    while True:
        did_work = False
        # Only claim more if we have room, so in-flight work stays bounded.
        room = settings.claim_batch - len(in_flight)
        if room > 0:
            async with SessionLocal() as db:
                ids = await claim(db, room)
            for delivery_id in ids:
                task = asyncio.create_task(process_one(delivery_id))
                in_flight.add(task)
                task.add_done_callback(in_flight.discard)
            did_work = did_work or bool(ids)

        flushed = await run_batches()  # warehouse (batch_size > 1) destinations
        did_work = did_work or flushed > 0

        # Sleep only when truly idle (no work found and nothing in flight); tick
        # fast otherwise so completed deliveries free capacity promptly.
        if not did_work and not in_flight:
            await asyncio.sleep(settings.poll_interval_seconds)
        else:
            await asyncio.sleep(0.05)


if __name__ == "__main__":
    asyncio.run(main())
