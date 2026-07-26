"""Step 6: batching — flush on size OR window, whichever first."""

import asyncio

from sqlalchemy import func, select

from app.batcher import run_batches
from app.db import SessionLocal
from app.models import WarehouseEvent


async def _warehouse_count(dest_id: str) -> int:
    async with SessionLocal() as db:
        return await db.scalar(
            select(func.count())
            .select_from(WarehouseEvent)
            .where(WarehouseEvent.destination_id == dest_id)
        )


async def test_batch_flushes_on_size(client, source, auth):
    r = await client.post(
        "/v1/destinations",
        json={
            "source_id": source["id"], "type": "warehouse", "filter": "*",
            "batch_size": 5, "batch_window_s": 3600,  # window won't fire in this test
        },
    )
    dest_id = r.json()["id"]
    for i in range(5):
        headers = {**auth, "Idempotency-Key": f"s{i}"}
        await client.post("/v1/track", json={"type": "m", "payload": {}}, headers=headers)

    await run_batches()

    assert await _warehouse_count(dest_id) == 5


async def test_batch_does_not_flush_before_size_or_window(client, source, auth):
    r = await client.post(
        "/v1/destinations",
        json={
            "source_id": source["id"], "type": "warehouse", "filter": "*",
            "batch_size": 100, "batch_window_s": 3600,
        },
    )
    dest_id = r.json()["id"]
    for i in range(3):
        headers = {**auth, "Idempotency-Key": f"w{i}"}
        await client.post("/v1/track", json={"type": "m", "payload": {}}, headers=headers)

    await run_batches()

    assert await _warehouse_count(dest_id) == 0, "must wait for size or window, not flush early"


async def test_batch_flushes_on_window(client, source, auth):
    r = await client.post(
        "/v1/destinations",
        json={
            "source_id": source["id"], "type": "warehouse", "filter": "*",
            "batch_size": 1000, "batch_window_s": 1,  # size won't fire; window will
        },
    )
    dest_id = r.json()["id"]
    for i in range(3):
        headers = {**auth, "Idempotency-Key": f"t{i}"}
        await client.post("/v1/track", json={"type": "m", "payload": {}}, headers=headers)

    await asyncio.sleep(1.2)  # let the window elapse
    await run_batches()

    assert await _warehouse_count(dest_id) == 3
