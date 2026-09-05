from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from services.account_service import account_service
from services.config import config
from services.storage.coordination_repository import AccountReplenishmentRepository
from services.json_file import read_json_object, write_json_file
from utils.log import logger


_SAVED_SESSION_RE = re.compile(r"Saved session:\s*(.+)$")


def _text(value: object, default: str = "") -> str:
    if value is None:
        return default
    return str(value).strip()


def _tail(text: str, limit: int = 4000) -> str:
    value = str(text or "")
    if len(value) <= limit:
        return value
    return value[-limit:]


def _mask_secret(value: object) -> str:
    secret = _text(value)
    if not secret:
        return ""
    if len(secret) <= 7:
        return "*" * len(secret)
    return f"{secret[:3]}****{secret[-3:]}"


def _default_workdir() -> Path:
    project_root = Path(__file__).resolve().parents[2]
    bundled = project_root / "GPT-Register-Tool-main"
    if bundled.exists():
        return bundled
    sibling = project_root.parent / "GPT-Register-Tool-main"
    if sibling.exists():
        return sibling
    return project_root


def _resolve_path(raw: str, base_dir: Path) -> Path:
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    return path


def _split_args(raw: str) -> list[str]:
    value = _text(raw)
    if not value:
        return []
    try:
        return shlex.split(value, posix=os.name != "nt")
    except ValueError:
        return value.split()


