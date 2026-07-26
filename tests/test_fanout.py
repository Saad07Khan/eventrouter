"""Step 2: fan-out — glob filter matching and transactional atomicity."""

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Delivery, Destination


async def _dest_filters_for(event_id: str) -> set[str]:
    async with SessionLocal() as db:
        rows = await db.execute(
            select(Destination.filter)
            .join(Delivery, Delivery.destination_id == Destination.id)
            .where(Delivery.event_id == event_id)
        )
        return set(rows.scalars().all())


async def test_fanout_matches_only_filters_that_apply(client, source, auth):
    for f in ["*", "user.*", "order.*"]:
        r = await client.post(
            "/v1/destinations",
            json={"source_id": source["id"], "type": "http", "filter": f, "config": {}},
        )
        assert r.status_code == 201

    r = await client.post(
        "/v1/track",
        json={"type": "user.signed_up", "payload": {}},
        headers={**auth, "Idempotency-Key": "e1"},
    )
    matched = await _dest_filters_for(r.json()["id"])

    assert matched == {"*", "user.*"}, "order.* must NOT match a user.* event"


async def test_duplicate_event_creates_no_extra_deliveries(client, source, auth):
    """Proxy for full crash-atomicity: a duplicate must roll back its OWN
    fan-out too, not just the event row — otherwise a retried request would
    leave orphan delivery rows with nothing to dedupe them."""
    await client.post(
        "/v1/destinations",
        json={"source_id": source["id"], "type": "http", "filter": "*", "config": {}},
    )
    body = {"type": "user.signed_up", "payload": {}}
    headers = {**auth, "Idempotency-Key": "e2"}

    r1 = await client.post("/v1/track", json=body, headers=headers)
    r2 = await client.post("/v1/track", json=body, headers=headers)

    matched = await _dest_filters_for(r1.json()["id"])
    assert r1.json()["id"] == r2.json()["id"]
    assert len(matched) == 1, "the duplicate must not have added a second delivery row"
