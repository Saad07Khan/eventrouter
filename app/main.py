from fastapi import FastAPI

from app.routes import router

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
        "health": "/health",
        "source": "https://github.com/Saad07Khan/eventrouter",
    }


@app.get("/health")
async def health():
    return {"ok": True}


app.include_router(router)
