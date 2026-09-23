"""Dedicated authenticated import API/worker. Does not register image routes."""
from contextlib import asynccontextmanager
from fastapi import FastAPI, Header, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import RedirectResponse

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
    return RedirectResponse("/#/accounts?import=access_token", status_code=307)


@app.get("/internal/monitor/load")
def maintenance_load(x_cluster_monitor_secret: str = Header(default="")):
    from services.cluster_monitor_service import local_internal_image_load
    try:
        return local_internal_image_load(x_cluster_monitor_secret)
    except PermissionError:
        raise HTTPException(status_code=403, detail="Forbidden") from None
