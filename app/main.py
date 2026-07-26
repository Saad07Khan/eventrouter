from fastapi import FastAPI

from app.routes import router

app = FastAPI(title="EventRouter")


@app.get("/health")
async def health():
    return {"ok": True}


app.include_router(router)
