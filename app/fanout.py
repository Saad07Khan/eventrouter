from fnmatch import fnmatch

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Delivery, Destination


async def matching_destinations(
    db: AsyncSession, source_id: str, event_type: str
) -> list[Destination]:
    """Enabled destinations for this source whose filter matches the event type.

    Filters are glob patterns: "user.*" matches "user.signed_up", "*" matches all.
    """
    destinations = await db.scalars(
        select(Destination).where(
            Destination.source_id == source_id,
            Destination.enabled.is_(True),
        )
    )
    return [d for d in destinations if fnmatch(event_type, d.filter)]


def build_deliveries(event_id: str, destinations: list[Destination]) -> list[Delivery]:
    """One pending delivery row per matching destination."""
    return [Delivery(event_id=event_id, destination_id=d.id) for d in destinations]
