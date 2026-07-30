"""Root and health endpoints. Both must stay cheap and DB-free — the
platform's health check hits them, so a database hiccup must not make the
service look dead."""


async def test_root_describes_the_service(client):
    r = await client.get("/")
    assert r.status_code == 200
    body = r.json()
    assert body["service"] == "EventRouter"
    assert body["docs"] == "/docs"


async def test_health_returns_ok(client):
    r = await client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"ok": True}
