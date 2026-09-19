from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from api.support import require_admin
from services.account_ingest_service import get_account_ingest_service, IngestConflict


class AccountIngestRequest(BaseModel):
    accounts: list[dict] = Field(default_factory=list, max_length=50000)
    tokens: list[str] = Field(default_factory=list, max_length=50000)
    sync_after_import: bool = False
    target_group_id: str | None = None
    request_key: str | None = Field(default=None, max_length=160)


def create_router():
    router = APIRouter()

    @router.post("/api/account-import-jobs", status_code=202)
    async def submit(body: AccountIngestRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        from api.accounts import _target_account_group_id
        group = _target_account_group_id(body.target_group_id)
        items = [*body.accounts, *({"access_token": token} for token in body.tokens)]
        if group is not None:
            items = [{**item, "group_id": group} for item in items]
        try:
            service = await run_in_threadpool(get_account_ingest_service)
            job = await run_in_threadpool(service.submit, items, sync_after_import=body.sync_after_import, request_key=body.request_key)
        except IngestConflict as exc:
            raise HTTPException(409, detail={"error": str(exc)}) from exc
        except ValueError as exc:
            raise HTTPException(400, detail={"error": str(exc)}) from exc
        return {"job": job}

    @router.get("/api/account-import-jobs")
    async def list_jobs(authorization: str | None = Header(default=None), limit: int = Query(30, ge=1, le=100)):
        require_admin(authorization)
        service = await run_in_threadpool(get_account_ingest_service)
        return {"jobs": await run_in_threadpool(service.list_jobs, limit)}

    @router.get("/api/account-import-jobs/{job_id}")
    async def get(job_id: str, authorization: str | None = Header(default=None), result: bool = False):
        require_admin(authorization)
        service = await run_in_threadpool(get_account_ingest_service)
        job = await run_in_threadpool(service.get, job_id, include_result=result)
        if job is None:
            raise HTTPException(404, detail={"error": "导入任务不存在"})
        return {"job": job}

    @router.post("/api/account-import-jobs/{job_id}/retry")
    async def retry(job_id: str, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        service = await run_in_threadpool(get_account_ingest_service)
        try:
            job = await run_in_threadpool(service.retry, job_id)
        except IngestConflict as exc:
            raise HTTPException(409, detail={"error": str(exc)}) from exc
        if job is None:
            raise HTTPException(404, detail={"error": "导入任务不存在"})
        return {"job": job}

    return router
