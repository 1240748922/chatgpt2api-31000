from __future__ import annotations

from contextlib import asynccontextmanager
from threading import Event, Thread

from anyio.to_thread import current_default_thread_limiter
from fastapi import FastAPI, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from api import accounts, ai, image_tasks, prompts, system, account_ingest
from api.errors import install_exception_handlers
from api.support import (
    resolve_web_asset,
    start_account_lifecycle_watcher,
    start_account_replenishment_watcher,
)
from services.account_service import account_service
from services.backup_service import backup_service
from services.config import config
from services.credential_event_log import credential_log_runner
from services.dashboard_metrics_service import dashboard_metrics_service
from services.genbox_push_service import (
    shutdown_genbox_push_service,
    start_genbox_push_service,
)
from services.image_task_service import image_task_service
from services.log_service import log_service
from services.realtime_monitor_service import realtime_monitor_service
from services.retention_cleanup_service import retention_cleanup_coordinator, start_retention_cleanup_scheduler
from services.runtime_configuration import DEFAULT_THREAD_TOKENS, account_shard_settings, env_int
from utils.log import logger


RETENTION_SHUTDOWN_TIMEOUT_SECS = 1.0


def _configure_threadpool() -> None:
    tokens = env_int("CHATGPT2API_THREAD_TOKENS", DEFAULT_THREAD_TOKENS)
    limiter = current_default_thread_limiter()
    previous = int(getattr(limiter, "total_tokens", 0) or 0)
    if previous != tokens:
        limiter.total_tokens = tokens
    realtime_monitor_service.set_threadpool(tokens=tokens, previous_tokens=previous)
    logger.info({
        "event": "runtime_threadpool_configured",
        "previous_tokens": previous,
        "tokens": tokens,
    })


def create_app() -> FastAPI:
    app_version = config.app_version

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        _configure_threadpool()
        credential_log_runner.start()
        from services.page_prewarm_pool import page_prewarm_pool
        page_prewarm_pool.start(account_service)
        singleton_background = account_shard_settings()[1] == 0
        import_service = None
        startup_maintenance_thread: Thread | None = None

        def run_startup_maintenance() -> None:
            """Run singleton maintenance without delaying the HTTP listener."""
            try:
                projection_reset = dashboard_metrics_service.reset_projection_schema_if_needed()
                if projection_reset.changed:
                    logger.info({
                        "event": "dashboard_metrics_projection_schema_reset",
                        "state_recreated": projection_reset.state_recreated,
                        "hourly_recreated": projection_reset.hourly_recreated,
                        "model_hourly_recreated": projection_reset.model_hourly_recreated,
                    })
            except Exception as exc:
                logger.error({"event": "dashboard_metrics_projection_schema_reset_failed", "error": str(exc)})
            try:
                cleanup_result = retention_cleanup_coordinator.run_startup_automatic(
                    enforce_image_free_space=False,
                )
                cleanup_errors = cleanup_result.get("errors") or {}
                if cleanup_errors.get("logs"):
                    logger.error({"event": "log_startup_cleanup_failed", "error": cleanup_errors["logs"]})
                if cleanup_errors.get("images"):
                    logger.error({"event": "image_startup_cleanup_failed", "error": cleanup_errors["images"]})
            except Exception as exc:
                logger.error({"event": "retention_startup_cleanup_failed", "error": str(exc)})
            try:
                dashboard_metrics_service.sync_from_log_service(log_service)
            except Exception as exc:
                logger.error({"event": "dashboard_metrics_startup_sync_failed", "error": str(exc)})
            try:
                account_service.cleanup_auto_remove_accounts()
            except Exception as exc:
                logger.error({"event": "account_startup_cleanup_failed", "error": str(exc)})
            logger.info({"event": "startup_maintenance_finished"})

        if singleton_background and env_int("CHATGPT2API_IMPORT_WORKER_ENABLED", 1, 0, 1):
            from services.account_ingest_service import get_account_ingest_service
            import_service = await run_in_threadpool(get_account_ingest_service)
            import_service.start()
        if singleton_background:
            start_genbox_push_service()
            image_task_service.start()
        if singleton_background:
            startup_maintenance_thread = Thread(
                target=run_startup_maintenance,
                name="startup-maintenance",
                daemon=True,
            )
            startup_maintenance_thread.start()
        stop_event = Event()
        thread = start_account_lifecycle_watcher(stop_event) if singleton_background else None
        replenishment_thread = start_account_replenishment_watcher(stop_event) if singleton_background else None
        cleanup_thread = start_retention_cleanup_scheduler(stop_event) if singleton_background else None
        dashboard_metrics_thread = dashboard_metrics_service.start_refresh_scheduler(log_service, stop_event) if singleton_background else None
        if singleton_background:
            backup_service.start()
        try:
            yield
        finally:
            stop_event.set()
            await run_in_threadpool(page_prewarm_pool.stop())
            if startup_maintenance_thread is not None:
                startup_maintenance_thread.join(timeout=1)
            if import_service is not None:
                await run_in_threadpool(import_service.stop)
            if thread is not None:
                thread.join(timeout=1)
            if replenishment_thread is not None:
                replenishment_thread.join(timeout=1)
            if dashboard_metrics_thread is not None:
                dashboard_metrics_thread.join(timeout=1)
            if cleanup_thread is not None:
                await run_in_threadpool(cleanup_thread.join, RETENTION_SHUTDOWN_TIMEOUT_SECS)
            if singleton_background:
                await run_in_threadpool(image_task_service.shutdown_cancel_pending_and_wait)
                await run_in_threadpool(shutdown_genbox_push_service)
            if singleton_background:
                try:
                    await run_in_threadpool(
                        dashboard_metrics_service.sync_from_log_service,
                        log_service,
                    )
                except Exception as exc:
                    logger.error({
                        "event": "dashboard_metrics_shutdown_sync_failed",
                        "error": str(exc),
                    })
                backup_service.stop()
            await run_in_threadpool(credential_log_runner.shutdown, wait=True)
    app = FastAPI(title="chatgpt2api", version=app_version, lifespan=lifespan)
    install_exception_handlers(app)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Export-Requested", "X-Exported", "X-Skipped"],
    )
    app.include_router(ai.create_router())
    app.include_router(accounts.create_router())
    app.include_router(account_ingest.create_router())
    app.include_router(image_tasks.create_router())
    app.include_router(prompts.create_router())
    app.include_router(system.create_router(app_version))

    @app.api_route("/{full_path:path}", methods=["GET", "HEAD"], include_in_schema=False)
    async def serve_web(full_path: str):
        asset = resolve_web_asset(full_path)
        if asset is None:
            raise HTTPException(status_code=404, detail="Not Found")
        # The HTML entry point contains the hashed module URLs. Keeping it
        # fresh is essential after a frontend hotfix; otherwise a browser can
        # keep an old entry point and never request the repaired chunks.
        headers = None
        if asset.name == "index.html":
            headers = {"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"}
        return FileResponse(asset, headers=headers)

    return app
