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

**The queue is a Postgres table rather than Redis or Celery.** Saving the event
and creating its delivery rows has to be one atomic step. Split across two
systems, a crash in between leaves an accepted event that nothing will ever
deliver, which is the exact failure this service exists to prevent. Two systems
cannot share a transaction. One database can.

**Concurrent workers claim work with `FOR UPDATE SKIP LOCKED`.** A worker locks
the rows it takes, and a second worker skips past locked rows instead of waiting
behind them. Without `SKIP LOCKED` two workers deliver the same webhook twice.
Without `FOR UPDATE` they block each other and running two is pointless.

**Retries back off exponentially with jitter.** Plain backoff means everything
that failed at 9:00 retries at 9:00:10 together, hitting a recovering server
with a synchronized wave. Randomising each delay spreads them out.

**Batches flush on size or on elapsed time, whichever comes first.** Size alone
lets twelve rows wait forever for a five hundredth that never arrives. Time
alone turns a traffic spike into one enormous write.

**A failed batch is retried whole, not split into per-row state.** The sink
write is idempotent (`ON CONFLICT DO NOTHING` on delivery id), so re-sending
rows that already landed costs nothing. Tracking which rows in a batch failed
is real machinery for a rare case.

**Transforms are JMESPath expressions, not user-supplied code.** Running
customer code on your own server means sandboxing it, and that is a much harder
problem than this project needs. JMESPath can reshape data and nothing else.

**One bad destination cannot stall the others.** Two things are needed here, and
I found that out the hard way. A per-destination concurrency cap stops one
endpoint from occupying every worker slot. Separately, the claim loop spawns
deliveries as tasks instead of awaiting the batch, because awaiting meant a
single hanging request froze the loop from picking up new work at all. Fixing
only the first one is not enough.

Worth being clear about what this is not: with a single Postgres as the source
of truth there is no consensus, leader election, or split brain to handle. The
problems here are concurrency and partial failure.

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