class AccountReplenishmentService:
    def __init__(
        self,
        *,
        repository: AccountReplenishmentRepository | None = None,
    ) -> None:
        self._repository = repository or AccountReplenishmentRepository()
        # A manual run reserves this lock in the API thread and releases it in
        # the dedicated worker thread, so it must support cross-thread release.
        self._run_lock = threading.Lock()
        self._state_lock = threading.RLock()
        self._scheduler_lock = threading.Lock()
        self._scheduler_stop_event: threading.Event | None = None
        self._scheduler_thread: threading.Thread | None = None
        self._wake_event = threading.Event()
        self._status_lock = threading.Lock()
        self._scheduler_active = False
        self._scheduler_status_owner: object | None = None
        self._running = False
        self._last_started_at: str | None = None
        self._last_finished_at: str | None = None
        self._next_run_at: str | None = None
        self._last_checked_at: str | None = None
        self._last_triggered_at: str | None = None
        self._last_error: str | None = None
        self._last_result: dict[str, Any] = {}
        self._last_metrics: dict[str, Any] = {}
        self._log_history: list[dict[str, Any]] = []

    @staticmethod
    def _timestamp(value: float | None = None) -> str:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time() if value is None else value))

    @staticmethod
    def _empty_status() -> dict[str, Any]:
        return {
            "running": False,
            "last_started_at": None,
            "last_finished_at": None,
            "next_run_at": None,
            "last_checked_at": None,
            "last_triggered_at": None,
            "last_error": None,
            "last_result": {},
            "last_metrics": {},
            "log_history": [],
        }

    @contextmanager
    def _shared_state(self):
        with self._state_lock:
            with self._repository.edit() as state:
                yield state

    @contextmanager
    def _run_owner(self):
        if not self._run_lock.acquire(blocking=False):
            raise TimeoutError("account replenishment is already running")
        try:
            with self._repository.run_lock(timeout_seconds=0):
                yield
        finally:
            self._run_lock.release()

    def _mark_started(self) -> None:
        now = self._timestamp()
        with self._status_lock:
            self._running = True
            self._last_started_at = now
            if self._repository is not None:
                with self._shared_state() as state:
                    state["running"] = True
                    state["last_started_at"] = now

    def _update_live_result(self, *, batch_id: str, log_path: Path, output: str) -> None:
        result = {
            "ok": None,
            "triggered": True,
            "reason": "running",
            "batch_id": batch_id,
            "log_path": str(log_path),
            "stdout_tail": _tail(output),
        }
        with self._status_lock:
            self._last_result = dict(result)
            if self._repository is not None:
                with self._shared_state() as state:
                    state["last_result"] = dict(result)

    def _mark_starting(self, requested_count: int) -> None:
        now = self._timestamp()
        result = {
            "ok": None,
            "triggered": True,
            "reason": "starting",
            "manual_count": requested_count,
            "requested_count": requested_count,
            "stdout_tail": "",
        }
        with self._status_lock:
            self._running = True
            self._last_started_at = now
            self._last_error = None
            self._last_result = dict(result)
            if self._repository is not None:
                with self._shared_state() as state:
                    state.update({
                        "running": True,
                        "last_started_at": now,
                        "last_error": None,
                        "last_result": dict(result),
                    })

    def _mark_finished(self, *, result: dict[str, Any], error: str | None = None) -> None:
        now = self._timestamp()
        history_entry = {
            key: value
            for key, value in result.items()
            if key not in {"command", "workdir", "entrypoint", "output_dir"}
        }
        history_entry["finished_at"] = now
        if error:
            history_entry["error"] = error
        with self._status_lock:
            self._running = False
            self._last_finished_at = now
            self._last_error = error
            self._last_result = dict(result)
            self._log_history = [*self._log_history, history_entry][-50:]
            if self._repository is not None:
                with self._shared_state() as state:
                    persisted_history = state.get("log_history")
                    if not isinstance(persisted_history, list):
                        persisted_history = []
                    next_history = [*persisted_history, history_entry][-50:]
                    self._log_history = next_history
                    state.update({
                        "running": False,
                        "last_finished_at": now,
                        "last_error": error,
                        "last_result": dict(result),
                        "log_history": next_history,
                    })

    def _set_next_run(self, timestamp: float | None) -> None:
        next_run = self._timestamp(timestamp) if timestamp is not None else None
        with self._status_lock:
            self._next_run_at = next_run
            if self._repository is not None:
                with self._shared_state() as state:
                    state["next_run_at"] = next_run

    def _touch_check(self, metrics: dict[str, Any]) -> None:
        now = self._timestamp()
        with self._status_lock:
            self._last_checked_at = now
            self._last_metrics = dict(metrics)
            if self._repository is not None:
                with self._shared_state() as state:
                    state["last_checked_at"] = now
                    state["last_metrics"] = dict(metrics)

    def status(self) -> dict[str, Any]:
        metrics = {}
        try:
            metrics = account_service.evaluate_account_pool(refresh_stale=False)
        except Exception as exc:
            metrics = {"error": str(exc)}

        if self._repository is not None:
            with self._shared_state() as state:
                status = self._empty_status()
                for field in status:
                    if field in state:
                        status[field] = state[field]
                for field in ("last_result", "last_metrics"):
                    if isinstance(state.get(field), dict):
                        status[field] = dict(state[field])
                if isinstance(state.get("log_history"), list):
                    status["log_history"] = list(state["log_history"])[-50:]
                status["config"] = self._config_snapshot()
                status["current_metrics"] = metrics
                return status

        with self._status_lock:
            return {
                "running": self._running,
                "last_started_at": self._last_started_at,
                "last_finished_at": self._last_finished_at,
                "next_run_at": self._next_run_at,
                "last_checked_at": self._last_checked_at,
                "last_triggered_at": self._last_triggered_at,
                "last_error": self._last_error,
                "last_result": dict(self._last_result),
                "last_metrics": dict(self._last_metrics),
                "log_history": list(self._log_history)[-50:],
                "config": self._config_snapshot(),
                "current_metrics": metrics,
            }

    def _config_snapshot(self) -> dict[str, Any]:
        settings = dict(config.account_replenishment)
        settings["workdir"] = _text(settings.get("workdir")) or str(_default_workdir())
        return settings

    def _resolve_tool_paths(self, settings: dict[str, Any]) -> tuple[Path, Path, Path, Path]:
        workdir = _resolve_path(_text(settings.get("workdir")) or str(_default_workdir()), Path.cwd())
        if not workdir.exists():
            raise FileNotFoundError(f"registration workdir not found: {workdir}")
        entrypoint = _resolve_path(_text(settings.get("entrypoint"), "chatgpt_phone_reg.py"), workdir)
        if not entrypoint.exists():
            raise FileNotFoundError(f"registration entrypoint not found: {entrypoint}")
        output_dir = _resolve_path(
            _text(settings.get("output_dir")) or "sessions",
            workdir,
        )
        output_dir.mkdir(parents=True, exist_ok=True)
        python_executable = _text(settings.get("python_executable"))
        if python_executable:
            python_path = _resolve_path(python_executable, workdir)
        else:
            candidates = [
                workdir / ".venv" / "Scripts" / "python.exe",
                workdir / ".venv" / "bin" / "python",
            ]
            python_path = next((candidate for candidate in candidates if candidate.exists()), Path(sys.executable))
        return workdir, output_dir, entrypoint, python_path

    def _provider_config_path(self) -> Path:
        settings = dict(config.account_replenishment)
        workdir = _resolve_path(_text(settings.get("workdir")) or str(_default_workdir()), Path.cwd())
        if not workdir.exists():
            raise FileNotFoundError(f"registration workdir not found: {workdir}")
        path = workdir / "config.json"
        if not path.exists():
            raise FileNotFoundError(f"registration config not found: {path}")
        return path

    def provider_config(self) -> dict[str, Any]:
        path = self._provider_config_path()
        raw = read_json_object(path, name="registration config")
        email = raw.get("email_registration") if isinstance(raw.get("email_registration"), dict) else {}
        remail = email.get("remail") if isinstance(email.get("remail"), dict) else {}
        smailr = email.get("smailr") if isinstance(email.get("smailr"), dict) else {}
        phone = raw.get("phone_reuse") if isinstance(raw.get("phone_reuse"), dict) else {}
        smsbower = phone.get("smsbower") if isinstance(phone.get("smsbower"), dict) else {}
        proxy = raw.get("proxy") if isinstance(raw.get("proxy"), dict) else {}
        proxy_pool = proxy.get("pool") if isinstance(proxy.get("pool"), list) else []
        return {
            "config_path": str(path),
            "registration_proxy": _text(proxy.get("registration")),
            "registration_proxy_pool": "\n".join(_text(item) for item in proxy_pool if _text(item)),
            "remail_base_url": _text(remail.get("base_url"), "https://remail.aishop6.com"),
            "remail_api_key": "",
            "remail_has_api_key": bool(_text(remail.get("api_key"))),
            "remail_api_key_masked": _mask_secret(remail.get("api_key")),
            "smailr_base_url": _text(smailr.get("base_url"), "https://smailr.com"),
            "smailr_api_key": "",
            "smailr_has_api_key": bool(_text(smailr.get("api_key"))),
            "smailr_api_key_masked": _mask_secret(smailr.get("api_key")),
            "cfworker_url": _text(email.get("cfworker_url")),
            "cfworker_admin_token": "",
            "cfworker_has_admin_token": bool(_text(email.get("cfworker_admin_token"))),
            "cfworker_admin_token_masked": _mask_secret(email.get("cfworker_admin_token")),
            "cfworker_api_token": "",
            "cfworker_has_api_token": bool(_text(email.get("cfworker_api_token"))),
            "cfworker_api_token_masked": _mask_secret(email.get("cfworker_api_token")),
            "smsbower_api_key": "",
            "smsbower_has_api_key": bool(_text(smsbower.get("api_key"))),
            "smsbower_api_key_masked": _mask_secret(smsbower.get("api_key")),
            "smsbower_country": _text(smsbower.get("country"), "38"),
        }

    def update_provider_config(self, updates: Mapping[str, Any]) -> dict[str, Any]:
        path = self._provider_config_path()
        raw = read_json_object(path, name="registration config")
        email = dict(raw.get("email_registration") or {})
        remail = dict(email.get("remail") or {})
        smailr = dict(email.get("smailr") or {})
        phone = dict(raw.get("phone_reuse") or {})
        smsbower = dict(phone.get("smsbower") or {})
        proxy = dict(raw.get("proxy") or {})

        def update(target: dict[str, Any], key: str, value: Any) -> None:
            text = _text(value)
            if text:
                target[key] = text

        update(remail, "base_url", updates.get("remail_base_url"))
        update(remail, "api_key", updates.get("remail_api_key"))
        update(smailr, "base_url", updates.get("smailr_base_url"))
        update(smailr, "api_key", updates.get("smailr_api_key"))
        update(email, "cfworker_url", updates.get("cfworker_url"))
        update(email, "cfworker_admin_token", updates.get("cfworker_admin_token"))
        update(email, "cfworker_api_token", updates.get("cfworker_api_token"))
        update(smsbower, "api_key", updates.get("smsbower_api_key"))
        update(smsbower, "country", updates.get("smsbower_country"))
        # Proxy fields are intentionally different from secret fields: an
        # explicitly submitted empty value means "clear this setting".
        if "registration_proxy" in updates:
            proxy["registration"] = _text(updates.get("registration_proxy"))
        if "registration_proxy_pool" in updates:
            proxy_pool = _text(updates.get("registration_proxy_pool"))
            proxy["pool"] = [
                item
                for raw_item in re.split(r"[\r\n,]+", proxy_pool)
                if (item := _text(raw_item))
            ]
        # The UI intentionally exposes only the fixed proxy and pool. When
        # both are explicitly cleared, clear the legacy hidden default too;
        # otherwise the provider config can silently fall back to the local
        # desktop proxy (127.0.0.1:7897) inside a Docker container.
        if (
            "registration_proxy" in updates
            and "registration_proxy_pool" in updates
            and not _text(updates.get("registration_proxy"))
            and not _text(updates.get("registration_proxy_pool"))
        ):
            proxy["default"] = ""

        email["remail"] = remail
        email["smailr"] = smailr
        raw["email_registration"] = email
        phone["smsbower"] = smsbower
        raw["phone_reuse"] = phone
        raw["proxy"] = proxy
        write_json_file(path, raw)
        return self.provider_config()

    def _registration_configuration_error(self, settings: Mapping[str, Any]) -> str | None:
        source = _text(settings.get("registration_source"), "configured").lower()
        if source not in {"remail_target", "smailr", "phone"}:
            return None
        try:
            raw = read_json_object(self._provider_config_path(), name="registration config")
        except (FileNotFoundError, ValueError) as exc:
            return f"注册工具配置不可用：{exc}"

        email = raw.get("email_registration") if isinstance(raw.get("email_registration"), dict) else {}
        if source == "remail_target":
            remail = email.get("remail") if isinstance(email.get("remail"), dict) else {}
            if not (_text(os.environ.get("REMAIL_API_KEY")) or _text(remail.get("api_key"))):
                return "未配置 ReMail API Key，请在注册机页面填写后保存邮箱与代理配置。"
        elif source == "smailr":
            smailr = email.get("smailr") if isinstance(email.get("smailr"), dict) else {}
            if not (_text(os.environ.get("SMAILR_API_KEY")) or _text(smailr.get("api_key"))):
                return "未配置 Smailr API Key，请在注册机页面填写后保存邮箱与代理配置。"
        elif source == "phone":
            phone = raw.get("phone_reuse") if isinstance(raw.get("phone_reuse"), dict) else {}
            smsbower = phone.get("smsbower") if isinstance(phone.get("smsbower"), dict) else {}
            configured_key = _text(smsbower.get("api_key"))
            if configured_key.startswith("$"):
                configured_key = _text(os.environ.get(configured_key[1:]))
            if not (configured_key or _text(os.environ.get("SMSBOWER_API_KEY"))):
                return "未配置 SMSBower API Key，请在注册机页面填写后保存邮箱与代理配置。"
        return None

    def import_mailbox_file(self, filename: str, content: str) -> dict[str, str]:
        text = str(content or "")
        if not text.strip():
            raise ValueError("mailbox file is empty")
        if len(text.encode("utf-8")) > 10 * 1024 * 1024:
            raise ValueError("mailbox file is larger than 10 MB")
        settings = dict(config.account_replenishment)
        workdir = _resolve_path(_text(settings.get("workdir")) or str(_default_workdir()), Path.cwd())
        workdir.mkdir(parents=True, exist_ok=True)
        safe_name = Path(str(filename or "mailboxes.txt")).name or "mailboxes.txt"
        if Path(safe_name).suffix.lower() not in {".txt", ".json", ".csv"}:
            raise ValueError("mailbox file must be .txt, .json, or .csv")
        target = workdir / safe_name
        target.write_text(text, encoding="utf-8")
        return {"mailbox_file": str(target), "filename": safe_name}

    def _build_command(
        self,
        *,
        python_executable: Path,
        entrypoint: Path,
        output_dir: Path,
        settings: dict[str, Any],
        batch_size: int,
        batch_id: str,
    ) -> list[str]:
        source = _text(settings.get("registration_source"), "configured").lower()
        registration_mode = _text(settings.get("registration_mode"), "password").lower()
        if registration_mode not in {"password", "passwordless", "har", "legacy"}:
            registration_mode = "password"
        command = [str(python_executable), str(entrypoint)]
        command.extend(["--registration-mode", registration_mode])
        if source == "remail_target":
            command.extend([
                "--target-at200",
                str(batch_size),
                "--buy-remail-mailbox",
                "--remail-service-mode",
                _text(settings.get("remail_service_mode"), "purchase") or "purchase",
                "--remail-supply",
                _text(settings.get("remail_supply"), "private_first") or "private_first",
                "--remail-email-suffix",
                _text(settings.get("remail_email_suffix"), "outlook.com") or "outlook.com",
                "--remail-project-id",
                str(max(1, int(settings.get("remail_project_id") or 2))),
            ])
        else:
            command.extend(["--count", str(batch_size)])
            if source == "mailbox_file":
                mailbox_raw = _text(settings.get("mailbox_file"))
                if not mailbox_raw:
                    raise ValueError("mailbox_file is required when registration_source is mailbox_file")
                mailbox_file = _resolve_path(mailbox_raw, entrypoint.parent)
                command.extend(["--mailbox-file", str(mailbox_file), "--registration-at-only"])
            elif source == "cfworker":
                command.extend([
                    "--buy-cfworker-mailbox",
                    "--cfworker-domain",
                    _text(settings.get("cfworker_domain")),
                    "--registration-at-only",
                ])
            elif source == "smailr":
                command.extend([
                    "--buy-smailr-mailbox",
                    "--smailr-domain",
                    _text(settings.get("smailr_domain")),
                    "--registration-at-only",
                ])
            elif source == "phone":
                command.append("--phone-register")
        command.extend([
            "--workers",
            str(max(1, int(settings.get("workers") or 1))),
            "--output-dir",
            str(output_dir),
            "--registration-batch-id",
            batch_id,
        ])
        if _text(settings.get("no_2fa"), "true").lower() in {"1", "true", "yes", "on"}:
            command.append("--no-2fa")
        if source != "phone" and _text(settings.get("no_phone_reuse"), "true").lower() in {"1", "true", "yes", "on"}:
            command.append("--no-phone-reuse")
        command.extend(_split_args(settings.get("extra_args") or ""))
        return command

    @staticmethod
    def _extract_saved_session_paths(stdout: str) -> list[Path]:
        paths: list[Path] = []
        for line in str(stdout or "").splitlines():
            match = _SAVED_SESSION_RE.search(line)
            if match:
                raw = _text(match.group(1))
                if raw:
                    paths.append(Path(raw))
        return paths

    @staticmethod
    def _session_payload(path: Path) -> dict[str, Any] | None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
        return data if isinstance(data, dict) else None

    def _recent_session_paths(self, output_dir: Path, started_at: float) -> list[Path]:
        candidates: list[Path] = []
        for path in sorted(output_dir.glob("session_*.json")):
            try:
                if path.stat().st_mtime >= started_at - 1:
                    candidates.append(path)
            except OSError:
                continue
        return candidates

    def _import_sessions(
        self,
        session_paths: list[Path],
        *,
        batch_id: str = "",
        account_proxy: str = "",
    ) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        imported_paths: list[str] = []
        for path in dict.fromkeys(session_paths):
            payload = self._session_payload(path)
            if not payload:
                continue
            if batch_id and _text(payload.get("batch_id")) != batch_id:
                continue
            access_token = _text(payload.get("access_token"))
            if not access_token:
                continue
            if account_proxy and not _text(payload.get("proxy")):
                payload = {**payload, "proxy": account_proxy}
            items.append(payload)
            imported_paths.append(str(path))
        if not items:
            return {
                "imported": 0,
                "skipped": 0,
                "paths": imported_paths,
                "proxy_assigned": False,
                "_access_tokens": [],
            }
        result = account_service.add_account_items(items, return_items=False)
        return {
            "imported": int(result.get("added") or 0),
            "skipped": int(result.get("skipped") or 0),
            "paths": imported_paths,
            "proxy_assigned": bool(account_proxy),
            "_access_tokens": [
                _text(item.get("access_token") or item.get("accessToken"))
                for item in items
                if _text(item.get("access_token") or item.get("accessToken"))
            ],
        }

    def run_once(
        self,
        *,
        force: bool = False,
        requested_count: int | None = None,
    ) -> dict[str, Any]:
        try:
            with self._run_owner():
                return self._run_once(force=force, requested_count=requested_count)
        except TimeoutError:
            with self._status_lock:
                result = dict(self._last_result)
            return {
                "ok": None,
                "triggered": False,
                "reason": "busy",
                "running": True,
                "batch_id": result.get("batch_id"),
                "log_path": result.get("log_path"),
            }

    def start_manual(self, requested_count: int) -> dict[str, Any]:
        requested_count = int(requested_count)
        if not 1 <= requested_count <= 1000:
            raise ValueError("手动注册数量必须在 1 到 1000 之间")
        if not self._run_lock.acquire(blocking=False):
            with self._status_lock:
                result = dict(self._last_result)
            return {
                "ok": None,
                "triggered": False,
                "reason": "busy",
                "running": True,
                "batch_id": result.get("batch_id"),
                "log_path": result.get("log_path"),
            }

        self._mark_starting(requested_count)

        def worker() -> None:
            try:
                with self._repository.run_lock(timeout_seconds=0):
                    self._run_once(force=True, requested_count=requested_count)
            except TimeoutError as exc:
                message = str(exc) or "已有其他实例正在执行注册"
                self._mark_finished(
                    result={
                        "ok": False,
                        "triggered": True,
                        "reason": "failed",
                        "manual_count": requested_count,
                        "requested_count": requested_count,
                        "stderr_tail": message,
                    },
                    error=message,
                )
            except Exception as exc:
                message = str(exc) or type(exc).__name__
                self._mark_finished(
                    result={
                        "ok": False,
                        "triggered": True,
                        "reason": "failed",
                        "manual_count": requested_count,
                        "requested_count": requested_count,
                        "stderr_tail": message,
                    },
                    error=message,
                )
            finally:
                self._run_lock.release()

        thread = threading.Thread(
            target=worker,
            name="account-replenishment-manual",
            daemon=True,
        )
        thread.start()
        return {
            "ok": True,
            "triggered": True,
            "reason": "started",
            "running": True,
            "manual_count": requested_count,
            "requested_count": requested_count,
        }

    def _run_once(
        self,
        *,
        force: bool = False,
        requested_count: int | None = None,
    ) -> dict[str, Any]:
        if requested_count is not None:
            requested_count = int(requested_count)
            if not 1 <= requested_count <= 1000:
                raise ValueError("手动注册数量必须在 1 到 1000 之间")
            force = True
        settings = self._config_snapshot()
        enabled = bool(settings.get("enabled"))
        minimum_available = max(0, int(settings.get("minimum_available") or 0))
        target_available = max(
            minimum_available,
            int(settings.get("target_available") or 0),
        )
        max_batch_size = max(1, int(settings.get("max_batch_size") or 1))
        cooldown_seconds = max(0, int(settings.get("cooldown_seconds") or 0))
        interval_seconds = max(1, int(settings.get("interval_seconds") or 1))
        launch_timeout_seconds = max(300, int(settings.get("launch_timeout_seconds") or 300))

        try:
            quick_metrics = account_service.evaluate_account_pool(refresh_stale=False)
        except Exception as exc:
            quick_metrics = {"error": str(exc)}
        metrics = dict(quick_metrics)

        if not force and not enabled:
            self._touch_check(metrics)
            result = {
                "ok": True,
                "triggered": False,
                "reason": "disabled",
                "metrics": metrics,
            }
            self._set_next_run(time.time() + interval_seconds)
            self._mark_finished(result=result)
            return result

        trigger_threshold = target_available if minimum_available <= 0 else minimum_available
        if not force and (trigger_threshold <= 0 or int(metrics.get("current_available") or 0) >= trigger_threshold):
            self._touch_check(metrics)
            result = {
                "ok": True,
                "triggered": False,
                "reason": "enough_accounts",
                "metrics": metrics,
            }
            self._set_next_run(time.time() + interval_seconds)
            self._mark_finished(result=result)
            return result

        configuration_error = self._registration_configuration_error(settings)
        if configuration_error:
            self._touch_check(metrics)
            result = {
                "ok": False,
                "triggered": False,
                "reason": "configuration_required",
                "config_error": configuration_error,
                "metrics": metrics,
            }
            self._set_next_run(time.time() + interval_seconds)
            self._mark_finished(result=result, error=configuration_error)
            return result

        try:
            metrics = account_service.evaluate_account_pool(
                refresh_stale=True,
                target_available=trigger_threshold or None,
                freshness_seconds=interval_seconds,
            )
        except Exception as exc:
            metrics = account_service.evaluate_account_pool(refresh_stale=False)
            metrics["evaluation_error"] = str(exc)
        self._touch_check(metrics)

        current_available = int(metrics.get("current_available") or 0)
        desired_available = target_available or minimum_available
        deficit = max(0, desired_available - current_available)
        should_trigger = force or (enabled and trigger_threshold > 0 and current_available < trigger_threshold)
        if not force and not should_trigger:
            result = {
                "ok": True,
                "triggered": False,
                "reason": "enough_accounts",
                "metrics": metrics,
            }
            self._set_next_run(time.time() + interval_seconds)
            self._mark_finished(result=result)
            return result

        if requested_count is not None:
            batch_size = requested_count
        else:
            batch_size = max_batch_size if force and deficit <= 0 else min(max_batch_size, max(1, deficit))
        if batch_size <= 0:
            result = {
                "ok": True,
                "triggered": False,
                "reason": "no_batch_needed",
                "metrics": metrics,
            }
            self._set_next_run(time.time() + interval_seconds)
            self._mark_finished(result=result)
            return result

        workdir, output_dir, entrypoint, python_executable = self._resolve_tool_paths(settings)
        batch_id = f"account_replenishment_{time.strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:8]}"
        command = self._build_command(
            python_executable=python_executable,
            entrypoint=entrypoint,
            output_dir=output_dir,
            settings=settings,
            batch_size=batch_size,
            batch_id=batch_id,
        )
        started_at = time.time()
        log_dir = workdir / "runtime"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"{batch_id}.log"
        log_path.write_text("", encoding="utf-8")
        self._mark_started()
        with self._shared_state() as state:
            state["last_triggered_at"] = self._timestamp(started_at)
            state["last_result"] = {
                "ok": None,
                "triggered": True,
                "reason": "starting",
                "batch_id": batch_id,
                "log_path": str(log_path),
                "stdout_tail": "",
            }
        self._update_live_result(batch_id=batch_id, log_path=log_path, output="")
        env = os.environ.copy()
        env.setdefault("PYTHONUTF8", "1")
        env.setdefault("PYTHONIOENCODING", "utf-8")
        env.setdefault("PYTHONUNBUFFERED", "1")

        stdout = ""
        stderr = ""
        returncode = -1
        timed_out = False
        try:
            process = subprocess.Popen(
                command,
                cwd=str(workdir),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
            output_parts: list[str] = []

            def consume_output() -> None:
                if process.stdout is None:
                    return
                with log_path.open("a", encoding="utf-8") as log_file:
                    for line in process.stdout:
                        output_parts.append(line)
                        log_file.write(line)
                        log_file.flush()
                        self._update_live_result(
                            batch_id=batch_id,
                            log_path=log_path,
                            output="".join(output_parts),
                        )

            reader = threading.Thread(target=consume_output, name=f"replenishment-log-{batch_id}", daemon=True)
            reader.start()
            try:
                returncode = int(process.wait(timeout=launch_timeout_seconds) or 0)
            except subprocess.TimeoutExpired:
                timed_out = True
                process.kill()
                returncode = 124
            reader.join(timeout=10)
            stdout = "".join(output_parts)
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            stdout = _text(getattr(exc, "stdout", "") or getattr(exc, "output", ""))
            stderr = _text(getattr(exc, "stderr", ""))
            returncode = 124
        except OSError as exc:
            stderr = str(exc)
            try:
                log_path.write_text(f"{stderr}\n", encoding="utf-8")
            except OSError:
                pass

        session_paths = self._extract_saved_session_paths(stdout)
        if not session_paths:
            session_paths = self._recent_session_paths(output_dir, started_at)
        account_proxy = ""
        try:
            provider_values = self.provider_config()
            account_proxy = _text(provider_values.get("registration_proxy"))
            if not account_proxy:
                pool_values = [
                    _text(item)
                    for item in _text(provider_values.get("registration_proxy_pool")).splitlines()
                    if _text(item)
                ]
                account_proxy = pool_values[0] if len(pool_values) == 1 else ""
        except (FileNotFoundError, ValueError):
            # Import still works when provider config is unavailable; the UI reports
            # the missing proxy assignment through proxy_assigned=False.
            account_proxy = ""
        import_result: dict[str, Any] = {"imported": 0, "skipped": 0, "paths": []}
        if bool(settings.get("import_new_sessions")) and session_paths:
            import_result = self._import_sessions(
                session_paths,
                batch_id=batch_id,
                account_proxy=account_proxy,
            )

        imported_tokens = import_result.pop("_access_tokens", [])
        quota_refresh: dict[str, Any] = {"synced": 0, "errors": 0, "attempts": 0}
        if imported_tokens:
            self._update_live_result(
                batch_id=batch_id,
                log_path=log_path,
                output=f"{stdout}\n\n[系统] 注册完成，正在同步新账号额度...\n",
            )
            last_error = ""
            for attempt in range(1, 3):
                quota_refresh["attempts"] = attempt
                try:
                    quota_result = account_service.sync_accounts_and_quota(imported_tokens)
                    sync_errors = quota_result.get("errors") or []
                    quota_refresh["synced"] = max(
                        int(quota_refresh.get("synced") or 0),
                        int(quota_result.get("synced") or 0),
                    )
                    quota_refresh["errors"] = len(sync_errors)
                    if not sync_errors:
                        break
                    last_error = str(sync_errors[0].get("error") or "额度同步失败")
                    if attempt == 1:
                        self._update_live_result(
                            batch_id=batch_id,
                            log_path=log_path,
                            output=f"{stdout}\n\n[系统] 首次额度同步有 {len(sync_errors)} 个失败，正在重试...\n",
                        )
                        time.sleep(1)
                except Exception as exc:
                    last_error = str(exc)
                    quota_refresh["errors"] = len(imported_tokens)
                    if attempt == 1:
                        time.sleep(1)
            if last_error and quota_refresh.get("errors"):
                quota_refresh["error"] = last_error

        post_metrics = account_service.evaluate_account_pool(refresh_stale=False)
        result = {
            "ok": returncode == 0 and not timed_out,
            "triggered": True,
            "reason": "replenished" if returncode == 0 and not timed_out else "failed",
            "manual_count": requested_count,
            "batch_id": batch_id,
            "requested_count": batch_size,
            "current_available_before": current_available,
            "current_available_after": int(post_metrics.get("current_available") or 0),
            "metrics": metrics,
            "post_metrics": post_metrics,
            "import": import_result,
            "quota_refresh": quota_refresh,
            "returncode": returncode,
            "timed_out": timed_out,
            "stdout_tail": _tail(stdout),
            "stderr_tail": _tail(stderr),
            "log_path": str(log_path),
            "command": command,
            "workdir": str(workdir),
            "entrypoint": str(entrypoint),
            "output_dir": str(output_dir),
        }
        self._mark_finished(result=result, error=None if result["ok"] else result["stderr_tail"] or "registration_failed")
        next_wait = cooldown_seconds if result["ok"] else interval_seconds
        self._set_next_run(time.time() + max(1, next_wait))
        if result["ok"]:
            logger.info({
                "event": "account_replenishment_completed",
                "batch_id": batch_id,
                "requested_count": batch_size,
                "imported": import_result.get("imported", 0),
                "current_available_after": result["current_available_after"],
            })
        else:
            logger.error({
                "event": "account_replenishment_failed",
                "batch_id": batch_id,
                "returncode": returncode,
                "timed_out": timed_out,
                "stderr": _tail(stderr, 1000),
            })
        return result

    def _scheduler_loop(self, stop_event: threading.Event) -> None:
        while not stop_event.is_set():
            try:
                result = self.run_once(force=False)
                settings = self._config_snapshot()
                wait_key = "cooldown_seconds" if result.get("triggered") else "interval_seconds"
                wait_seconds = max(1, int(settings.get(wait_key) or 60))
            except Exception as exc:
                logger.error({
                    "event": "account_replenishment_scheduler_failed",
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                })
                wait_seconds = max(10, int(self._config_snapshot().get("interval_seconds") or 60))
            if stop_event.wait(wait_seconds):
                return

    def scheduler_worker(self, stop_event: threading.Event) -> None:
        while not stop_event.is_set():
            try:
                with self._repository.scheduler_leader_lock(timeout_seconds=0):
                    self._scheduler_loop(stop_event)
                    return
            except TimeoutError:
                if stop_event.wait(1.0):
                    return
            except Exception as exc:
                logger.error({
                    "event": "account_replenishment_scheduler_boot_failed",
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                })
                if stop_event.wait(5.0):
                    return

    def start_scheduler(self, stop_event: threading.Event) -> threading.Thread:
        with self._scheduler_lock:
            if self._scheduler_stop_event is stop_event and self._scheduler_thread is not None:
                if self._scheduler_thread.is_alive() or stop_event.is_set():
                    return self._scheduler_thread
            thread = threading.Thread(
                target=self.scheduler_worker,
                args=(stop_event,),
                daemon=True,
                name="account-replenishment",
            )
            self._scheduler_stop_event = stop_event
            self._scheduler_thread = thread
            self._set_next_run(time.time())
            thread.start()
            return thread


account_replenishment_service = AccountReplenishmentService()


def start_account_replenishment_watcher(stop_event: threading.Event) -> threading.Thread:
    return account_replenishment_service.start_scheduler(stop_event)
