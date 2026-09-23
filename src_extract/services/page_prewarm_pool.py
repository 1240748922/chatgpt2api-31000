"""Request-before-arrival page contexts, single-consumer and bounded.

Only GET / is performed: no requirements tokens, uploads, conversations or
image slots. Misses never wait. Sessions use a sequential transferable curl
handle; a context is destroyed after one request, never returned to inventory.
"""
from dataclasses import asdict
import hashlib
import json
from threading import Event, Lock, Thread
import time

from services.credential_coordinator import generation
from services.runtime_configuration import env_int, account_shard_settings


def context_key(account, profile):
    proxy = asdict(profile)
    for name in ("image_egress_reserved", "image_egress_wait_ms", "image_concurrency_limit"):
        proxy.pop(name, None)
    fingerprint = {k: account.get(k) for k in ("fp", "user-agent", "impersonate", "oai-device-id", "oai-session-id",
                                               "sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform")}
    payload = [generation(account), fingerprint, proxy, "chatgpt.com/page-v1"]
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


class PagePrewarmPool:
    def __init__(self, capacity=None, ttl=None):
        self.capacity = env_int("CHATGPT2API_PAGE_PREWARM_TARGET", 16, 0, 512) if capacity is None else capacity
        self.ttl = env_int("CHATGPT2API_PAGE_PREWARM_TTL_SECONDS", 45, 5, 120) if ttl is None else ttl
        self.lock, self.stop_event = Lock(), Event()
        self.entries, self.pending, self.threads = {}, set(), []
        self.mode = "not_started"
        self.cooldown_until = 0.0
        self.counters = dict(hit=0, miss=0, expired=0, discarded=0, failed=0, built=0)

    def candidates(self):
        with self.lock:
            return tuple(token for token, (_key, _backend, stamp) in self.entries.items() if time.monotonic() - stamp < self.ttl)

    def _prune(self):
        with self.lock:
            old = [token for token, (_key, _backend, stamp) in self.entries.items() if time.monotonic() - stamp >= self.ttl]
            removed = [self.entries.pop(token)[1] for token in old]
            self.counters["expired"] += len(removed)
        for backend in removed:
            backend.close()

    def put(self, backend):
        token = str(backend.access_token)
        with self.lock:
            accepted = not self.stop_event.is_set() and len(self.entries) < self.capacity and token not in self.entries
            if accepted:
                self.entries[token] = (context_key(backend.account, backend.proxy_profile), backend, time.monotonic())
                self.counters["built"] += 1
        if not accepted:
            backend.close()
        return accepted

    def take(self, account, profile):
        self._prune()
        with self.lock:
            item = self.entries.pop(str(account.get("access_token") or ""), None)
        if item and item[0] != context_key(account, profile):
            item[1].close()
            with self.lock:
                self.counters["discarded"] += 1
            item = None
        with self.lock:
            self.counters["hit" if item else "miss"] += 1
        return (item[1], int((time.monotonic() - item[2]) * 1000)) if item else (None, 0)

    def status(self):
        with self.lock:
            return {**self.counters, "ready": len(self.entries), "building": len(self.pending),
                    "target": self.capacity, "ttl_seconds": self.ttl, "scope": "local_instance", "mode": self.mode,
                    "cooldown_seconds": max(0, int(self.cooldown_until - time.monotonic()))}

    def start(self, accounts):
        self.threads = [thread for thread in self.threads if thread.is_alive()]
        if self.threads or not self.capacity or not accounts._strict_admission():
            return
        self.stop_event.clear()
        shards, index = account_shard_settings()
        total = env_int("CHATGPT2API_PAGE_PREWARM_WORKERS", 8, 0, 64)
        workers = total // shards + int(index < total % shards)
        rate = env_int("CHATGPT2API_PAGE_PREWARM_RATE", 8, 1, 256) / shards / max(1, workers)
        for number in range(workers):
            thread = Thread(target=self._work, args=(accounts, rate), name=f"page-inventory-{number}", daemon=True)
            self.threads.append(thread)
            thread.start()

    def defer_failure(self, exc):
        # Public-page throttling is shared network pressure, not an account
        # credential failure. Pause all local refill workers, not just this one.
        try:
            retry = max(5, int(getattr(exc, "retry_after", None) or 5))
        except (TypeError, ValueError, OverflowError):
            retry = 5
        with self.lock:
            self.cooldown_until = max(self.cooldown_until, time.monotonic() + retry)
            self.counters["failed"] += 1

    def _work(self, accounts, rate):
        from services.openai_backend_api import OpenAIBackendAPI
        from services.account_maintenance_policy import evaluate_pressure
        from services.maintenance_pressure import local_pressure_snapshot
        while not self.stop_event.is_set():
            with self.lock:
                cooldown = self.cooldown_until - time.monotonic()
            if cooldown > 0:
                self.stop_event.wait(min(cooldown, 60))
                continue
            backend = None
            token = None
            owned = False
            delay = max(1 / rate, 0.1)
            try:
                self._prune()
                mode, _ = evaluate_pressure({"cluster": {"expected": 1, "responding": 1},
                                             "maintenance_health": [local_pressure_snapshot()]})
                with self.lock:
                    self.mode = mode
                if mode in {"paused", "reduced"}:
                    self.stop_event.wait(2)
                    continue
                with self.lock:
                    excluded = set(self.entries) | self.pending
                    full = len(excluded) >= self.capacity
                if full:
                    self.stop_event.wait(1)
                    continue
                token = accounts.page_prewarm_candidate(excluded)
                if not token:
                    self.stop_event.wait(2)
                    continue
                with self.lock:
                    if token not in self.pending and token not in self.entries and len(self.entries) + len(self.pending) < self.capacity:
                        self.pending.add(token)
                        owned = True
                if not owned:
                    self.stop_event.wait(delay)
                    continue
                backend = OpenAIBackendAPI(access_token=token, transferable_session=True,
                                           deadline_monotonic=time.monotonic() + 10)
                backend._bootstrap(timeout_secs=10)
                # No token exchange or auth state mutation on a public-page failure.
                current = accounts.get_account(token, refresh_snapshot=False)
                if current and generation(current) == generation(backend.account):
                    self.put(backend)
                    backend = None
            except Exception as exc:
                self.defer_failure(exc)
                delay = max(delay, 5)
            finally:
                if backend is not None:
                    backend.close()
                if owned:
                    with self.lock:
                        self.pending.discard(token)
            self.stop_event.wait(delay)

    def stop(self):
        self.stop_event.set()
        deadline = time.monotonic() + 11
        for thread in self.threads:
            thread.join(timeout=max(0, deadline - time.monotonic()))
        with self.lock:
            remaining, self.entries = list(self.entries.values()), {}
        for _, backend, _ in remaining:
            backend.close()
        # In-flight builders close on completion instead of repopulating a stopped pool.


page_prewarm_pool = PagePrewarmPool()
