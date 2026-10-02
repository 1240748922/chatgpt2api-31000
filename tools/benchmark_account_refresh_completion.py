"""Compare renewal batch finalization using synthetic pools, without service I/O.

Run from the repository root: python tools/benchmark_account_refresh_completion.py
This measures only the in-memory completion lookup, not renewal/dispatch latency.
"""
import ast
import json
import statistics
import threading
import time
import tracemalloc
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src_extract/services/account_service.py"


def load_methods():
    # Extract only the methods under test; importing the full service starts
    # configuration/database singletons, which this offline probe must avoid.
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "AccountService")
    methods = [n for n in cls.body if isinstance(n, ast.FunctionDef)
               and n.name in {"list_accounts", "_existing_account_ids"}]
    scope = {}
    exec(compile(ast.Module(body=methods, type_ignores=[]), str(SOURCE), "exec"), scope)
    return scope


class Pool:
    def __init__(self, size):
        self._lock = threading.Lock()
        self._image_inflight = {}
        self._accounts = {
            f"synthetic-{i}": {
                **{f"field_{j}": "synthetic" for j in range(50)},
                "access_token": f"synthetic-{i}",
                "management_id": f"id-{i}",
            } for i in range(size)
        }

    def _refresh_accounts_snapshot_if_stale(self):
        pass  # Exclude storage refresh, parsing, network and runtime contention.


def measure(fn):
    for _ in range(3):
        fn()
    elapsed = []
    for _ in range(40):
        start = time.perf_counter()
        fn()
        elapsed.append((time.perf_counter() - start) * 1000)
    # Memory is sampled separately so tracing does not distort timing.
    tracemalloc.start()
    try:
        fn()
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    return {
        "p50_ms": round(statistics.median(elapsed), 3),
        "p95_ms": round(sorted(elapsed)[37], 3),
        "peak_temporary_kib": round(peak / 1024, 1),
    }


def main():
    methods = load_methods()
    Pool.list_accounts = methods["list_accounts"]
    Pool._existing_account_ids = methods["_existing_account_ids"]
    rows = []
    for size in (5000, 8000, 10000, 20000):
        pool = Pool(size)
        # Both targets at the tail: new lookup still has to scan the whole
        # pool. Avoid claiming a constant-time result from head-only targets.
        targets = [f"id-{size - 2}", f"id-{size - 1}"]

        def before():
            existing = {str(a.get("management_id") or "").strip()
                        for a in pool.list_accounts()
                        if str(a.get("management_id") or "").strip()}
            return set(targets) & existing

        def after():
            return pool._existing_account_ids(targets)

        assert before() == after() == set(targets)
        rows.append({
            "accounts": size, "fields_per_account": 52, "batch_size": len(targets),
            "before": measure(before), "after": measure(after),
        })
    print(json.dumps({
        "scope": "synthetic in-memory microbenchmark, not an end-to-end load test",
        "rows": rows,
    }, indent=2))


if __name__ == "__main__":
    main()
