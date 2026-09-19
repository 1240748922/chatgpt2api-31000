"""Dedicated authenticated import API/worker. Does not register image routes."""
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from pathlib import Path

from api.account_ingest import create_router
from services.account_ingest_service import get_account_ingest_service
from services.config import config


@asynccontextmanager
async def lifespan(_app):
    service = await run_in_threadpool(get_account_ingest_service)
    service.start()
    try:
        yield
    finally:
        await run_in_threadpool(service.stop)


app = FastAPI(title="chatgpt2api account importer", lifespan=lifespan)
app.include_router(create_router())


@app.get("/version")
def version():
    return {"version": config.app_version, "role": "account-importer"}


@app.get("/account-import.html")
def page():
    return FileResponse(Path(__file__).parent / "web_dist" / "account-import.html")


@app.get("/account-import.js")
def script():
    return FileResponse(Path(__file__).parent / "web_dist" / "account-import.js", media_type="application/javascript")
