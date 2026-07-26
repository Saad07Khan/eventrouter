from fastapi import APIRouter, Depends, Header, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import generate_write_key, hash_key, require_source
from app.db import get_db
from app.models import Event, Source
from app.schemas import SourceCreate, SourceCreated, TrackAccepted, TrackIn

router = APIRouter(prefix="/v1")


@router.post("/sources", response_model=SourceCreated, status_code=status.HTTP_201_CREATED)
async def create_source(body: SourceCreate, db: AsyncSession = Depends(get_db)):
    """Register a source. Returns a write key — shown here once and never again."""
    raw_key = generate_write_key()
    source = Source(name=body.name, write_key_hash=hash_key(raw_key))
    db.add(source)
    await db.commit()
    return SourceCreated(id=source.id, name=source.name, write_key=raw_key)


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
        await db.commit()
        event_id = event.id  # not expired (expire_on_commit=False)
    except IntegrityError:
        # Another request already inserted this (source_id, idempotency_key).
        # We let the DB's unique constraint arbitrate the race, then return the
        # event that won. This is why dedup is correct even under concurrency:
        # a check-then-insert in Python could not do this safely.
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
