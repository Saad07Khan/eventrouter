from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse

from app.config import settings
from app.routes import router

_DEMO_PAGE = Path(__file__).parent / "static" / "demo.html"

app = FastAPI(
    title="EventRouter",
    description=(
        "Ingest an event once, fan it out to many destinations — each with its own "
        "transform, batching, and independent retry state."
    ),
)


@app.get("/")
async def root():
    """Landing route: says what this service is and where to go next.

    Also what the platform's default health check hits, so it must stay
    cheap — no database access here.
    """
    return {
        "service": "EventRouter",
        "description": "Reliable event delivery to multiple destinations.",
        "docs": "/docs",
        "demo": "/demo",
        "health": "/health",
        "source": "https://github.com/Saad07Khan/eventrouter",
    }


@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/demo", response_class=HTMLResponse, include_in_schema=False)
async def demo():
    """A one-page live view of a fan-out: three destinations, one of them broken.

    Swagger shows every endpoint but none of the behaviour — a single 202 tells
    you nothing about retries. This stages a failure and polls the event so the
    per-destination divergence is visible.
    """
    if not settings.demo_enabled:
        raise HTTPException(status_code=404, detail="demo page is disabled")
    return HTMLResponse(_DEMO_PAGE.read_text(encoding="utf-8"))


app.include_router(router)
