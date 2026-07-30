# EventRouter

Accept an event once, deliver it to many destinations, each with its own format
and its own retry state. Similar in shape to Segment or Hookdeck, scoped down to
the delivery engine.

```bash
docker compose up      # Postgres, migrations, API, and worker
```

## The problem

A signup needs to reach Slack, a CRM, and a data warehouse. Call all three
inline and signup becomes as slow as the slowest one. Worse, when the CRM
returns a 500 you either fail the signup or wrap it in `except: pass` and
silently lose leads for a month before anyone notices.

EventRouter takes the event off the request path and takes responsibility for
getting it everywhere it needs to go.

## How it works

```
POST /v1/track
      │
      ▼
┌─────────────┐
│ API SERVER  │ ──► 202 Accepted {"id": "evt_8f2a"}
└──────┬──────┘
       │   ONE transaction:
       │   1 event row + 1 delivery row per matching destination
       ▼
┌────────────────────────────────────────┐
│ POSTGRES                               │
│   events                               │
│   deliveries  (this table is the queue)│
└──────┬─────────────────────────────────┘
       │   claim with FOR UPDATE SKIP LOCKED
       ▼
┌─────────────┐
│   WORKER    │
└──┬───┬───┬──┘
   │   │   │
   │   │   └──► warehouse   bulk write, batched by size or time
   │   └──────► slack       honors 429 Retry-After
   └──────────► webhook     HMAC signed, backoff on failure
```

Two processes sharing one database and nothing else. Each delivery row carries
its own status, attempt count, and next retry time, so Slack failing has no
effect on the warehouse.

## Design decisions

**Queue lives in Postgres, not Redis or Celery.** Two systems cannot share one
transaction.

```
  Redis queue                       Postgres queue
  ───────────                       ──────────────
  INSERT event    ──► committed     BEGIN
       ✗ crash                        INSERT event
  ENQUEUE job     ──► never ran       INSERT deliveries
                                    COMMIT
  accepted event, nothing
  will ever deliver it              all or nothing
```

**Workers claim with `FOR UPDATE SKIP LOCKED`.** Both halves are load bearing.

```
                        worker A        worker B
  plain SELECT          takes 1-50      takes 1-50    same webhook twice
  FOR UPDATE            locks 1-50      blocks...     2 workers, 1x speed
  FOR UPDATE            locks 1-50      takes 51-100  ✓
    SKIP LOCKED
```

**Backoff carries jitter.**

```
  no jitter    500 fail at 9:00:00  ──►  all retry at 9:00:10   second outage
  jitter       500 fail at 9:00:00  ──►  spread over 9:00:05-15  ✓
```

**Batches flush on size or elapsed time, first one wins.**

```
  size only     12 rows wait forever for a 500th that never arrives
  window only   traffic spike ──► one 50,000 row write
  both          500 rows OR 30s   ✓
```

**A failed batch retries whole.** The sink write is idempotent
(`ON CONFLICT DO NOTHING` on delivery id), so rows that already landed cost
nothing to resend. Per-row state inside a batch is machinery for a rare case.

**Transforms are JMESPath, not user code.** Running customer code means
sandboxing arbitrary execution, a much harder problem than this needs.
JMESPath reshapes data and does nothing else.

**One slow destination cannot stall the others.** This took two fixes, and the
first one alone looked like it should have been enough.

```
  destination A hangs 30s, B and C healthy, 6 events each

  per-destination cap only     A: 0   B: 1   C: 1    loop still stuck
  + spawn deliveries as tasks  A: 0   B: 6   C: 6    ✓
```

The cap stops A occupying every worker slot. But the claim loop was awaiting
the whole batch, so one hanging request also froze it from picking up new work.

With a single Postgres as the source of truth there is no consensus, leader
election, or split brain here. The problems are concurrency and partial failure.

## API

```
POST  /v1/sources                     register a source, returns a write key (shown once)
POST  /v1/destinations                register a destination (filter, transform, batching)
POST  /v1/track                       ingest an event (Idempotency-Key header)
GET   /v1/events/{id}                 event plus every delivery's status and attempts
POST  /v1/destinations/{id}/replay    reset dead deliveries back to pending
GET   /v1/destinations/{id}/stats     counts by status, average attempts
```

Interactive docs at `/docs`.

```bash
# register a source, keep the write key
curl -X POST $BASE/v1/sources -d '{"name": "web-app"}'

# point it somewhere
curl -X POST $BASE/v1/destinations -d '{
  "source_id": "src_...", "type": "http", "filter": "user.*",
  "config": {"url": "https://your-endpoint/hook", "secret": "shh"}
}'

# send an event
curl -X POST $BASE/v1/track \
  -H "Authorization: Bearer wk_..." -H "Idempotency-Key: signup:u_123" \
  -d '{"type": "user.signed_up", "payload": {"user_id": "u_123"}}'

# see what happened to it
curl $BASE/v1/events/evt_...
```

## Tests

```bash
pytest -q      # 20 tests
```

They run against a real Postgres. Only outbound HTTP is mocked (`respx`), since
the properties worth proving here are database behaviour.

| Test | Proves |
|---|---|
| Duplicate idempotency key | one event under a concurrent retry, not two |
| Duplicate track | rollback covers the fan-out, leaving no orphan deliveries |
| Concurrent claim | two workers never claim the same delivery |
| Retry then success | transient failures recover |
| Dead-letter at max attempts | a gone destination stops consuming retries |
| Stale claim reclaimed | a crashed worker's row does not stay stuck |
| Batch flush on size, on window | both triggers fire, and neither fires early |
| Circuit breaker | a dead destination gets deferred, not hammered |

CI runs lint, migrations, and the suite on every push.

## Stack

FastAPI, Postgres (Neon), SQLAlchemy 2.0 async, Alembic, httpx, JMESPath,
pytest with respx, ruff, GitHub Actions.

## Running locally

`docker compose up` starts Postgres, applies migrations, then runs the API and
the worker as separate services. Open http://localhost:8000/docs.

To watch the queue actually queue, stop the worker, send a few events, and start
it again:

```bash
docker compose stop worker
docker compose start worker
```

Without Docker:

```bash
python3.13 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env          # set DATABASE_URL, and DATABASE_SSL=false for local PG

alembic upgrade head
uvicorn app.main:app --reload   # terminal 1
python -m app.worker            # terminal 2
```

## Limitations

Circuit breaker state lives in process memory, so running several workers means
each keeps its own view of which destinations are down rather than sharing one.

Batching and immediate delivery share a worker loop, so a large batch flush
briefly delays the next poll for immediate deliveries.

Tests truncate and reuse the development database instead of running against a
dedicated branch. Fine solo, not how a team would do it.

`start.sh` runs the API and worker in one container to fit on a single free
hosting instance. If the worker dies there the health check stays green while
deliveries quietly stop. Splitting them into two monitored services is a deploy
config change, not a code change, and `docker-compose.yml` already runs them
that way.
