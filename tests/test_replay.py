"""Step 8: replay — recover dead deliveries once a destination is fixed."""

from datetime import UTC, datetime

from app.db import SessionLocal
from app.models import Delivery


async def test_replay_resets_dead_deliveries_to_pending(client, source, auth):
    dest = (
        await client.post(
            "/v1/destinations",
            json={"source_id": source["id"], "type": "http", "filter": "*", "config": {}},
        )
    ).json()["id"]
    ev = (
        await client.post(
            "/v1/track", json={"type": "t", "payload": {}}, headers=auth
        )
    ).json()["id"]

    async with SessionLocal() as db:
        await db.execute(
            Delivery.__table__.update()
            .where(Delivery.event_id == ev)
            .values(status="dead", attempts=99)
        )
        await db.commit()

    r = await client.post(f"/v1/destinations/{dest}/replay")
    assert r.status_code == 200
    assert r.json()["replayed"] == 1

    async with SessionLocal() as db:
        row = (
            await db.execute(
                Delivery.__table__.select().where(Delivery.event_id == ev)
            )
        ).first()
    assert row.status == "pending"
    assert row.attempts == 0
    assert row.next_attempt_at <= datetime.now(UTC)


async def test_get_event_returns_full_delivery_history(client, source, auth):
    await client.post(
        "/v1/destinations",
        json={"source_id": source["id"], "type": "http", "filter": "*", "config": {}},
    )
    ev = (
        await client.post(
            "/v1/track", json={"type": "user.signed_up", "payload": {"a": 1}}, headers=auth
        )
    ).json()["id"]

    r = await client.get(f"/v1/events/{ev}")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == ev
    assert body["payload"] == {"a": 1}
    assert len(body["deliveries"]) == 1
    assert body["deliveries"][0]["status"] == "pending"


async def test_get_unknown_event_404s(client):
    r = await client.get("/v1/events/evt_does_not_exist")
    assert r.status_code == 404
