# EventRouter: Complete Build and Study Guide

A full walkthrough of this project: the concepts, the syntax, every design
decision, and the reasoning behind each one. Written so you can rebuild it from
scratch without looking at the repo.

---

## How to use this document

Read Part 0 and 1 first. After that, either read straight through, or jump to
Part 4 and code along, dipping into Part 5 whenever a concept is unfamiliar.

Part 8 is interview prep. If you only have an hour before an interview, read
Part 8 and Part 7.

**Conventions in this doc.** Code blocks marked `# app/thing.py` are real files
in the project. Blocks with no filename are illustrative.

---

## Table of contents

| Part | Contents |
|---|---|
| 0 | Python and async prerequisites |
| 1 | What the product is and why it exists |
| 2 | The stack, and what each piece does |
| 3 | Database concepts you need |
| 4 | Building it, file by file |
| 5 | The hard concepts, in depth |
| 6 | Testing |
| 7 | Bugs hit while building, and what they teach |
| 8 | Interview preparation |
| 9 | Rebuild-from-scratch checklist |
| A | Glossary, SQL reference, file map |

---
---

# Part 0: Python and async prerequisites

Skip if you already know async Python and type hints.

## 0.1 Type hints

Python does not enforce types at runtime, but modern backend code annotates
everything. FastAPI and SQLAlchemy both *use* these annotations to do real work,
so they are not decoration here.

```python
def greet(name: str) -> str:        # takes a str, returns a str
    return f"hello {name}"

count: int = 0
names: list[str] = []
lookup: dict[str, int] = {}
maybe: str | None = None            # either a str or None
```

`str | None` is the modern spelling of `Optional[str]`. You will see it
constantly in this project, because most database columns are nullable.

## 0.2 Async and await

Normal Python runs one thing at a time. When you call a database or an HTTP API,
your program sits and waits, doing nothing, for milliseconds or seconds. That is
dead time.

Async lets a single thread work on something else during that wait.

```python
import asyncio

async def fetch(name: str) -> str:      # async def = a coroutine function
    await asyncio.sleep(1)              # await = "I am waiting, run something else"
    return name

async def main():
    # Sequential: 3 seconds total
    a = await fetch("a")
    b = await fetch("b")
    c = await fetch("c")

    # Concurrent: 1 second total
    a, b, c = await asyncio.gather(fetch("a"), fetch("b"), fetch("c"))

asyncio.run(main())                     # entry point, starts the event loop
```

Three rules that cover almost everything:

1. `await` only works inside an `async def`.
2. Calling an async function without `await` gives you a coroutine object that
   never runs. This is the single most common async bug.
3. Blocking calls (`time.sleep`, `requests.get`, heavy CPU work) freeze the
   entire event loop. In async code you use `asyncio.sleep` and `httpx`, not
   `time.sleep` and `requests`.

**The event loop** is the scheduler. It keeps a list of coroutines, runs one
until it hits an `await`, parks it, and runs the next. One thread, many
in-flight operations. This is why a single worker process here can have dozens
of HTTP deliveries in the air at once.

## 0.3 Async context managers

```python
async with SessionLocal() as db:
    ...                    # db is open here
# db is closed here, even if the block raised
```

`async with` is `with` for things whose setup or teardown is itself async, like
a database session or an HTTP client. You will see this on nearly every database
call in this project.

## 0.4 asyncio.Semaphore

A counter of permits. Used in Part 5 for backpressure.

```python
sem = asyncio.Semaphore(5)     # 5 permits

async with sem:                # take one, wait if none are free
    await do_work()            # at most 5 coroutines are ever inside this block
                               # permit returned automatically on exit
```

## 0.5 asyncio.create_task

`await coro()` runs it and waits. `asyncio.create_task(coro())` starts it in the
background and returns immediately. That difference is the entire fix to the
backpressure bug in Part 7.

```python
task = asyncio.create_task(slow_thing())   # starts now, does not block
# ... other work happens here while slow_thing runs
await task                                  # wait for it, if you want to
```

One catch: you must keep a reference to the task, or Python may garbage-collect
it mid-flight. That is why the worker holds an `in_flight` set.

---
---

# Part 1: The product

## 1.1 The problem, concretely

A user signs up on an e-commerce site. Five systems need to know:

| System | Why |
|---|---|
| Mailchimp | welcome email |
| HubSpot | create a sales lead |
| Mixpanel | analytics |
| Data warehouse | reporting |
| Slack | ping the team, enterprise plans only |

The naive implementation puts five API calls in the signup handler:

```python
@app.post("/signup")
async def signup(data):
    user = await create_user(data)
    await mailchimp.subscribe(user.email)      # 200ms
    await hubspot.create_contact(user)         # 400ms
    await mixpanel.track("signup", user.id)    # 150ms
    await warehouse.insert(user.as_row())      # 300ms
    await slack.post(f"New signup: {user.email}")
    return user
```

Three problems:

1. **Slow.** Signup now takes 1.3 seconds instead of 50ms, because the user
   waits on five third-party services.
2. **Fragile.** HubSpot returns a 500 and the whole signup fails. So someone
   wraps it in `try/except: pass`, and leads silently stop flowing for three
   weeks before anyone notices.
3. **Coupled.** Marketing wants a sixth tool. That is a ticket, an engineer, a
   code review, and a deploy.

## 1.2 What EventRouter does instead

The app fires one event and moves on:

```python
@app.post("/signup")
async def signup(data):
    user = await create_user(data)
    await pipeline.track("user.signed_up", {"user_id": user.id, "email": user.email})
    return user
```

EventRouter stores it, replies `202 Accepted` in about 5ms, and takes over.
It delivers to every configured destination, in that destination's format, with
its own retry schedule. A sixth tool becomes one API call, no deploy.

## 1.3 The mental model: a courier company

Use this throughout. It maps exactly.

| Piece | Courier equivalent |
|---|---|
| API server | Front desk. Takes the parcel, hands you a tracking number, walks away. Never delivers anything. |
| Postgres | The back room. The ledger, plus the shelf of parcels waiting to go out. |
| `deliveries` table | The shelf of delivery slips. One slip per parcel per recipient. |
| Worker | The driver. Takes slips off the shelf, drives out, logs what happened. |
| Retry with backoff | Nobody home. Come back in 10 minutes, then an hour, then tomorrow. |
| Dead letter | After 8 tries the parcel goes in the undeliverable bin for a human. |
| Circuit breaker | The building is on fire. Stop sending drivers for a while. |

**Why the front desk and the driver are separate programs:** accepting takes
milliseconds, delivering takes seconds. If the front desk had to drive each
parcel out before serving the next customer, the queue would be out the door.

## 1.4 The life of one event

```
T+0ms     app calls track()
T+4ms     API writes 1 event row + 3 delivery rows in ONE transaction,
          returns 202. The user's signup is DONE.
T+2.1s    worker claims 2 of the 3 (the third is a batched destination)
T+2.3s    Slack     ✓ delivered
T+2.4s    CRM       ✗ HTTP 500  -> retry scheduled for T+14s
T+14s     CRM       ✓ delivered (recovered, nobody was paged)
T+30s     Warehouse ✓ flushed in a batch with 46 other events
```

Notice: **three independent fates from one event.** Slack finished in 2.3
seconds. The CRM failed and recovered. The warehouse waited to fill a truck.
None of them affected the others. That independence is the product.

---
---

# Part 2: The stack

## 2.1 Every dependency and why

