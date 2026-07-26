from fastapi import FastAPI

app = FastAPI(title="Event Pipeline")


@app.get("/health")
async def health():
    return {"ok": True}
