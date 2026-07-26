"""Step 1: accepting events — auth and idempotency."""


async def test_duplicate_idempotency_key_returns_same_event(client, auth):
    body = {"type": "user.signed_up", "payload": {"user_id": "u_1"}}
    headers = {**auth, "Idempotency-Key": "signup:u_1"}

    r1 = await client.post("/v1/track", json=body, headers=headers)
    r2 = await client.post("/v1/track", json=body, headers=headers)

    assert r1.status_code == 202  # freshly accepted
    assert r2.status_code == 200  # "already had it"
    assert r1.json()["id"] == r2.json()["id"], "must be the SAME event, not a duplicate"


async def test_no_idempotency_key_creates_distinct_events(client, auth):
    body = {"type": "user.signed_up", "payload": {"user_id": "u_2"}}

    r1 = await client.post("/v1/track", json=body, headers=auth)
    r2 = await client.post("/v1/track", json=body, headers=auth)

    assert r1.status_code == r2.status_code == 202
    assert r1.json()["id"] != r2.json()["id"], "no key -> no dedup, by design"


async def test_invalid_write_key_rejected(client):
    r = await client.post(
        "/v1/track",
        json={"type": "x", "payload": {}},
        headers={"Authorization": "Bearer wk_not_a_real_key"},
    )
    assert r.status_code == 401