| Package | Role | Why this one |
|---|---|---|
| `fastapi` | Turns Python functions into HTTP endpoints | Async native, validates with type hints, generates OpenAPI docs free |
| `uvicorn` | The actual server process | Standard ASGI server. FastAPI defines *what* to answer; uvicorn *receives* the request |
| `sqlalchemy[asyncio]` | ORM, Python classes to SQL tables | 2.0's typed `Mapped[]` syntax, real async support |
| `asyncpg` | Postgres driver | Fastest async Postgres driver |
| `alembic` | Database migrations | Version control for your schema |
| `pydantic-settings` | Env vars into a typed settings object | Config validation for free |
| `httpx` | Outbound HTTP | Async, API nearly identical to `requests` |
| `jmespath` | Payload reshaping | Declarative and safe, cannot execute code |
| `pytest`, `pytest-asyncio` | Tests | Standard |
| `respx` | Mocks httpx calls | Lets tests fake destinations without a server |
| `ruff` | Lint and format | One tool replacing black, flake8, isort |

Ten runtime dependencies. If that list grows past fifteen, something has gone
wrong.

## 2.2 Deliberately NOT used

This list is as important as the one above, and it is the first thing to explain
in an interview.

| Not used | Why |
|---|---|
| Celery / RQ / arq | The queue is a Postgres table. A broker reintroduces the dual-write problem (Part 5.1) |
| Redis | Nothing needs it. Circuit breaker state is in-process, concurrency caps are semaphores |
| Kafka / RabbitMQ | Wildly out of proportion for this |
| GraphQL | Six REST endpoints |
| Repository pattern / service layer | Routes use the session directly. FastAPI's dependency injection is already the seam |
| mypy | SQLAlchemy 2.0 plus Pydantic already give most of the value |

## 2.3 Project structure

Flat, twelve files. No nested packages.

```
app/
  main.py            app setup, route registration
  config.py          settings from environment
  db.py              engine, session factory, Base
  models.py          all tables
  schemas.py         all request/response shapes
  auth.py            write-key verification
  routes.py          all endpoints
  fanout.py          filter matching, delivery creation
  transform.py       JMESPath mapping
  destinations.py    http, slack deliverers
  worker.py          claim loop, delivery, retry
  batcher.py         batch accumulation and flush
  circuit.py         circuit breaker
tests/
alembic/
```

**When to split a file:** when it passes ~300 lines, or when you scroll to find
things. **When to merge:** when two files always change together. Do not create
structure in anticipation.

---
---

# Part 3: Database concepts

## 3.1 Why Postgres and not MySQL

Both are relational SQL databases and 80% of what you learn transfers. Two
features decided it here:

1. **JSONB.** Event payloads are arbitrary shapes you cannot model in advance.
   Postgres stores them as real, queryable, indexable JSON. MySQL's JSON support
   is weaker.
2. **`SKIP LOCKED`.** The heart of the worker's claim query. MySQL 8 has it,
   but it is newer and less battle-tested there.

## 3.2 Transactions

A transaction means "these writes happen together or not at all." Like a bank
transfer: money leaves one account and arrives in the other as one indivisible
act. There is no instant where it has left and not arrived.

```sql
BEGIN;
  INSERT INTO events ...;
  INSERT INTO deliveries ...;
COMMIT;          -- both, or if we crash before here, neither
```

This single idea is the foundation of the whole architecture. Part 5.1.

## 3.3 Constraints

A constraint is a rule the database enforces, no matter what the application
does. The one that matters here:

```sql
UNIQUE (source_id, idempotency_key)
```

This makes it *impossible* to have two events with the same key for one source.
Not "unlikely", impossible. Attempting it raises an error. Part 5.2 explains
why that is the only correct way to deduplicate.

**Postgres treats NULLs as distinct**, so rows with `idempotency_key = NULL`
never collide with each other. That is why events sent without a key are never
deduplicated, which is the behaviour we want.

## 3.4 Indexes

An index is a lookup structure that makes a query fast, at the cost of slightly
slower writes and some disk. Without one, the database scans every row.

This project has one composite index that matters:

```python
Index("ix_deliveries_claim", "status", "next_attempt_at")
```

The worker's claim query filters on `status` and `next_attempt_at` and orders by
`next_attempt_at`. This index is exactly that query's access path. Column order
matters: the index is useful for a query filtering on `status` alone, but not
for one filtering on `next_attempt_at` alone.

## 3.5 JSONB

```python
payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
```

Store a Python dict, get a Python dict back. Postgres stores it in a binary
format you can index and query into:

```sql
SELECT * FROM events WHERE payload->>'plan' = 'enterprise';
```

Use JSONB for genuinely arbitrary data, like customer event payloads. Do NOT
use it for things you know the shape of, like delivery status. Those get real
typed columns, so the database can constrain and index them properly.

## 3.6 ON CONFLICT (upsert)

```sql
INSERT INTO warehouse_events (...) VALUES (...)
ON CONFLICT (delivery_id) DO NOTHING;
```

"Insert this, but if a row with that primary key already exists, do nothing and
do not error." This is what makes the batch write **idempotent**, which in turn
is what lets a failed batch be retried whole instead of tracked per row.
Part 5.5.

---
---

# Part 4: Building it, file by file

Each step states what to build, why, and how to know it works.

## Step 0: Skeleton

**Goal:** an HTTP server that answers, before any real logic exists. If
`/health` does not respond, nothing else will, and you want to know that now
rather than tangled up with business logic.

### `app/config.py`

```python
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://localhost/event_pipeline"
    database_ssl: bool = True

    retry_base_seconds: float = 10.0
    retry_cap_seconds: float = 3600.0
    retry_max_attempts: int = 8
    poll_interval_seconds: float = 5.0
    claim_batch: int = 50
    claim_timeout_seconds: float = 60.0

    worker_concurrency: int = 16
    destination_concurrency: int = 5
    circuit_fail_threshold: int = 10
    circuit_cooldown_seconds: float = 300.0


settings = Settings()
```

**Syntax notes.**

- Subclassing `BaseSettings` makes pydantic read each field from an environment
  variable of the same name, uppercased. `retry_max_attempts` reads
  `RETRY_MAX_ATTEMPTS`.
- Values are **typed and converted**. `RETRY_MAX_ATTEMPTS=3` in the environment
  is the string `"3"`, and pydantic converts it to the integer `3`. A bad value
  fails loudly at startup rather than silently later.
- `env_file=".env"` loads a local file for development.
- `extra="ignore"` stops unrelated environment variables from causing errors.
- `settings = Settings()` at module level means it is constructed once at import
  and shared. Every module does `from app.config import settings`.

**Why config lives in the environment:** the same code has to run on your laptop
and in production without edits. Anything that differs between the two is
configuration, not code. This is one of the twelve-factor app principles and is
universally expected.

**Why worker tuning is configurable:** the defaults are production values
(retries over an hour). Tests override them to fractions of a second, which is
what makes the test suite run in seconds instead of hours.

### `app/db.py`

```python
from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import settings

connect_args = {"ssl": "require"} if settings.database_ssl else {}

engine = create_async_engine(
    settings.database_url,
    pool_size=10,
    connect_args=connect_args,
)

SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    """Parent class for every table model."""


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with SessionLocal() as session:
        yield session
```

**The three objects, and the difference between them.**

- **Engine.** One per process. Holds the connection pool. Expensive to create,
  so you make exactly one and reuse it.
- **Session.** One per request (or per unit of work). Cheap. Tracks the objects
  you have loaded and the changes you have made, and flushes them to the
  database on commit.
- **Base.** The parent class every model inherits from. SQLAlchemy collects
  table definitions onto `Base.metadata`, which is what Alembic compares against
  the real database to generate migrations.

