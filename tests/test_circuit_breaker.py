"""Step 7: circuit breaker — stop hammering a destination that's clearly down."""

import asyncio
from datetime import UTC, datetime, timedelta

import respx
from httpx import Response
from sqlalchemy import select

from app import worker
from app.db import SessionLocal
from app.models import Delivery


@respx.mock
async def test_circuit_opens_and_defers_instead_of_dead_lettering(client, source, auth):
    route = respx.post("http://always-down.test/hook").mock(return_value=Response(500))

    dest = (
        await client.post(
            "/v1/destinations",
            json={"source_id": source["id"], "type": "http", "filter": "*",
                  "config": {"url": "http://always-down.test/hook"}},
        )
    ).json()["id"]
    for i in range(8):
        await client.post(
            "/v1/track", json={"type": "t", "payload": {}},
            headers={**auth, "Idempotency-Key": f"cb{i}"},
        )

    # Drain repeatedly — enough ticks for the breaker (threshold=3, from
    # fast_settings) to trip well before every delivery exhausts its retries.
    for _ in range(30):
        async with SessionLocal() as db:
            ids = await worker.claim(db, limit=20)
        for did in ids:
            await worker.process_one(did)
        await asyncio.sleep(0.05)

    async with SessionLocal() as db:
        rows = (
            await db.execute(
                select(Delivery.status, Delivery.next_attempt_at).where(
                    Delivery.destination_id == dest
                )
            )
        ).all()

    dead = sum(1 for s, _ in rows if s == "dead")
    deferred = sum(
        1 for s, nxt in rows if s == "pending" and nxt > datetime.now(UTC) + timedelta(seconds=10)
    )

    assert worker._breaker.is_open(dest), "circuit should be open after repeated failures"
    assert dead == 0, "an open circuit should defer work, not let it dead-letter"
    assert deferred >= 6, "most deliveries should be pushed out to the cooldown"
    assert route.call_count < 8 * 3, "the breaker should cut off calls well before max_attempts * N"
