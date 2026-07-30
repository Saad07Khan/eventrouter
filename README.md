# EventRouter

A backend service that reliably delivers events to multiple destinations.
Ingest an event once, fan it out to many destinations — each with its own
transform, batching, and independent retry state.

One event in, N differently-shaped deliveries out, N independent failure states,
with retries, exponential backoff, batching, and dead-lettering.
Think Segment / RudderStack, scoped to the interesting core.

Run the whole thing locally with `docker compose up` — see
[Running locally](#running-locally).

## The problem

A signup needs to reach Slack, a CRM, and a data warehouse. Calling all three
inline makes signup as slow as the slowest one, and a CRM outage either fails
the signup or gets silently swallowed by a bare `except: pass`. EventRouter
takes one event and guarantees each destination gets it — independently,
with retries, without touching the request path.

## Architecture

```
   Your app  ──▶  API SERVER  ──▶  202 Accepted (+ tracking id)
                      │  writes event + one delivery row per destination,
                      │  in ONE transaction
                      ▼
                 POSTGRES  (events + deliveries = the queue)
                      │
                      ▼
                   WORKER  ──▶  Webhook   (HMAC-signed, per-destination retry)
                           ──▶  Slack     (honors 429 Retry-After)
                           ──▶  Warehouse (batched: size or window, idempotent)
```

Two processes — an API server and a worker — sharing one Postgres and nothing
else. Run, restart, or crash either independently.

## Why these choices (the tradeoffs, not just the features)

**The queue is a Postgres table, not Redis/Celery.** Accepting an event and
fanning it out to N deliveries must be one atomic unit — if we wrote the event
to Postgres and enqueued a job in Redis separately, a crash between the two
would leave an accepted event with nowhere to go, which is exactly the silent
data loss this service exists to prevent. Two different systems can't share a
transaction; one database can.

**Two workers never claim the same delivery**, via
`SELECT ... FOR UPDATE SKIP LOCKED`: one worker locks the rows it claims, a
second worker skips past anything already locked instead of blocking on it.
Correct and non-blocking, with no extra coordination service.

**Retries use exponential backoff *with jitter***. Backoff alone means every
delivery that failed together retries together, in a synchronized wave — the
worst possible moment to hit a destination that's just recovering. Jitter
spreads that wave out.

**Batching flushes on size OR window**, whichever comes first. Size-only
means a dozen rows can wait forever for a 500th that never comes; window-only
means a traffic spike sends one enormous batch. Both triggers = full batches
when busy, on-schedule delivery when quiet.

**A batch retry re-sends the whole batch**, not just the failed rows, because
the sink write is idempotent (`ON CONFLICT DO NOTHING` keyed by delivery id).
Splitting a partially-failed batch into per-row retry state is real machinery
for a rare case; re-sending is simpler and free once writes are idempotent.

**Destination failures are isolated with per-destination concurrency caps and
a circuit breaker.** Without them, one destination that starts hanging fills
the worker's capacity with stuck deliveries, and healthy destinations stop
receiving anything. The cap bounds how much of the worker one destination can
occupy; the breaker stops calling a destination that's clearly down instead of
retrying it into the ground.

**Transform mappings are JMESPath, not user-supplied code.** Letting customers
upload arbitrary code to run on this server means sandboxing arbitrary
execution — a hard problem this project doesn't need to take on. JMESPath can
only reshape data; it cannot make it unsafe.

## What this is *not*

Not a consensus or distributed-transactions system — one Postgres is the
single source of truth, so there's no leader election, no quorum, no split
brain to handle. What it does solve: concurrent workers safely sharing one
queue, partial failure as the normal case, and at-least-once delivery with
idempotency to make retries safe.

## API

```
POST   /v1/sources                          register a source, get a write key (shown once)
POST   /v1/destinations                     register a destination (filter, transform, batching)
POST   /v1/track                            ingest an event (Idempotency-Key header)
GET    /v1/events/{id}                       event + every delivery's status and attempt history
POST   /v1/destinations/{id}/replay          reset dead deliveries back to pending
GET    /v1/destinations/{id}/stats           delivery counts by status, avg attempts
```

Full interactive docs at `/docs` (FastAPI's auto-generated OpenAPI UI).

## Walkthrough

```bash
# Register a source
curl -X POST $BASE/v1/sources -d '{"name": "web-app"}'
# -> {"id": "src_...", "write_key": "wk_..."}   (shown once)

# Register a destination
curl -X POST $BASE/v1/destinations -d '{
  "source_id": "src_...", "type": "http", "filter": "user.*",
  "config": {"url": "https://your-endpoint/hook", "secret": "shh"}
}'

# Send an event
curl -X POST $BASE/v1/track \
  -H "Authorization: Bearer wk_..." -H "Idempotency-Key: signup:u_123" \
  -d '{"type": "user.signed_up", "payload": {"user_id": "u_123"}}'
# -> 202 {"id": "evt_..."}

# Check what happened
curl $BASE/v1/events/evt_...
```

## Tests

```bash
pytest -q
```

18 tests against a real Postgres (no mocked database — only outbound HTTP is
mocked, with `respx`), covering the properties that actually matter:

| Test | Proves |
|---|---|
| Duplicate idempotency key | one event, not two, under a race |
| Fan-out filter matching | only matching destinations get a delivery |
| Duplicate track | the rollback covers fan-out too — no orphan deliveries |
| Concurrent claim | `SKIP LOCKED` — two workers never grab the same row |
| Retry then success | recovers after transient failures |
| Dead-letter after max attempts | stops retrying a destination that's truly gone |
| Stale claim reclaimed | a crashed worker's row is recoverable, not stuck forever |
| Transform reshape + validation | bad expressions rejected at creation, not at 3am |
| Batch flush on size / on window | both triggers work, and neither fires early |
| Circuit breaker opens | stops hammering a dead destination, defers instead of killing |
| Replay | dead deliveries can be recovered |

CI (`.github/workflows/ci.yml`) runs lint + this suite against Neon on every push.

## Tech

FastAPI · Postgres (Neon) · SQLAlchemy 2.0 (async) · Alembic · httpx · JMESPath
pytest + respx · ruff · GitHub Actions

## Running locally

Everything — Postgres, migrations, API, worker — in one command:

```bash
docker compose up
```

Then open http://localhost:8000/docs.

Compose runs the API and the worker as **separate services**, which is the
real architecture. To see the queue do its job, stop the worker and send some
events — they pile up as `pending` rows in Postgres — then start it again and
watch them drain:

```bash
docker compose stop worker
docker compose start worker
```

Without Docker:

```bash
python3.13 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env   # set DATABASE_URL (and DATABASE_SSL=false for local PG)

alembic upgrade head
uvicorn app.main:app --reload      # terminal 1: API  -> http://localhost:8000
python -m app.worker               # terminal 2: delivery worker
```

## Known limitations

- Circuit breaker state is in-process — fine for a single worker; running
  multiple workers means each tracks failures independently rather than
  sharing one circuit state.
- Batching and immediate delivery share one worker loop; a very large
  warehouse batch flush briefly delays the next immediate-delivery poll.
- Tests share the dev database (truncated per test) rather than a dedicated
  test database/branch — fine for a solo project, not how a team would run CI.
- The free-tier deploy co-locates the API and worker in one container
  (`start.sh`) for a single instance. If the worker subprocess dies, the API's
  health check stays green while deliveries silently stop — acceptable for a
  demo deploy, not for production. A real deployment runs `app.main` and
  `app.worker` as two separately-monitored services, which needs no code
  change, only a deploy config change.