**`expire_on_commit=False` is not optional here.** By default, after
`session.commit()` SQLAlchemy marks every loaded object as stale, so the next
attribute access silently re-queries the database. In async code that hidden
query happens during plain attribute access, which is a synchronous context, and
it crashes with `MissingGreenlet`. Part 7.1 covers the version of this that bit
me anyway.

**Why SSL is set in code, not the URL.** Neon hands you a connection string
ending in `?sslmode=require&channel_binding=require`. Those are libpq-style
parameters. `asyncpg` does not understand them and errors out. So you strip them
from the URL and pass `ssl` through `connect_args`, which goes straight to the
driver.

**`get_db` is a FastAPI dependency.** The `yield` makes it a generator
dependency: everything before `yield` is setup, everything after is teardown,
and FastAPI runs the teardown after the response is sent. `async with` gives us
the teardown for free.

### `app/main.py`

```python
from fastapi import FastAPI

from app.routes import router

app = FastAPI(title="EventRouter", description="...")


@app.get("/")
async def root():
    return {"service": "EventRouter", "docs": "/docs", "health": "/health"}


@app.get("/health")
async def health():
    return {"ok": True}


app.include_router(router)
```

**Why `/health` touches no database.** Platform health checks hit it. If it
queried the database, a brief database hiccup would make the platform think your
app is dead and restart it, turning a small problem into an outage.

**Verify:** `curl localhost:8000/health` returns 200. Also visit `/docs`, which
FastAPI generates for free from your type hints.

---

## Step 1: Accept events

**Goal:** register senders, accept their events, deduplicate retries.

### `app/models.py`, the first two tables

```python
def _id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(8)}"


class Source(Base):
    __tablename__ = "sources"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: _id("src"))
    name: Mapped[str] = mapped_column(String, nullable=False)
    write_key_hash: Mapped[str] = mapped_column(String, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Event(Base):
    __tablename__ = "events"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: _id("evt"))
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"), nullable=False, index=True)
    type: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String, nullable=True)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("source_id", "idempotency_key", name="uq_events_source_idem"),
    )
```

**Syntax notes.**

- `Mapped[str]` is the SQLAlchemy 2.0 typed style. The Python type annotation
  and the SQL column type are declared together and checked against each other.
- `mapped_column(String, primary_key=True, default=...)` describes the column.
- `default=lambda: _id("src")` runs in **Python** when you create the object.
  Contrast with `server_default=func.now()`, which becomes a **SQL** default and
  runs in the database. Use `server_default` for timestamps so the database
  clock is authoritative, not whichever machine happened to run the code.
- `Mapped[str | None]` plus `nullable=True` is how you declare a nullable
  column. The annotation and the argument should agree.
- `__table_args__` is where table-level things go: composite constraints and
  indexes that span more than one column.

**Prefixed IDs.** `evt_9f8e7d...` instead of a bare integer or UUID. Costs
nothing, and when you are staring at a log line you know instantly what kind of
thing you are looking at. Stripe does this and it is worth copying.

**Why hash the write key.** If someone steals a database dump they get a pile of
SHA-256 hashes, which cannot be reversed into working keys. Same reason a
reputable site cannot email you your forgotten password: they do not have it.
One line of code, removes an entire category of disaster.

### `app/auth.py`

```python
def hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def generate_write_key() -> str:
    return f"wk_{secrets.token_urlsafe(24)}"


async def require_source(
    authorization: str = Header(...),
    db: AsyncSession = Depends(get_db),
) -> Source:
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Expected 'Authorization: Bearer <write_key>'")
    raw = authorization.removeprefix("Bearer ")
    source = await db.scalar(select(Source).where(Source.write_key_hash == hash_key(raw)))
    if source is None:
        raise HTTPException(status_code=401, detail="Invalid write key")
    return source
```

**`secrets`, not `random`.** `random` is a predictable pseudo-random generator
seeded from the clock. Given enough output an attacker can predict future values.
`secrets` uses the operating system's cryptographic source. Use `secrets` for
anything security-relevant, always.

**How a FastAPI dependency works.** `require_source` is a plain async function.
When a route declares `source: Source = Depends(require_source)`, FastAPI calls
it before the route body, passes the result in, and if it raises `HTTPException`
the route never runs. That is authentication in three lines with no middleware.

Dependencies compose: `require_source` itself depends on `get_db`. FastAPI
resolves the whole tree, and **caches each dependency per request**, so
`get_db` yields one session shared by the dependency and the route. That sharing
matters in Part 7.1.

### `app/schemas.py`

```python
class SourceCreate(BaseModel):
    name: str


class SourceCreated(BaseModel):
    id: str
    name: str
    write_key: str


class TrackIn(BaseModel):
    type: str
    payload: dict
```

**Models vs schemas, and why both exist.** Models are database tables. Schemas
are the shapes that cross the HTTP boundary. They are deliberately different:

- `Source` the model has `write_key_hash`. `SourceCreated` the schema has
  `write_key`, the raw one, returned exactly once and never stored.
- If you returned the model directly you would leak the hash, and every future
  column you add to the table would silently appear in your public API.

Pydantic validates incoming JSON against the schema automatically. A request
missing `type` gets a 422 with a precise error, and your function never runs.

### `app/routes.py`, the track endpoint

```python
@router.post("/track", response_model=TrackAccepted, status_code=status.HTTP_202_ACCEPTED)
async def track(
    body: TrackIn,
    response: Response,
    source: Source = Depends(require_source),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db),
):
    source_id = source.id          # capture BEFORE any rollback; see Part 7.1

    event = Event(
        source_id=source_id,
        type=body.type,
        payload=body.payload,
        idempotency_key=idempotency_key,
    )
    db.add(event)
    try:
        await db.flush()
        destinations = await matching_destinations(db, source_id, body.type)
        db.add_all(build_deliveries(event.id, destinations))
        await db.commit()
        event_id = event.id
    except IntegrityError:
        await db.rollback()
        existing = await db.scalar(
            select(Event).where(
                Event.source_id == source_id,
                Event.idempotency_key == idempotency_key,
            )
        )
        event_id = existing.id
        response.status_code = status.HTTP_200_OK

    return TrackAccepted(id=event_id)
```

**How FastAPI reads that signature.** Each parameter is classified by its
declaration:

| Parameter | Source |
|---|---|
| `body: TrackIn` | Pydantic model, so the JSON request body |
| `response: Response` | Injected, lets you change the status code |
| `source: ... = Depends(...)` | Dependency result |
| `idempotency_key: ... = Header(...)` | HTTP header, renamed via `alias` |
| `db: ... = Depends(get_db)` | Dependency result |

**Why 202 and not 200.** `200 OK` means done. `202 Accepted` means "I have taken
responsibility, it has not happened yet." That is exactly true: the event is
stored, nothing is delivered. The status code is a promise, and this one is
honest.

The duplicate path returns **200** instead, meaning "already had this." A caller
can tell the difference between a fresh accept and a deduplicated retry.

**`flush()` vs `commit()`.** `flush()` sends the INSERT to the database inside
the open transaction, so the unique constraint fires, but does **not** commit.
That is what lets us catch the duplicate, and if the insert succeeds, add the
delivery rows to the *same* transaction before committing everything together.

**Verify:** send the same event twice with the same `Idempotency-Key`. Same
event id both times, 202 then 200, one row in the table.

---

## Step 2: Fan out

**Goal:** one event becomes N independent delivery jobs, written atomically with
the event.

### The `Delivery` model

