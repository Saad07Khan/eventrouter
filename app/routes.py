from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import generate_write_key, hash_key, require_source
from app.db import get_db
from app.fanout import build_deliveries, matching_destinations
from app.models import Delivery, Destination, Event, Source
from app.schemas import (
    DeliveryOut,
    DestinationCreate,
    DestinationCreated,
    DestinationStats,
    EventDetail,
    ReplayResult,
    SourceCreate,
    SourceCreated,
    TrackAccepted,
    TrackIn,
)
from app.transform import JMESPathError, validate_transform

router = APIRouter(prefix="/v1")


@router.post("/sources", response_model=SourceCreated, status_code=status.HTTP_201_CREATED)
async def create_source(body: SourceCreate, db: AsyncSession = Depends(get_db)):
    """Register a source. Returns a write key — shown here once and never again."""
    raw_key = generate_write_key()
    source = Source(name=body.name, write_key_hash=hash_key(raw_key))
    db.add(source)
    await db.commit()
    return SourceCreated(id=source.id, name=source.name, write_key=raw_key)


@router.post(
    "/destinations", response_model=DestinationCreated, status_code=status.HTTP_201_CREATED
)
async def create_destination(body: DestinationCreate, db: AsyncSession = Depends(get_db)):
    """Register a destination for a source. Matched against events by its filter."""
    if await db.get(Source, body.source_id) is None:
        raise HTTPException(status_code=404, detail="source not found")
    # Validate transform expressions now, so a typo fails here (with a human
    # watching) instead of silently producing nulls during delivery.
    try:
        validate_transform(body.transform)
    except (JMESPathError, ValueError, TypeError) as exc:
        raise HTTPException(status_code=422, detail=f"invalid transform: {exc}") from exc
    dest = Destination(
        source_id=body.source_id,
        type=body.type,
        config=body.config,
        filter=body.filter,
        transform=body.transform,
        batch_size=body.batch_size,
        batch_window_s=body.batch_window_s,
        enabled=body.enabled,
    )
    db.add(dest)
    await db.commit()
    return DestinationCreated(id=dest.id, type=dest.type, filter=dest.filter)


@router.post("/track", response_model=TrackAccepted, status_code=status.HTTP_202_ACCEPTED)
async def track(
    body: TrackIn,
    response: Response,
    source: Source = Depends(require_source),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db),
):
    """Accept an event and return 202 immediately.

    202 (not 200) is deliberate: we've taken responsibility for the event, but
    we have NOT delivered it yet. The status code is an honest promise.

    The event AND its delivery rows are written in ONE transaction. If we saved
    the event first and crashed before saving deliveries, we'd have an accepted
    event with nowhere to go — silent data loss, the exact thing this service
    exists to prevent. One transaction makes a half-done state impossible.
    """
    # Capture the id as a plain string NOW, while `source` is still loaded.
    # A rollback below expires every ORM object in the session; touching
    # `source.id` after that would trigger a lazy DB reload during attribute
    # access — sync IO with no async greenlet — and crash with MissingGreenlet.
    source_id = source.id

    event = Event(
        source_id=source_id,
        type=body.type,
        payload=body.payload,
        idempotency_key=idempotency_key,
    )
    db.add(event)
    try:
        # flush() sends the event INSERT (and trips the unique constraint on a
        # duplicate) WITHOUT committing yet. If it survives, we fan out into the
        # same open transaction, then commit event + deliveries together.
        await db.flush()
        destinations = await matching_destinations(db, source_id, body.type)
        db.add_all(build_deliveries(event.id, destinations))
        await db.commit()
        event_id = event.id
    except IntegrityError:
        # Duplicate (source_id, idempotency_key): the original event already
        # created its deliveries, so we roll this whole attempt back (no extra
        # deliveries) and return the event that won.
        await db.rollback()
        existing = await db.scalar(
            select(Event).where(
                Event.source_id == source_id,
                Event.idempotency_key == idempotency_key,
            )
        )
        event_id = existing.id
        response.status_code = status.HTTP_200_OK  # 200 = "already had it", not freshly accepted

    return TrackAccepted(id=event_id)


@router.get("/events/{event_id}", response_model=EventDetail)
async def get_event(event_id: str, db: AsyncSession = Depends(get_db)):
    """Everything about one event: its payload and every delivery's current
    state and history. This is what you hand a customer who says 'we never
    got it' — the honest answer, not a guess."""
    event = await db.get(Event, event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="event not found")
    rows = await db.execute(
        select(Delivery, Destination.type)
        .join(Destination, Delivery.destination_id == Destination.id)
        .where(Delivery.event_id == event_id)
    )
    deliveries = [
        DeliveryOut(
            id=d.id,
            destination_id=d.destination_id,
            destination_type=dest_type,
            status=d.status,
            attempts=d.attempts,
            last_error=d.last_error,
            delivered_at=d.delivered_at,
            next_attempt_at=d.next_attempt_at,
        )
        for d, dest_type in rows
    ]
    return EventDetail(
        id=event.id,
        type=event.type,
        payload=event.payload,
        received_at=event.received_at,
        deliveries=deliveries,
    )


@router.post("/destinations/{destination_id}/replay", response_model=ReplayResult)
async def replay_destination(destination_id: str, db: AsyncSession = Depends(get_db)):
    """Reset every 'dead' delivery for this destination back to pending, so the
    worker picks them up again. For recovering after an outage is fixed."""
    if await db.get(Destination, destination_id) is None:
        raise HTTPException(status_code=404, detail="destination not found")
    result = await db.execute(
        update(Delivery)
        .where(Delivery.destination_id == destination_id, Delivery.status == "dead")
        .values(status="pending", attempts=0, next_attempt_at=datetime.now(UTC))
    )
    await db.commit()
    return ReplayResult(replayed=result.rowcount)


@router.get("/destinations/{destination_id}/stats", response_model=DestinationStats)
async def destination_stats(destination_id: str, db: AsyncSession = Depends(get_db)):
    """Delivery counts by status, plus average attempts (a proxy for how
    flaky this destination has been)."""
    if await db.get(Destination, destination_id) is None:
        raise HTTPException(status_code=404, detail="destination not found")
    rows = await db.execute(
        select(Delivery.status, func.count(), func.avg(Delivery.attempts))
        .where(Delivery.destination_id == destination_id)
        .group_by(Delivery.status)
    )
    counts = {"pending": 0, "delivering": 0, "delivered": 0, "dead": 0}
    total_attempts = 0.0
    total_rows = 0
    for st, n, avg_attempts in rows:
        counts[st] = n
        total_attempts += (avg_attempts or 0) * n
        total_rows += n
    return DestinationStats(
        destination_id=destination_id,
        avg_attempts=(total_attempts / total_rows) if total_rows else None,
        **counts,
    )
