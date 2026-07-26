"""Step 3: the worker — SKIP LOCKED claiming, retries, dead-lettering, and
crash recovery. Outbound HTTP is mocked with respx; the database is real."""

import asyncio
from datetime import UTC, datetime, timedelta

import respx
from httpx import Response
from sqlalchemy import select

from app import worker
from app.db import SessionLocal
from app.models import Delivery


async def _states(event_id: str) -> list[tuple[str, int]]:
    async with SessionLocal() as db:
        rows = await db.execute(
            select(Delivery.status, Delivery.attempts).where(Delivery.event_id == event_id)
        )
        return list(rows.all())


async def _one_delivery(client, source, auth, url: str) -> str:
    await client.post(
        "/v1/destinations",
        json={"source_id": source["id"], "type": "http", "filter": "*",
              "config": {"url": url}},
    )
    r = await client.post(
        "/v1/track", json={"type": "t", "payload": {}},
        headers={**auth, "Idempotency-Key": url},
    )
    return r.json()["id"]


async def test_skip_locked_prevents_double_claim(client, source, auth):
    """The core correctness property: two concurrent claims of the same pool
    of deliveries must never return overlapping ids."""
    await client.post(
        "/v1/destinations",
        json={"source_id": source["id"], "type": "http", "filter": "*", "config": {}},
    )
    for i in range(20):
        await client.post(
            "/v1/track", json={"type": "t", "payload": {}},
            headers={**auth, "Idempotency-Key": f"c{i}"},
        )

    async def claim_10():
        async with SessionLocal() as db:
            return await worker.claim(db, limit=10)

    a, b = await asyncio.gather(claim_10(), claim_10())
    assert set(a).isdisjoint(set(b)), "two workers claimed the same delivery"
    assert len(set(a) | set(b)) == len(a) + len(b), "no id returned twice"


@respx.mock
async def test_retry_then_success(client, source, auth):
    route = respx.post("http://dest.test/hook")
    route.side_effect = [Response(500), Response(500), Response(200)]

    eid = await _one_delivery(client, source, auth, "http://dest.test/hook")
    for _ in range(20):
        async with SessionLocal() as db:
            ids = await worker.claim(db, limit=10)
        for did in ids:
            await worker.process_one(did)
        states = await _states(eid)
        if states and states[0][0] in ("delivered", "dead"):
            break
        await asyncio.sleep(0.05)

    assert states == [("delivered", 3)], "should recover on the 3rd attempt"


@respx.mock
async def test_dead_letter_after_max_attempts(client, source, auth):
    respx.post("http://dead.test/hook").mock(return_value=Response(500))

    eid = await _one_delivery(client, source, auth, "http://dead.test/hook")
    for _ in range(20):
        async with SessionLocal() as db:
            ids = await worker.claim(db, limit=10)
        for did in ids:
            await worker.process_one(did)
        states = await _states(eid)
        if states and states[0][0] == "dead":
            break
        await asyncio.sleep(0.05)

    assert states == [("dead", 3)], "must stop at retry_max_attempts, not retry forever"


async def test_stale_claim_is_reclaimed(client, source, auth):
    """A 'delivering' row past claim_timeout belongs to a worker that died
    mid-delivery — it must be eligible for claim again, not stuck forever."""
    r = await client.post(
        "/v1/destinations",
        json={"source_id": source["id"], "type": "http", "filter": "nomatch.*", "config": {}},
    )
    dest_id = r.json()["id"]
    ev = await client.post(
        "/v1/track", json={"type": "user.signed_up", "payload": {}}, headers=auth
    )
    event_id = ev.json()["id"]

    async with SessionLocal() as db:
        stale = Delivery(
            event_id=event_id, destination_id=dest_id, status="delivering",
            claimed_at=datetime.now(UTC) - timedelta(seconds=10),  # older than claim_timeout
        )
        db.add(stale)
        await db.commit()
        stale_id = stale.id

    async with SessionLocal() as db:
        claimed = await worker.claim(db, limit=10)
    assert stale_id in claimed, "an orphaned 'delivering' row should be reclaimable"