```python
class Delivery(Base):
    __tablename__ = "deliveries"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: _id("dlv"))
    event_id: Mapped[str] = mapped_column(ForeignKey("events.id"), nullable=False, index=True)
    destination_id: Mapped[str] = mapped_column(
        ForeignKey("destinations.id"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(String, nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (Index("ix_deliveries_claim", "status", "next_attempt_at"),)
```

**This table is the whole architecture.** One row per `(event, destination)`.

The alternative would be a `sent_to_slack` boolean column on the event. Compare:

| | Boolean columns on event | One row per delivery |
|---|---|---|
| Independent retry counts | no | yes |
| Independent next-retry time | no | yes |
| Independent error message | no | yes |
| Adding a destination type | schema change | no change |
| Is a queue | no | yes |

**The status machine:**

```
pending ──claim──► delivering ──success──► delivered
   ▲                    │
   │                    ├──failure, attempts < max──► pending (with next_attempt_at)
   └────────────────────┘
                        └──failure, attempts >= max──► dead ──replay──► pending
```

### `app/fanout.py`

```python
async def matching_destinations(
    db: AsyncSession, source_id: str, event_type: str
) -> list[Destination]:
    destinations = await db.scalars(
        select(Destination).where(
            Destination.source_id == source_id,
            Destination.enabled.is_(True),
        )
    )
    return [d for d in destinations if fnmatch(event_type, d.filter)]


def build_deliveries(event_id: str, destinations: list[Destination]) -> list[Delivery]:
    return [Delivery(event_id=event_id, destination_id=d.id) for d in destinations]
```

**`fnmatch` is glob matching from the standard library.** `user.*` matches
`user.signed_up`. `*` matches everything. No regex, no dependency.

**Why filter in Python rather than SQL.** Postgres has no glob operator that
matches this semantic cleanly, and the number of destinations per source is
small (tens, not millions). If it ever grew, you would push it into SQL with
`LIKE` and a translated pattern. Knowing *why* the simple version is fine, and
what you would do if it stopped being fine, is the answer an interviewer wants.

**`.is_(True)` not `== True`.** SQLAlchemy overloads `==` to build SQL
expressions, and linters flag `== True`. `.is_(True)` generates `IS true` and is
the idiomatic form.

**Verify:** one event, three destinations with filters `*`, `user.*`, `order.*`.
A `user.signed_up` event produces exactly two delivery rows.

---

## Step 3: The worker

The hardest and most valuable part of the project.

### The claim query

```sql
UPDATE deliveries
SET status = 'delivering', claimed_at = now()
WHERE id IN (
    SELECT d.id
    FROM deliveries d
    JOIN destinations dst ON dst.id = d.destination_id
    WHERE dst.batch_size = 1
      AND (
            (d.status = 'pending' AND d.next_attempt_at <= now())
         OR (d.status = 'delivering'
             AND d.claimed_at < now() - make_interval(secs => :claim_timeout))
      )
    ORDER BY d.next_attempt_at
    FOR UPDATE OF d SKIP LOCKED
    LIMIT :limit
)
RETURNING id
```

Read it clause by clause:

| Clause | Purpose |
|---|---|
| `UPDATE ... SET status = 'delivering'` | Claim the rows so no one else takes them |
| `claimed_at = now()` | Timestamp the claim, for crash recovery |
| `JOIN destinations ... WHERE dst.batch_size = 1` | Only immediate destinations. Batched ones belong to the batcher |
| `status = 'pending' AND next_attempt_at <= now()` | Due for a first try or a retry |
| `OR status = 'delivering' AND claimed_at < now() - interval` | Orphaned by a crashed worker, reclaim it |
| `ORDER BY next_attempt_at` | Oldest due work first, so nothing starves |
| `FOR UPDATE OF d` | Lock these rows |
| `SKIP LOCKED` | Skip rows another worker already locked, do not wait |
| `LIMIT :limit` | Bounded batch |
| `RETURNING id` | Get the claimed ids back in one round trip |

**`SKIP LOCKED` is the single most interview-relevant line in this project.**
Part 5.3 covers it fully.

**Why raw SQL here** when the rest of the project uses the ORM: `UPDATE ...
WHERE id IN (SELECT ... FOR UPDATE SKIP LOCKED)` is awkward to express in the
ORM and the SQL is clearer. Mixing is fine. Use the ORM where it helps and raw
SQL where it does not.

**Note `FOR UPDATE OF d`.** The subquery joins `destinations`, and without `OF d`
Postgres would try to lock the destination rows too, which we do not want and
which would serialise unrelated work.

### The loop

```python
async def main() -> None:
    in_flight: set[asyncio.Task] = set()
    while True:
        did_work = False
        room = settings.claim_batch - len(in_flight)
        if room > 0:
            async with SessionLocal() as db:
                ids = await claim(db, room)
            for delivery_id in ids:
                task = asyncio.create_task(process_one(delivery_id))
                in_flight.add(task)
                task.add_done_callback(in_flight.discard)
            did_work = did_work or bool(ids)

        flushed = await run_batches()
        did_work = did_work or flushed > 0

        if not did_work and not in_flight:
            await asyncio.sleep(settings.poll_interval_seconds)
        else:
            await asyncio.sleep(0.05)
```

**`create_task`, not `await`.** This is the critical line, and getting it wrong
is Part 7.3. Spawning tasks means the loop keeps claiming while slow deliveries
are still in flight.

**`room = claim_batch - len(in_flight)`** bounds total in-flight work. Without
it, a fast producer and a slow destination would let the set grow without limit.

**`task.add_done_callback(in_flight.discard)`** removes the task when it
finishes. It also keeps a strong reference while running, which matters because
asyncio only holds weak references to tasks and can garbage-collect one
mid-flight otherwise.

**Two sleep durations.** 5 seconds when genuinely idle, to spare a
scale-to-zero database like Neon. 50ms when there is work, so finished
deliveries free capacity promptly.

### Processing one delivery

```python
async def process_one(delivery_id: str) -> None:
    async with SessionLocal() as db:
        row = await load(db, delivery_id)
    if row is None:
        return
    dest_id = row.destination_id

    if _breaker.is_open(dest_id):
        # release the claim, deferred to the cooldown
        ...
        return

    async with _dest_sem(dest_id), _global_sem:
        result = await deliver(row)

    async with SessionLocal() as db:
        await record_result(db, row.id, row.attempts, result)
```

**Three separate short transactions**, deliberately. The network call sits
between them with no database transaction open. A transaction held open for a
10-second HTTP timeout would hold locks and consume a connection from a pool of
ten. Open late, close early.

**`load` selects columns, not the ORM object:**

```python
select(Delivery.id, Delivery.attempts, Delivery.destination_id,
       Destination.type, Destination.config, Destination.transform,
       Event.payload)
```

This returns a `Row` of plain values, not tracked ORM objects. Plain values stay
valid after the session closes. ORM objects do not, which is exactly the trap in
Part 7.1.

**Semaphore acquisition order matters:**

```python
async with _dest_sem(dest_id), _global_sem:
```

Per-destination first, then global. Reversed, a coroutine waiting on a busy
destination would already be holding a global slot, blocking unrelated
destinations. Subtle, and worth understanding.

### Recording the outcome

```python
async def record_result(db, delivery_id, attempts, result):
    attempts += 1
    if result.ok:
        values = {"status": "delivered", "attempts": attempts,
                  "delivered_at": datetime.now(UTC), "last_error": None}
    elif attempts >= settings.retry_max_attempts:
        values = {"status": "dead", "attempts": attempts, "last_error": result.error}
    else:
        delay = compute_delay(attempts, result.retry_after)
        values = {"status": "pending", "attempts": attempts,
                  "next_attempt_at": datetime.now(UTC) + timedelta(seconds=delay),
                  "last_error": result.error}
    await db.execute(update(Delivery).where(Delivery.id == delivery_id).values(**values))
    await db.commit()
```

