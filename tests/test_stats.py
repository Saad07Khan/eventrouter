"""Step 8: stats — delivery counts by status, and how flaky a destination is.

The endpoint had no coverage at all, and shipped a bug because of it: Postgres
avg() over an integer column returns numeric, asyncpg turns that into Decimal,
and adding a Decimal to a float raises TypeError. It only fires when the
destination actually has deliveries, which is why an empty-state test would
have stayed green. Every test here therefore asserts against real rows.
"""

from app.db import SessionLocal
from app.models import Delivery


async def _destination(client, source, **overrides):
    body = {"source_id": source["id"], "type": "http", "filter": "*", "config": {}}
    body.update(overrides)
    r = await client.post("/v1/destinations", json=body)
    assert r.status_code == 201
    return r.json()["id"]


async def test_stats_with_no_deliveries_is_all_zero(client, source):
    dest = await _destination(client, source)

    r = await client.get(f"/v1/destinations/{dest}/stats")

    assert r.status_code == 200
    assert r.json() == {
        "destination_id": dest,
        "pending": 0,
        "delivering": 0,
        "delivered": 0,
        "dead": 0,
        "avg_attempts": None,
    }


async def test_stats_counts_deliveries_and_averages_attempts(client, source, auth):
    """The regression test proper: avg() returns Decimal, so this 500s unmarshalled."""
    dest = await _destination(client, source)
    for _ in range(3):
        r = await client.post("/v1/track", json={"type": "t", "payload": {}}, headers=auth)
        assert r.status_code == 202

    # Three deliveries with 1, 2 and 3 attempts across two different statuses.
    async with SessionLocal() as db:
        rows = (
            await db.execute(
                Delivery.__table__.select().where(Delivery.destination_id == dest)
            )
        ).fetchall()
        assert len(rows) == 3
        statuses = ["delivered", "delivered", "dead"]
        for i, (row, status) in enumerate(zip(rows, statuses, strict=True)):
            await db.execute(
                Delivery.__table__.update()
                .where(Delivery.id == row.id)
                .values(status=status, attempts=i + 1)
            )
        await db.commit()

    r = await client.get(f"/v1/destinations/{dest}/stats")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["delivered"] == 2
    assert body["dead"] == 1
    assert body["pending"] == 0
    # (1 + 2 + 3) / 3
    assert body["avg_attempts"] == 2.0
    assert isinstance(body["avg_attempts"], float)


async def test_stats_for_unknown_destination_404s(client):
    r = await client.get("/v1/destinations/dst_does_not_exist/stats")
    assert r.status_code == 404
