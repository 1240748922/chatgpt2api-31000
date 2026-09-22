"""CPU-only snapshot/dispatch probe; synthetic accounts, no production DB/network."""
import argparse
import json
import os
import sys
import tempfile
import statistics
import time
from copy import deepcopy
from pathlib import Path
from threading import Condition, Lock
from types import SimpleNamespace

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--counts", type=int, nargs="+", default=[7000, 29429])
parser.add_argument("--output", required=True, type=Path)
parser.add_argument("--code-root", type=Path)
args = parser.parse_args()
if any(count < 1 for count in args.counts):
    parser.error("counts must be positive")
root = args.code_root or Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / "src_extract"))
directory = tempfile.TemporaryDirectory(prefix="account-snapshot-benchmark-")
os.environ["DATABASE_URL"] = f"sqlite:///{Path(directory.name).as_posix()}/isolated.db"
os.environ["CHATGPT2API_AUTH_KEY"] = "offline-benchmark"
from services.account_service import AccountService
from services.storage.base import StorageSnapshot
from services.application_database import dispose_all_database_engines


class TimedLock:
    def __init__(self):
        self.lock = Lock()
        self.held = []
        self.started = 0

    def acquire(self, *args, **kwargs):
        acquired = self.lock.acquire(*args, **kwargs)
        if acquired:
            self.started = time.perf_counter()
        return acquired

    def release(self):
        self.held.append((time.perf_counter() - self.started) * 1000)
        self.lock.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *args):
        self.release()


def probe(count):
    service = AccountService.__new__(AccountService)
    service._lock = TimedLock()
    service._image_slot_condition = Condition(service._lock)
    service._write_lock = Lock()
    service._account_snapshot_refresh_lock = Lock()
    service._account_snapshot_refresh_dispatch_lock = Lock()
    service._account_snapshot_refresh_scheduled = False
    service._image_inflight = {}
    service._token_aliases = {}
    service._index = service._image_index = service._image_shard_index = 0
    service._image_shard_count = 8
    service._accounts_revision = "initial"
    service._account_snapshot_checked_at = time.monotonic()
    rows = [dict(access_token=f"synthetic-{i}-" + "x" * 1500, management_id=f"acct_{i:032x}",
                 status="正常", type="free", quota=8, image_quota_unknown=False,
                 last_remote_checked_at="2026-09-22T12:00:00+00:00") for i in range(count)]
    service._accounts, _ = service._normalize_loaded_accounts(rows, recover_interrupted_checks=False)
    service._persisted_accounts = deepcopy(service._accounts)
    remote = deepcopy(service._accounts)
    revision = ["initial"]
    reads = [0]

    def read():
        reads[0] += 1
        return StorageSnapshot(items=list(remote.values()), revision=revision[0])

    service.storage = SimpleNamespace(load_accounts_snapshot=read,
        get_collection_revision=lambda _: revision[0], get_backend_info=lambda: {"type": "sqlite"})
    selections = []
    for _ in range(30):
        started = time.perf_counter()
        token = service.get_available_access_token()
        selections.append((time.perf_counter() - started) * 1000)
        service.release_image_slot(token)
    reloads, lock_holds = [], []
    first = next(iter(remote))
    for cycle in range(3):
        remote[first]["success"] = cycle + 1
        revision[0] = str(cycle)
        service._account_snapshot_checked_at = 0
        service._lock.held.clear()
        started = time.perf_counter()
        assert service._refresh_accounts_snapshot_if_stale(wait_for_refresh=True)
        reloads.append((time.perf_counter() - started) * 1000)
        lock_holds.append(max(service._lock.held))
        assert service._accounts[first]["success"] == cycle + 1
    return dict(accounts=count, normalized_fields=len(remote[first]),
                healthy_lookup_median_ms=statistics.median(selections),
                healthy_lookup_max_ms=max(selections),
                single_row_changes=3, full_snapshot_reads=reads[0],
                reload_cpu_ms=reloads, dispatch_lock_max_hold_ms=lock_holds)


try:
    results = [probe(count) for count in args.counts]
    args.output.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(json.dumps(results, indent=2), flush=True)
finally:
    dispose_all_database_engines()
    directory.cleanup()