Three outcomes, one write. `datetime.now(UTC)` and never a naive datetime:
timezone-aware everywhere, always, or you will eventually compare an aware and a
naive datetime and get a `TypeError` in production.

### Backoff

```python
def compute_delay(attempts: int, retry_after: int | None = None) -> float:
    if retry_after is not None and retry_after > 0:
        return float(retry_after)
    delay = min(settings.retry_base_seconds * (2 ** (attempts - 1)), settings.retry_cap_seconds)
    return delay * random.uniform(0.5, 1.5)
```

Three behaviours in five lines, covered fully in Part 5.4.

### Delivering

```python
async def _send(url: str, body: bytes, headers: dict) -> DeliveryResult:
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.post(url, content=body, headers=headers)
    except httpx.TimeoutException:
        return DeliveryResult(ok=False, error="timeout")
    except httpx.HTTPError as exc:
        return DeliveryResult(ok=False, error=f"connection error: {type(exc).__name__}")

    if 200 <= resp.status_code < 300:
        return DeliveryResult(ok=True)
    return DeliveryResult(ok=False, error=f"HTTP {resp.status_code}",
                          retry_after=_parse_retry_after(resp))
```

**Every failure becomes a return value, never an exception.** A timeout, a dead
host, a 500: all just "not ok, retry." The caller has one thing to handle.

**The timeout is not optional.** Without it, a destination that accepts the
connection and never responds ties up that coroutine forever.

**HMAC signing:**

```python
def _sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
```

The receiver recomputes this over the body they received using the shared
secret. Match means it genuinely came from you and was not modified. Sign the
**exact bytes** you send, not the dict, because re-serialising can reorder keys
and change whitespace, breaking the signature.

**Verify:** three destinations, one always failing. Two go delivered, one climbs
through retries and dies. The successes are never re-sent.

---

## Step 4: Transform

```python
def validate_transform(mapping: dict) -> None:
    for key, expr in mapping.items():
        if not isinstance(expr, str):
            raise ValueError(f"transform value for '{key}' must be a JMESPath string")
        jmespath.compile(expr)


def apply_transform(mapping: dict, payload: dict) -> dict:
    if not mapping:
        return payload
    return {key: jmespath.search(expr, payload) for key, expr in mapping.items()}
```

Each destination stores a mapping like `{"distinct_id": "user_id", "tier": "plan"}`.
Applied to `{"user_id": "u_9", "plan": "enterprise", "extra": "x"}` it produces
`{"distinct_id": "u_9", "tier": "enterprise"}`.

**Validate at creation time, not delivery time.** A typo should fail with a 422
while a human is watching, not silently produce nulls at 3am inside a delivery
nobody is looking at. General rule: **catch bad input at the boundary where
someone can fix it.**

**Why JMESPath and not user code.** Running customer-supplied code on your
server means they can read your files, steal your database credentials, and mine
crypto on your CPU. Preventing that is sandboxing, a genuinely hard problem that
entire companies exist to solve. JMESPath can only pick and reshape fields. It
cannot open a file or make a network call.

Saying "I chose declarative mapping because sandboxing arbitrary execution is a
security problem I did not want to own" is a strong interview line. Knowing
which problems to decline is a seniority signal.

---

## Step 5: Slack

```python
async def deliver_slack(payload: dict, config: dict) -> DeliveryResult:
    url = config.get("webhook_url") or config.get("url")
    if not url:
        return DeliveryResult(ok=False, error="destination config missing 'webhook_url'")
    body = json.dumps(payload).encode()
    return await _send(url, body, {"Content-Type": "application/json"})


DELIVERERS = {"http": deliver_http, "slack": deliver_slack}
```

**Why a second destination type at all.** It teaches the thing the first one
cannot: **429 is not a generic failure.** Slack tells you exactly how long to
wait in a `Retry-After` header. Honouring that beats guessing, and guessing too
short earns another 429.

That is the real reason destinations are separate implementations. They differ
not just in URL but in **what failure means**.

**`DELIVERERS` is a dispatch dict**, not an `if/elif` chain and not a class
hierarchy. Adding a type is one function plus one dict entry.

---

## Step 6: Batching

Warehouse destinations (`batch_size > 1`) accumulate and flush in bulk.

```python
def _is_due(row) -> bool:
    if row.n_stale > 0:                      # orphaned batch, always recover
        return True
    if row.n_pending >= row.batch_size:      # size trigger
        return True
    if row.oldest is not None:               # window trigger
        cutoff = datetime.now(UTC) - timedelta(seconds=row.window_s)
        return row.oldest <= cutoff
    return False
```

**Both triggers are necessary.** Size alone means twelve rows wait forever for a
five hundredth that never arrives. Window alone means a spike sends one
fifty-thousand-row write.

The flush writes the batch and marks the deliveries delivered in one
transaction, using `ON CONFLICT DO NOTHING` so re-running it is harmless.
Part 5.5 explains why that makes whole-batch retry the right call.

---

## Step 7: Backpressure

Two mechanisms, and you need both. Part 5.6.

```python
_global_sem = asyncio.Semaphore(settings.worker_concurrency)
_dest_sems: dict[str, asyncio.Semaphore] = {}
_breaker = CircuitBreaker(settings.circuit_fail_threshold, settings.circuit_cooldown_seconds)
```

### `app/circuit.py`

```python
class CircuitBreaker:
    def __init__(self, threshold: int, cooldown: float):
        self.threshold = threshold
        self.cooldown = cooldown
        self._fails: dict[str, int] = defaultdict(int)
        self._open_until: dict[str, datetime] = {}

    def is_open(self, dest_id: str) -> bool:
        until = self._open_until.get(dest_id)
        if until is None:
            return False
        if until <= datetime.now(UTC):          # cooldown elapsed
            del self._open_until[dest_id]
            self._fails[dest_id] = 0
            return False
        return True

    def record(self, dest_id: str, ok: bool) -> bool:
        if ok:
            self._fails[dest_id] = 0
            self._open_until.pop(dest_id, None)
            return False
        self._fails[dest_id] += 1
        if self._fails[dest_id] >= self.threshold and dest_id not in self._open_until:
            self._open_until[dest_id] = datetime.now(UTC) + timedelta(seconds=self.cooldown)
            return True
        return False
```

Two dicts and some arithmetic. **No Redis needed**, because the state is
per-process and advisory. If a worker restarts and forgets a circuit was open,
the worst case is it retries a dead destination once more and reopens it.

`record` returns `True` only on the transition into open, which is how the
caller knows to defer that destination's pending work exactly once.

**Known limitation, worth stating before an interviewer finds it:** with several
workers, each keeps its own view. Sharing it would mean Redis, which is a real
trade you would make only if the cost of extra retries justified the extra
dependency.

---

## Step 8: Replay, stats, history

```python
@router.post("/destinations/{destination_id}/replay", response_model=ReplayResult)
async def replay_destination(destination_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        update(Delivery)
        .where(Delivery.destination_id == destination_id, Delivery.status == "dead")
        .values(status="pending", attempts=0, next_attempt_at=datetime.now(UTC))
    )
    await db.commit()
    return ReplayResult(replayed=result.rowcount)
```

Reset `attempts` to 0 so replayed deliveries get a full fresh retry budget.

`GET /v1/events/{id}` returns the event plus every delivery's status and attempt
history. This is the endpoint you point at when a customer says "we never got
it." The honest answer is right there, not a guess.

---
---

# Part 5: The hard concepts

