"""Destination config that cannot possibly work is rejected at creation.

batch_size is not a tuning knob, it is a router: worker.py claims only
batch_size = 1 rows and hands them to a deliverer, batcher.py claims
batch_size > 1 rows and writes them to the warehouse sink. So a type that
disagrees with its batch_size gets delivered somewhere nobody asked for, and
both directions used to be accepted silently.
"""


async def _create(client, source, **overrides):
    body = {"source_id": source["id"], "type": "http", "filter": "*", "config": {}}
    body.update(overrides)
    return await client.post("/v1/destinations", json=body)


async def test_warehouse_without_batching_is_rejected(client, source):
    """Used to be accepted, then dead-lettered with 'no deliverer for type warehouse'."""
    r = await _create(client, source, type="warehouse")  # batch_size defaults to 1

    assert r.status_code == 422
    assert "batch_size" in r.json()["detail"]


async def test_batched_http_destination_is_rejected(client, source):
    """The dangerous one: this used to be written to the warehouse table and
    marked delivered, so the URL was never called but the event looked sent."""
    r = await _create(client, source, type="http", config={"url": "https://x.test"}, batch_size=5)

    assert r.status_code == 422
    assert "batch_size" in r.json()["detail"]


async def test_warehouse_with_batching_is_accepted(client, source):
    r = await _create(client, source, type="warehouse", batch_size=5, batch_window_s=30)

    assert r.status_code == 201
    assert r.json()["type"] == "warehouse"


async def test_plain_http_destination_is_accepted(client, source):
    r = await _create(client, source, config={"url": "https://x.test"})

    assert r.status_code == 201
    assert r.json()["type"] == "http"
