import hashlib
import secrets

from fastapi import Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.models import Source


def hash_key(raw: str) -> str:
    """sha256 of a write key. What we compare against and store."""
    return hashlib.sha256(raw.encode()).hexdigest()


def generate_write_key() -> str:
    """A fresh secret to hand a source. Shown to them exactly once."""
    return f"wk_{secrets.token_urlsafe(24)}"


async def require_source(
    authorization: str = Header(...),
    db: AsyncSession = Depends(get_db),
) -> Source:
    """FastAPI dependency: turn an 'Authorization: Bearer wk_...' header into a Source,
    or reject with 401. Any endpoint that depends on this is authenticated."""
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Expected 'Authorization: Bearer <write_key>'")
    raw = authorization.removeprefix("Bearer ")
    # Look up by the hash — we never have the raw key on file to compare directly.
    source = await db.scalar(select(Source).where(Source.write_key_hash == hash_key(raw)))
    if source is None:
        raise HTTPException(status_code=401, detail="Invalid write key")
    return source