## 5.1 The dual-write problem

**The single most important idea in the project.**

You need two things to happen together: save the event, and queue the work to
deliver it. If the queue is Redis and the data is Postgres:

```
INSERT event into Postgres     ✓ committed
        ✗ process crashes here
ENQUEUE job into Redis         ✗ never ran
```

You have now accepted an event, told the customer 202, and no worker will ever
deliver it. Silent data loss, and it is the exact thing the product exists to
prevent. Two separate systems **cannot** share a transaction.

Keep the queue in the same database as the data, and it is one transaction:

```python
async with db.begin():
    db.add(event)
    await db.flush()
    db.add_all(deliveries)
# both committed, or neither
```

This is also known as the **outbox pattern**. Postgres-as-queue is the outbox
pattern with the extra step removed.

**Cost of this choice:** Postgres handles far less throughput as a queue than
Kafka. At tens of thousands of events per second you would revisit it. At the
scale this serves, correctness matters more, and you should say so.

## 5.2 Idempotency and the race

The customer's server sends an event, the network hiccups, they never see the
reply, so they retry. Without protection the user gets two welcome emails.

The obvious code is **wrong**:

```python
existing = await find_by_key(key)     # two requests both reach here
if existing:                          # both find nothing
    return existing
await insert(event)                   # both insert. Two rows.
```

Two requests arriving in the same instant both check, both see nothing, both
insert. This is a **race condition**: code that is correct when you trace it once
and wrong when two copies run at the same moment.

The fix is to let the database arbitrate:

```python
try:
    await db.flush()          # unique constraint fires here on a duplicate
except IntegrityError:
    await db.rollback()
    existing = await db.scalar(select(Event).where(...))
```

The database processes those two inserts one at a time no matter how
simultaneous they looked. It is the only component that can settle the tie.
**There is no way to write a correct check-then-act in application code.**

Remember this pattern. It generalises to every "create only if not exists"
problem you will meet.

## 5.3 SKIP LOCKED

Run two workers for speed and both grab the same delivery slip, so the same
webhook fires twice. You caused that.

```sql
SELECT id FROM deliveries
WHERE status = 'pending'
FOR UPDATE SKIP LOCKED
LIMIT 50
```

- **`FOR UPDATE`**: lock these rows, I am taking them.
- **`SKIP LOCKED`**: anything already locked by someone else, skip it, do not
  wait.

Both halves are load bearing, and dropping either breaks something different:

| | Result |
|---|---|
| Plain `SELECT` | Both workers take rows 1-50. Same webhook sent twice |
| `FOR UPDATE` alone | Worker B blocks until A commits. Two workers, one worker's throughput |
| `FOR UPDATE SKIP LOCKED` | A takes 1-50, B takes 51-100. Correct and parallel |

The courier version: two drivers reach for slips on the same shelf. A grabs
1-50. B's hand arrives, sees those are taken, and grabs 51-100 instead. Neither
waits, neither collides.

**Learn this properly.** "How do you stop two workers processing the same job"
is a question you will be asked.

## 5.4 Backoff, jitter, and giving up

**Backoff.** A destination is down. Retrying every second makes you part of the
outage. So each retry waits longer: 10s, 20s, 40s, 80s, capped at an hour.

```python
delay = min(base * (2 ** (attempts - 1)), cap)
```

**Jitter, the non-obvious part.** 500 deliveries fail at 9:00 because a
destination went down. All 500 are scheduled to retry at 9:00:10. The
destination recovers at 9:00:09 and is immediately hit by 500 simultaneous
requests, and falls over again. You have built a machine that repeatedly kills a
recovering server. This is the **thundering herd**.

```python
return delay * random.uniform(0.5, 1.5)
```

Now those retries spread across a window instead of arriving as a wall.

**Honouring `Retry-After`.** When a destination returns 429 with a
`Retry-After: 30` header, it has told you exactly when to come back. Guessing is
strictly worse, so that check comes first in `compute_delay`.

**Giving up.** After 8 attempts, mark it `dead`. Some destinations are gone for
good, and retrying forever means an ever-growing pile of doomed work crowding
out real deliveries. Dead is recoverable via replay, so nothing is lost.

## 5.5 Idempotent writes and whole-batch retry

A batch of 500 partially fails. Two options:

1. Track which rows failed, retry only those. Per-row state inside a batch.
2. Retry the whole batch, and make the write idempotent.

Take option 2:

```sql
INSERT INTO warehouse_events (...) VALUES (...)
ON CONFLICT (delivery_id) DO NOTHING;
```

Re-sending 500 rows where 480 already landed is harmless when writes are
idempotent, and the code stays simple. Option 1 is real machinery for a rare
case.

**The general principle: making an operation idempotent is usually cheaper than
tracking exactly what happened.** This applies far beyond batching. It is why
`Idempotency-Key` exists on the ingest endpoint too.

Document the reasoning. **A documented tradeoff beats an unexamined one**, and
interviewers care much more that you considered the alternative than which you
picked.

## 5.6 Backpressure: why two fixes were needed

One destination starts hanging, 30 seconds per request. Without protection, the
worker's capacity fills with stuck deliveries and healthy destinations stop
receiving anything. One bad endpoint has taken down the pipeline.

**Fix one, per-destination concurrency cap:**

```python
async with _dest_sem(dest_id), _global_sem:
    result = await deliver(row)
```

At most 5 coroutines on any one destination.

**Fix two, do not block the claim loop.** This is the one I missed, and the
measured result shows why it mattered:

```
destination A hangs 30s, B and C healthy, 6 events each

per-destination cap only     A: 0   B: 1   C: 1    loop still stuck
+ spawn deliveries as tasks  A: 0   B: 6   C: 6    ✓
```

The original loop did `await asyncio.gather(*batch)`, so it waited for the whole
batch before claiming again. The cap correctly limited how many coroutines sat
on A, but the loop itself was still blocked behind them.

**Two different bottlenecks, two different fixes.** Understanding that
distinction is the most valuable thing in this project, and it is a real story
you can tell.

**Circuit breaker.** After N consecutive failures, stop calling a destination
for a cooldown. The name comes from electrical breakers: when there is a fault,
cut the connection so it does not damage everything downstream. Nothing is lost,
the deliveries stay pending and deferred.

## 5.7 Crash recovery

A worker claims a delivery, sets it to `delivering`, then the process dies. That
row is now stuck forever: not pending, so nothing claims it.

```sql
OR (d.status = 'delivering'
    AND d.claimed_at < now() - make_interval(secs => :claim_timeout))
```

Any row that has been `delivering` for longer than the timeout belonged to a
worker that is not coming back. Reclaim it.

**This is why at-least-once, not exactly-once.** If the worker died *after* the
HTTP request succeeded but *before* recording it, we will deliver it again.
Exactly-once across a network is not achievable in general, so you choose
at-least-once and make retries safe with idempotency. Being able to say that
sentence clearly is worth a lot in an interview.

---
---

# Part 6: Testing

## 6.1 What to test, and what not to

Test the properties that would be disasters if broken:

| Test | Disaster it prevents |
|---|---|
| Duplicate idempotency key | Customer's user gets two welcome emails |
| One of three destinations fails | A Slack outage stops the warehouse loading |
| Duplicate track creates no extra deliveries | Orphan rows with nothing to dedupe them |
| Two workers, one delivery | The same webhook fires twice |
| Retry then success | Transient failures are not permanent |
| Dead-letter at max attempts | A dead destination consumes retries forever |
| Stale claim reclaimed | A crashed worker's row stays stuck forever |
| Batch flush, both triggers | Data hours stale, or one huge write |
| Circuit breaker | One bad endpoint freezes everything |

