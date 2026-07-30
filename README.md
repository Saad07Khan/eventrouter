# EventRouter

Accept an event once, deliver it to many destinations, each with its own format
and its own retry state. Same shape as Segment or Hookdeck, scoped to the
delivery engine.

Calling Slack, a CRM, and a warehouse inline makes signup as slow as the slowest
one, and a CRM outage either fails the signup or gets swallowed by `except:
pass`. This takes the event off the request path and owns getting it delivered.

## Quick start

```bash
docker compose up      # Postgres, migrations, API, worker
```

Docs at http://localhost:8000/docs.

```bash
# 1. register a source, keep the write key it returns
curl -X POST localhost:8000/v1/sources -d '{"name": "web-app"}'

# 2. point it somewhere
curl -X POST localhost:8000/v1/destinations -d '{
  "source_id": "src_...", "type": "http", "filter": "user.*",
  "config": {"url": "https://your-endpoint/hook", "secret": "shh"}
}'

# 3. send an event
curl -X POST localhost:8000/v1/track \
  -H "Authorization: Bearer wk_..." -H "Idempotency-Key: signup:u_123" \
  -d '{"type": "user.signed_up", "payload": {"user_id": "u_123"}}'

# 4. see what happened to it
curl localhost:8000/v1/events/evt_...
```

```
POST  /v1/sources                     register a source, write key shown once
POST  /v1/destinations                filter, transform, batching config
POST  /v1/track                       ingest (Idempotency-Key header)
GET   /v1/events/{id}                 status and attempts per destination
POST  /v1/destinations/{id}/replay    dead deliveries back to pending
GET   /v1/destinations/{id}/stats     counts by status, average attempts
```

## Stack

FastAPI, Postgres (Neon), SQLAlchemy 2.0 async, Alembic, httpx, JMESPath.
Tests with pytest and respx, linting with ruff, CI on GitHub Actions.

Compose runs the API and worker as separate services, which is the real
architecture. Stop the worker, send events, start it again, and watch the queue
drain.

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

Every delivery row carries its own status, attempt count, and next retry time,
so Slack failing has no effect on the warehouse.

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

**One slow destination cannot stall the others.** Two fixes, and the first alone
looked like it should have been enough.

```
  destination A hangs 30s, B and C healthy, 6 events each

  per-destination cap only     A: 0   B: 1   C: 1    loop still stuck
  + spawn deliveries as tasks  A: 0   B: 6   C: 6    ✓
```

The cap stops A occupying every worker slot. Separately, the claim loop was
awaiting the whole batch, so one hanging request froze it from taking new work.

Two smaller ones. A failed batch retries whole rather than tracking per-row
state, because the sink write is idempotent so resending costs nothing.
Transforms are JMESPath rather than user-supplied code, which would mean
sandboxing arbitrary execution.

## Tests

```bash
pytest -q      # 20 tests
```

Against a real Postgres, with only outbound HTTP mocked, since the properties
worth proving are database behaviour: idempotent ingest under a concurrent
retry, fan-out rolling back cleanly, two workers never claiming the same row,
retry to recovery, dead-lettering, stale claims from a crashed worker, both
batch flush triggers, and the circuit breaker deferring instead of hammering.

CI runs lint, migrations, and the suite on every push.
