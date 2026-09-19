"""Offline synthetic import benchmark. Never reads/writes the deployed account DB."""
import argparse
import cProfile
import json
import os
from pathlib import Path
import sys
import tempfile
import time

parser = argparse.ArgumentParser()
parser.add_argument("--count", type=int, default=7000)
parser.add_argument("--batch-size", type=int, default=250)
parser.add_argument("--output", required=True)
parser.add_argument("--profile")
parser.add_argument("--existing", type=int, default=0)
parser.add_argument("--code-root", type=Path)
args = parser.parse_args()
root = args.code_root or Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / "src_extract"))
directory = tempfile.TemporaryDirectory(prefix="account-ingest-benchmark-")
os.environ["DATABASE_URL"] = f"sqlite:///{Path(directory.name).as_posix()}/benchmark.db"
os.environ["CHATGPT2API_AUTH_KEY"] = "offline-benchmark"
from services.account_ingest_service import AccountIngestService
from services.account_service import AccountService
import services.account_service as account_module
from services.storage.database_storage import DatabaseStorageBackend
from services.application_database import dispose_all_database_engines

AccountService._load_cumulative_total = lambda self: 0
AccountService._save_cumulative_total = lambda self: None
account_module.log_service.add = lambda *a, **kw: None
storage = DatabaseStorageBackend(os.environ["DATABASE_URL"])
accounts = AccountService(storage)
if args.existing:
    accounts.add_account_items([{"access_token": f"existing-{i}-" + "x" * 1500} for i in range(args.existing)], return_items=False)
service = AccountIngestService(os.environ["DATABASE_URL"], accounts, batch_size=args.batch_size)
inputs = [{"access_token": f"synthetic-{i}-" + "x" * 1500} for i in range(args.count)]
profile = cProfile.Profile() if args.profile else None
if profile:
    profile.enable()
start = time.perf_counter()
job = service.submit(inputs)
submitted = time.perf_counter()
assert service.claim("save", "benchmark") == job["id"]
while not job["done"]:
    job = service.save_batch(job["id"], "benchmark")
end = time.perf_counter()
if profile:
    profile.disable()
    profile.dump_stats(args.profile)
assert len(storage.load_accounts()) == args.count+args.existing and job["added"] == args.count
report = {"count": args.count, "existing_accounts": args.existing, "batch_size": args.batch_size, "backend": "SQLite",
          "submit_seconds": round(submitted-start, 4), "save_seconds": round(end-submitted, 4),
          "total_seconds": round(end-start, 4), "accounts_per_second": round(args.count/(end-start), 1),
          "network": "none; synthetic credentials; quota sync disabled"}
Path(args.output).write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(report))
accounts._image_result_persist_executor.shutdown()
accounts._image_failure_schedule_executor.shutdown()
dispose_all_database_engines()
directory.cleanup()