Do not test that FastAPI parses JSON or that SQLAlchemy writes rows. Test *your*
logic.

## 6.2 Real database, mocked HTTP

```python
@pytest_asyncio.fixture(autouse=True)
async def clean_db():
    async with SessionLocal() as db:
        await db.execute(text(f"TRUNCATE {TABLES} CASCADE"))
        await db.commit()
    yield
    await engine.dispose()
```

**Against a real Postgres, not SQLite and not a mock.** The properties worth
proving here (`SKIP LOCKED`, unique-constraint dedup, transactional fan-out) are
database behaviours. A mock would prove your mock works. SQLite does not have
`SKIP LOCKED` at all.

**Only outbound HTTP is mocked**, with `respx`:

```python
@respx.mock
async def test_retry_then_success(client, source, auth):
    route = respx.post("http://dest.test/hook")
    route.side_effect = [Response(500), Response(500), Response(200)]
```

`side_effect` as a list gives a different response per call, which is exactly
how you test "fails twice then recovers."

**`await engine.dispose()` in teardown.** pytest-asyncio gives each test its own
event loop, but pooled asyncpg connections stay bound to the loop they were
opened on. Without disposing, the next test reuses a connection tied to a dead
loop and fails with "attached to a different loop." Part 7.2.

## 6.3 Speeding up time

```python
@pytest.fixture(autouse=True)
def fast_settings(monkeypatch):
    monkeypatch.setattr(settings, "retry_base_seconds", 0.05)
    monkeypatch.setattr(settings, "retry_max_attempts", 3)
    monkeypatch.setattr(settings, "claim_timeout_seconds", 1.0)
```

Production retry delays are minutes to hours. Tests override them to
milliseconds. This is why worker tuning lives in settings rather than as
constants, and it is the practical payoff of that design choice.

## 6.4 The most valuable test

```python
async def test_skip_locked_prevents_double_claim(client, source, auth):
    async def claim_10():
        async with SessionLocal() as db:
            return await worker.claim(db, limit=10)

    a, b = await asyncio.gather(claim_10(), claim_10())
    assert set(a).isdisjoint(set(b)), "two workers claimed the same delivery"
```

Two genuinely concurrent claims against a real database, asserting no overlap.
This is the test that proves the core correctness property, and it is the one to
mention if an interviewer asks what you tested.

---
---

# Part 7: Bugs hit while building

These are worth more than the code that worked first time.

## 7.1 MissingGreenlet after rollback

**Symptom.** The duplicate-event path crashed with:

```
sqlalchemy.exc.MissingGreenlet: greenlet_spawn has not been called;
can't call await_only() here. Was IO attempted in an unexpected place?
```

**The traceback pointed at an innocent-looking line:**

```python
Event.source_id == source.id
                   ^^^^^^^^^
```

**Cause.** `require_source` loaded the `source` ORM object into the session.
Then `await db.rollback()` ran, and **rollback expires every ORM object in the
session** (`expire_on_commit=False` only affects commit, not rollback). Touching
`source.id` afterwards made SQLAlchemy try to reload it from the database, and
that lazy load happened during plain attribute access, a synchronous context
with no async greenlet.

**Fix.** Capture the value before anything can expire the object:

```python
source_id = source.id     # plain string, immune to expiry
```

**Lesson.** In async SQLAlchemy, after a rollback or commit, treat ORM objects
as unsafe to touch. Pull out the scalars you need first. This is also why the
worker's `load` selects columns rather than entities.

## 7.2 Event loop reuse in tests

**Symptom.** The first test passed, the second failed with "attached to a
different loop."

**Cause.** pytest-asyncio creates a fresh event loop per test. The SQLAlchemy
engine is module-level and pools connections. A pooled asyncpg connection is
bound to the loop that created it, so test two got a connection belonging to
test one's dead loop.

**Fix.** `await engine.dispose()` after each test, forcing fresh connections.

**Lesson.** Connection pools and per-test event loops interact badly. This
catches nearly everyone building async Python the first time.

## 7.3 The gather bug

**Symptom.** The backpressure test failed twice. Healthy destinations B and C
delivered 1 of 6 events in three seconds while A hung, when they should have
delivered all 6.

**First diagnosis, wrong.** I assumed leftover rows from earlier test runs were
competing for capacity, so I added a database reset. It did not fix it.

**Real cause.**

```python
ids = await claim(db)
await asyncio.gather(*(process_one(i) for i in ids))   # waits for the WHOLE batch
```

The per-destination semaphore correctly limited how many coroutines sat on A.
But the loop awaited the entire batch before claiming again, so one 30-second
hang froze all new work regardless.

**Fix.** Spawn tasks and keep looping:

```python
task = asyncio.create_task(process_one(delivery_id))
in_flight.add(task)
task.add_done_callback(in_flight.discard)
```

Result went from B: 1, C: 1 to B: 6, C: 6.

**Lesson, and the best story in this project.** A concurrency limit and a
non-blocking loop are different things, and fixing one does not fix the other.
Also: the first plausible explanation was wrong, and only reading the actual
logs found the real one.

---
---

# Part 8: Interview preparation

## 8.1 The seven questions

Answer these cold and the project does its job.

**1. Why is the queue a Postgres table instead of Celery or Redis?**

> Accepting an event and creating its delivery rows has to be atomic. With Redis
> as the queue I would write the event to Postgres and the job to Redis, two
> separate systems that cannot share a transaction. A crash in between leaves an
> accepted event that nothing will ever deliver, which is the exact failure the
> service exists to prevent. One database means one transaction. The tradeoff is
> throughput: Postgres as a queue does not scale like Kafka, and at very high
> volume I would revisit it.

**2. What does `SKIP LOCKED` do, and what breaks without it?**

> `FOR UPDATE` locks the rows a worker claims. `SKIP LOCKED` makes a second
> worker skip past locked rows instead of blocking. Without `SKIP LOCKED` the
> second worker waits for the first to commit, so two workers give you one
> worker's throughput. Without `FOR UPDATE` at all, both workers select the same
> rows and you deliver every webhook twice.

**3. What happens if the worker dies mid-delivery?**

> The row is left in `delivering`. The claim query also picks up rows that have
> been `delivering` longer than a timeout, so another worker reclaims it. That
> makes delivery at-least-once, not exactly-once: if the worker died after the
> HTTP call succeeded but before recording it, we deliver again. Exactly-once
> across a network is not achievable, so the design is at-least-once plus
> idempotency to make retries safe.

**4. Why 202 and not 200?**

> 200 means done. 202 means accepted but not yet processed, which is exactly
> true: the event is stored, nothing is delivered. The status code is a promise
> and 202 is the honest one. A deduplicated retry returns 200 instead, so the
> caller can tell a fresh accept from a duplicate.

**5. Why randomise the retry delay?**

> Without jitter, everything that failed at the same moment retries at the same
> moment. Five hundred deliveries hit a recovering destination simultaneously
> and knock it over again. Jitter spreads the retries across a window. It is the
> thundering herd problem.

**6. One destination hangs for 30 seconds. What happens to the others?**

> Nothing, and it took two separate fixes to get there. A per-destination
> semaphore caps how many coroutines one destination can occupy. But the claim
> loop was awaiting the whole batch, so one hanging request still froze new work
> even with the cap. Spawning each delivery as a task fixed that. Healthy
> destinations went from 1 of 6 delivered to 6 of 6. There is also a circuit
> breaker that stops calling a destination entirely after repeated failures.

**7. How do you avoid sending the same webhook twice?**

