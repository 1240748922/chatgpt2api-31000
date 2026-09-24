from __future__ import annotations

import time
from collections import deque
from itertools import islice
from pathlib import Path
from threading import Event, Thread

from fastapi import HTTPException, Request

from services.account_service import account_service
from services.maintenance_load import configured_thresholds
from services.account_maintenance_policy import account_maintenance_decision
from services.account_maintenance import sync_idle_batch, renew_idle_batch, maintenance_cycle_delay
from services.account_maintenance_progress import account_maintenance_progress
from services.account_replenishment_service import account_replenishment_service
from services.auth_service import auth_service
from services.config import config

BASE_DIR = Path(__file__).resolve().parents[1]
WEB_DIST_DIR = BASE_DIR / "web_dist"


def extract_bearer_token(authorization: str | None) -> str:
    scheme, _, value = str(authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        return ""
    return value.strip()


def _legacy_admin_identity(token: str) -> dict[str, object] | None:
    auth_key = str(config.auth_key or "").strip()
    if auth_key and token == auth_key:
        return {"id": "admin", "name": "管理员", "role": "admin"}
    return None


def require_identity(authorization: str | None) -> dict[str, object]:
    token = extract_bearer_token(authorization)
    identity = _legacy_admin_identity(token) or auth_service.authenticate(token)
    if identity is None:
        raise HTTPException(status_code=401, detail={"error": "密钥无效或已失效，请重新登录"})
    return identity


def require_auth_key(authorization: str | None) -> None:
    require_identity(authorization)


def require_admin(authorization: str | None) -> dict[str, object]:
    identity = require_identity(authorization)
    if identity.get("role") != "admin":
        raise HTTPException(status_code=403, detail={"error": "需要管理员权限才能执行这个操作"})
    return identity


def resolve_image_base_url(request: Request) -> str:
    return config.base_url or f"{request.url.scheme}://{request.headers.get('host', request.url.netloc)}"


def sanitize_cpa_pool(pool: dict | None) -> dict | None:
    if not isinstance(pool, dict):
        return None
    return {key: value for key, value in pool.items() if key != "secret_key"}


def sanitize_cpa_pools(pools: list[dict]) -> list[dict]:
    return [sanitized for pool in pools if (sanitized := sanitize_cpa_pool(pool)) is not None]


def sanitize_sub2api_server(server: dict | None) -> dict | None:
    if not isinstance(server, dict):
        return None
    sanitized = {key: value for key, value in server.items() if key not in {"password", "api_key"}}
    sanitized["has_api_key"] = bool(str(server.get("api_key") or "").strip())
    return sanitized


def sanitize_sub2api_servers(servers: list[dict]) -> list[dict]:
    return [sanitized for server in servers if (sanitized := sanitize_sub2api_server(server)) is not None]


def start_account_lifecycle_watcher(stop_event: Event) -> Thread:
    def worker() -> None:
        account_maintenance_progress.started()
        try:
            worker_body()
        finally:
            account_maintenance_progress.stopped()

    def worker_body() -> None:
        next_lifecycle = 0.0
        next_verification = 0.0
        next_quota_discovery = 0.0
        limited_pending: deque[str] = deque()
        expiring_pending: deque[str] = deque()
        unknown_pending: deque[str] = deque()
        while not stop_event.is_set():
            account_maintenance_progress.checking()
            retry = configured_thresholds()[2]
            interval = retry
            advanced = 0
            try:
                decision = account_maintenance_decision()
                allowed = decision["allowed"]
                if allowed:
                    if time.monotonic() >= next_lifecycle and not (limited_pending or expiring_pending):
                        limited_pending.extend(account_service.list_limited_tokens())
                        expiring_pending.extend(account_service.list_expiring_access_tokens())
                        next_lifecycle = time.monotonic() + config.refresh_account_interval_minute * 60
                    # Carry unfinished work into the next maintenance cycle, rather
                    # than repeatedly checking just the first accounts.
                    decision = account_maintenance_decision()
                    allowed = decision["allowed"]
                    if allowed and not stop_event.is_set() and time.monotonic() >= next_verification:
                        # A fast continuation must not repeatedly scan the pool
                        # or flood the separate authentication-verification pool.
                        next_verification = time.monotonic() + retry
                        account_service.resume_pending_auth_verifications(limit=decision["batch_size"])
                    decision = account_maintenance_decision()
                    allowed = decision["allowed"]
                    if allowed and not stop_event.is_set() and expiring_pending:
                        # Every small batch rechecks performance; image count
                        # alone no longer prevents credential maintenance.
                        expiring = list(islice(expiring_pending, 50))
                        checked = renew_idle_batch(account_service, expiring, stop_event)
                        advanced += checked
                        for _ in range(checked):
                            expiring_pending.popleft()
                    decision = account_maintenance_decision()
                    allowed = decision["allowed"]
                    if allowed and not stop_event.is_set():
                        discovered = False
                        if not unknown_pending and time.monotonic() >= next_quota_discovery:
                            next_quota_discovery = time.monotonic() + retry
                            unknown_pending.extend(account_service.list_unknown_quota_tokens())
                            discovered = True
                        tokens = list(islice(unknown_pending, 50))
                        if tokens:
                            if not discovered:
                                # Only inspect the queued IDs, without a whole-pool
                                # refresh/scan. Drop deleted, busy or already-checked
                                # entries and follow AT rotation before dispatch.
                                eligible = account_service.list_unknown_quota_tokens(
                                    limit=len(tokens), candidate_tokens=tokens,
                                )
                                for _ in tokens:
                                    unknown_pending.popleft()
                                unknown_pending.extendleft(reversed(eligible))
                                advanced += len(tokens) - len(eligible)
                                tokens = eligible
                            checked = sync_idle_batch(account_service, tokens, stop_event)
                            advanced += checked
                            for _ in range(checked):
                                unknown_pending.popleft()
                            print(f"[account-watcher] unknown quota checked={checked} selected={len(tokens)}")
                    limited = list(islice(limited_pending, 50))
                    checked = sync_idle_batch(account_service, limited, stop_event)
                    advanced += checked
                    for _ in range(checked):
                        limited_pending.popleft()
                if stop_event.is_set():
                    break
                if advanced:
                    # Recheck pressure AFTER this turn too. Healthy activity can
                    # continue; pauses, uncertain samples and legacy mode cannot.
                    interval = maintenance_cycle_delay(
                        account_maintenance_decision(),
                        pending=bool(expiring_pending or limited_pending or unknown_pending),
                        advanced=advanced,
                        next_discovery_in=max(0, next_quota_discovery - time.monotonic()),
                    )
            except Exception as exc:
                print(f"[account-watcher] fail {exc}")
            # Idle/error/pressure waits retain the original retry interval;
            # normal backlog gets a short, interruptible cooperative yield.
            account_maintenance_progress.waiting(interval)
            stop_event.wait(interval)

    thread = Thread(target=worker, name="account-lifecycle-watcher", daemon=True)
    thread.start()
    return thread


def start_limited_account_watcher(stop_event: Event) -> Thread:
    """Compatibility alias for integrations importing the old watcher name."""
    return start_account_lifecycle_watcher(stop_event)


def start_account_replenishment_watcher(stop_event: Event) -> Thread:
    return account_replenishment_service.start_scheduler(stop_event)


def resolve_web_asset(requested_path: str) -> Path | None:
    if not WEB_DIST_DIR.exists():
        return None
    clean_path = requested_path.strip("/")
    base_dir = WEB_DIST_DIR.resolve()
    candidates = [base_dir / "index.html"] if not clean_path else [
        base_dir / Path(clean_path),
        base_dir / clean_path / "index.html",
        base_dir / f"{clean_path}.html",
    ]
    for candidate in candidates:
        try:
            candidate.resolve().relative_to(base_dir)
        except ValueError:
            continue
        if candidate.is_file():
            return candidate
    return None
