"""Shared fixtures.

Runs against the REAL Postgres in DATABASE_URL (Neon) — not mocks, not
sqlite — because the things this project needs to prove (SKIP LOCKED,
unique-constraint dedup, transactional fan-out) only mean something against
a real database. `clean_db` truncates before every test, so tests are
independent but the database itself is real.

This intentionally reuses the dev database rather than a separate test
database/branch. Fine for a solo project; a team project would point
DATABASE_URL at a dedicated Neon branch in CI instead.
"""

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app import worker
from app.config import settings
from app.db import SessionLocal, engine
from app.main import app

TABLES = "warehouse_events, deliveries, events, destinations, sources"


@pytest_asyncio.fixture(autouse=True)
async def clean_db():
    async with SessionLocal() as db:
        await db.execute(text(f"TRUNCATE {TABLES} CASCADE"))
        await db.commit()
    yield
    # pytest-asyncio gives each test its own event loop, but pooled asyncpg
    # connections stay bound to the loop they were opened on. Without this,
    # the next test's loop reuses a connection tied to a dead loop and dies
    # with "attached to a different loop". Disposing forces fresh connections.
    await engine.dispose()


@pytest.fixture(autouse=True)
def fast_settings(monkeypatch):
    """Seconds-scale retry/backoff/circuit timing instead of the real
    minutes-to-hours values, so tests run in under a second each."""
    monkeypatch.setattr(settings, "retry_base_seconds", 0.05)
    monkeypatch.setattr(settings, "retry_cap_seconds", 0.2)
    monkeypatch.setattr(settings, "retry_max_attempts", 3)
    monkeypatch.setattr(settings, "claim_timeout_seconds", 1.0)
    # worker._breaker was constructed at import time with the real defaults;
    # patch its instance attributes directly so tests see the fast values too.
    monkeypatch.setattr(worker._breaker, "threshold", 3)
    monkeypatch.setattr(worker._breaker, "cooldown", 60.0)
    worker._breaker._fails.clear()
    worker._breaker._open_until.clear()


@pytest_asyncio.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest_asyncio.fixture
async def source(client):
    r = await client.post("/v1/sources", json={"name": "test-source"})
    assert r.status_code == 201
    return r.json()


@pytest.fixture
def auth(source):
    return {"Authorization": f"Bearer {source['write_key']}"}