> Two layers. On ingest, an `Idempotency-Key` with a unique constraint on
> `(source_id, idempotency_key)`, so a client retry does not create a second
> event. I let the database raise and catch `IntegrityError` rather than
> checking first, because check-then-insert is racy. On delivery, `SKIP LOCKED`
> means two workers never claim the same row. Delivery is still at-least-once
> across a crash, which is why receivers should treat webhooks as idempotent.

## 8.2 Framing the project

**One-liner:**

> EventRouter accepts an event once and guarantees delivery to many downstream
> systems, each with its own format and retry state. The interesting part is
> that partial failure is the normal case, so retry state lives per destination,
> not per event.

**What it is, precisely:** a concurrency and reliability problem, not a
consensus problem. One Postgres is the single source of truth, so there is no
leader election, quorum, or split brain.

**Vocabulary you have earned vs. have not:**

| Can claim | Cannot claim |
|---|---|
| At-least-once delivery | Consensus, Raft, Paxos |
| Idempotency | Distributed transactions |
| Backpressure, circuit breaking | CAP tradeoffs you manage |
| Partial failure handling | Leader election |
| Concurrent work claiming | Conflict resolution |

Being precise about this is itself a signal. A junior who says "it is a
concurrency problem, there is one Postgres so no consensus involved" sounds far
better than one who says "I built a distributed system" and cannot define a
quorum.

## 8.3 Likely follow-ups

**"How would you scale this?"**
> Workers scale horizontally today, `SKIP LOCKED` already makes that safe. The
> API scales behind a load balancer. Postgres is the first bottleneck: I would
> partition `deliveries` by time, move completed rows to cold storage, and only
> then consider a real broker.

**"What would you do differently?"**
> Circuit breaker state is in-process, so multiple workers each keep their own.
> Moving it to Redis would be the first change if I ran more than one worker.
> I would also split the batcher out of the delivery loop, since a large flush
> briefly delays the next poll.

**"How do you know it works?"**
> Twenty tests against a real Postgres. The one I care about most spawns two
> concurrent claims and asserts they never overlap, which is the core
> correctness property.

---
---

# Part 9: Rebuild-from-scratch checklist

Work in this order. Do not move on until the verify passes.

| # | Build | Verify |
|---|---|---|
| 0 | FastAPI app, `/health`, config, db module | `curl /health` returns 200 |
| 1 | `sources` + `events`, write-key auth, `POST /track` | Same idempotency key twice returns the same event id, 202 then 200 |
| 2 | `destinations` + `deliveries`, fan-out in one transaction | One event, three destinations, two matching filters, exactly two delivery rows |
| 3 | Worker: claim, deliver, retry, dead-letter | Three destinations, one always failing: two delivered, one dead, successes never re-sent |
| 4 | JMESPath transform, validated at creation | Bad expression returns 422; good one reshapes the payload |
| 5 | Slack deliverer honouring `Retry-After` | A 429 with `Retry-After: 1` retries after ~1s, not the backoff value |
| 6 | Batcher: size and window triggers | 500 events flush on size; 12 events flush after the window; nothing flushes early |
| 7 | Semaphores, circuit breaker, task-spawning loop | One hung destination, two healthy: healthy ones deliver everything |
| 8 | Replay, stats, event detail, tests, CI, Docker | Suite green in CI |

**Ordering rules.** Build the simplest correct version first and make it the
thing everything else is tested against. Deploy before polishing. Write the
verify condition before the implementation.

---
---

# Appendix A: Glossary

| Term | Meaning |
|---|---|
| ASGI | Async server interface Python web frameworks implement. Uvicorn speaks it |
| At-least-once | A message may be delivered more than once, never zero times |
| Backpressure | Limiting in-flight work so a slow consumer cannot overwhelm the system |
| Circuit breaker | Stop calling a failing dependency for a cooldown period |
| Coroutine | An `async def` function's return value. Does nothing until awaited |
| Dead letter | A message that has exhausted its retries and is set aside for a human |
| Dual write | Writing to two systems that cannot share a transaction. A correctness hazard |
| Event loop | The scheduler that runs coroutines on one thread |
| Fan-out | One input producing many outputs |
| HMAC | Hash-based message authentication code. Proves a payload came from you unmodified |
| Idempotent | Doing it twice has the same effect as doing it once |
| Jitter | Randomness added to retry delays to avoid synchronised waves |
| JSONB | Postgres binary JSON, indexable and queryable |
| Migration | A versioned, replayable change to database schema |
| ORM | Object-relational mapper. Python classes to SQL tables |
| Outbox pattern | Writing queue rows in the same transaction as the data |
| Race condition | Code correct when run once, wrong when two copies overlap |
| Semaphore | A counter of permits, bounding concurrency |
| Thundering herd | Many clients hitting a recovering service at once |
| Upsert | Insert, or do something else if it already exists |

# Appendix B: SQL reference

```sql
-- Claim work safely across concurrent workers
SELECT id FROM deliveries
WHERE status = 'pending' AND next_attempt_at <= now()
ORDER BY next_attempt_at
FOR UPDATE SKIP LOCKED
LIMIT 50;

-- Idempotent insert
INSERT INTO warehouse_events (delivery_id, ...) VALUES (...)
ON CONFLICT (delivery_id) DO NOTHING;

-- Enforce dedup at the database level
ALTER TABLE events ADD CONSTRAINT uq_events_source_idem
  UNIQUE (source_id, idempotency_key);

-- Composite index matching the claim query's access path
CREATE INDEX ix_deliveries_claim ON deliveries (status, next_attempt_at);

-- Conditional aggregation, used by the batcher
SELECT count(*) FILTER (WHERE status = 'pending') AS n_pending,
       min(created_at) FILTER (WHERE status = 'pending') AS oldest
FROM deliveries GROUP BY destination_id;

-- Interval arithmetic from a parameter
WHERE claimed_at < now() - make_interval(secs => :timeout)
```

# Appendix C: File map

| File | Lines | Responsibility |
|---|---|---|
| `app/backoff.py` | 20 | Retry delay with jitter |
| `app/fanout.py` | 27 | Filter matching, delivery construction |
| `app/transform.py` | 31 | JMESPath validate and apply |
| `app/auth.py` | 35 | Key hashing, generation, `require_source` |
| `app/main.py` | 35 | App construction, root and health routes |
| `app/config.py` | 36 | Typed settings from environment |
| `app/db.py` | 36 | Engine, session factory, `Base`, `get_db` |
| `app/circuit.py` | 45 | Circuit breaker |
| `app/schemas.py` | 73 | Request and response shapes |
| `app/destinations.py` | 85 | HTTP and Slack deliverers, HMAC, dispatch |
| `app/models.py` | 128 | All five tables |
| `app/batcher.py` | 150 | Batch accumulation and flush |
| `app/routes.py` | 196 | Six endpoints |
| `app/worker.py` | 236 | Claim loop, delivery, retry, backpressure |
| | **1,133** | plus ~600 lines of tests |

Note the shape: the two files carrying the hard logic (`worker.py`,
`batcher.py`) are a third of the codebase, and the file you would expect to be
biggest in a CRUD app (`routes.py`) is thin. That is the inverse of a tutorial
project, and it is deliberate.

**Read them in this order to understand the system:** `models.py`,
`routes.py` (the `track` function), `worker.py` (the claim query and `main`),
then the rest as needed.

---

## Exporting to PDF

```bash
pandoc GUIDE.md -o guide.pdf --toc --highlight-style=tango -V geometry:margin=1in
```

Or open in any Markdown editor and print to PDF. The ASCII diagrams need a
monospace font to line up, which every Markdown renderer already uses for code
blocks.
