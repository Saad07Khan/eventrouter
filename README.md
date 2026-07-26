# EventRouter

A backend service that reliably delivers events to multiple destinations.
Ingest an event once, fan it out to many destinations — each with its own
transform, batching, and independent retry state.

One event in, N differently-shaped deliveries out, N independent failure states,
with retries, exponential backoff, batching, and dead-lettering.
Think Segment / RudderStack, scoped to the interesting core.

## Architecture

```
   Your app  ──▶  API SERVER  ──▶  202 Accepted (+ tracking id)
                      │  writes event + one delivery row per destination,
                      │  in ONE transaction
                      ▼
                 POSTGRES  (events + deliveries = the queue)
                      │
                      ▼
                   WORKER  ──▶  Webhook   (1 at a time, HMAC-signed)
                           ──▶  Slack     (rate-limited)
                           ──▶  Warehouse (batches of 500 / 30s)
```

Two processes — an API server and a worker — sharing one Postgres and nothing
else. The queue is a database table, not Redis/Celery, because the enqueue must
be atomic with the event insert (otherwise a crash mid-write silently loses an
accepted event).

## Status

Built step by step. Currently: **Step 0 — skeleton + health check.**

## Tech

FastAPI · Postgres · SQLAlchemy 2.0 (async) · Alembic · httpx · JMESPath · pytest

## Running locally

```bash
python3.13 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

uvicorn app.main:app --reload      # http://localhost:8000/health
```
