"""Step 4: transform — validated at creation, applied at delivery."""

import asyncio

import respx
from httpx import Response

from app import worker
from app.db import SessionLocal


async def test_invalid_transform_rejected_at_creation(client, source):
    r = await client.post(
        "/v1/destinations",
        json={
            "source_id": source["id"], "type": "http", "filter": "*",
            "config": {"url": "http://x.test"},
            "transform": {"x": "user..email"},  # invalid JMESPath (double dot)
        },
    )
    assert r.status_code == 422


@respx.mock
async def test_transform_reshapes_payload_before_delivery(client, source, auth):
    route = respx.post("http://dest.test/hook").mock(return_value=Response(200))
    await client.post(
        "/v1/destinations",
        json={
            "source_id": source["id"], "type": "http", "filter": "*",
            "config": {"url": "http://dest.test/hook"},
            "transform": {"distinct_id": "user_id", "tier": "plan"},
        },
    )
    await client.post(
        "/v1/track",
        json={"type": "t", "payload": {"user_id": "u_9", "plan": "enterprise", "extra": "drop me"}},
        headers=auth,
    )
    for _ in range(20):
        async with SessionLocal() as db:
            ids = await worker.claim(db, limit=10)
        for did in ids:
            await worker.process_one(did)
        if route.called:
            break
        await asyncio.sleep(0.05)

    assert route.called
    sent = route.calls.last.request
    import json

    body = json.loads(sent.content)
    assert body == {"distinct_id": "u_9", "tier": "enterprise"}, "must reshape, dropping extras"
