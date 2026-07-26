from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import settings

# One engine (connection pool) for the whole process.
#
# connect_args go straight to asyncpg:
#   ssl="require"  -> Neon (like most cloud Postgres) refuses non-SSL connections.
#                     We set it here in code instead of in the URL, because asyncpg
#                     does not understand libpq's ?sslmode= query parameter.
engine = create_async_engine(
    settings.database_url,
    pool_size=10,
    connect_args={"ssl": "require"},
)

# A session factory. Each request gets its own session from here.
#
# expire_on_commit=False matters in async code: without it, touching an
# object's attributes AFTER commit triggers a hidden reload from the DB,
# which explodes in async because it happens outside an awaited call.
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    """Parent class for every table model."""


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency: hand a request one session, close it when done."""
    async with SessionLocal() as session:
        yield session
