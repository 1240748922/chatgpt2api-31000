# Admin reliability incident — 2026-10-19

## Confirmed cause

The watchdog captured app0's serving event-loop thread twice during the
reproduced outage (05:42:40 and 05:43:09 UTC / 13:42:40 and 13:43:09 Beijing):

    api/system.py:get_image
    services/image_service.py:get_image_response
    services/image_storage_service.py:has_local / get_bytes
    services/image_storage_service.py:_load_clean_index -> _load_index
    services/storage/image_index_journal.py:overlay
    pathlib:read_text / open

The async HTTP route performed synchronous full-catalog reads, including each
pending journal JSON, on the event loop. Image fallback requests are pinned to
app0; removing app0 from the generation pool alone did not eliminate the
outage. During this reproduction even /version exceeded an 18-second client
timeout. File/catalog work now uses a separate bounded media pool; existing
local-file reads bypass the catalog after path validation, matching NGINX.
WebDAV-only reads retain catalog verification and session cleanup.

Two independently reproduced amplification bugs were also fixed:

1. Retention deferral advanced only the local deadline, leaving persisted
   next_run_epoch overdue. The wait loop immediately woke again. An old-code
   regression reproduced 14,341 attempts in 0.25 seconds; production showed
   1,979 deferred messages in three minutes. The shared deadline now advances
   with the existing wake-revision guard. After deployment, deferrals are
   approximately 30–33 seconds apart.
2. Completed image handlers performed synchronous call-log DB writes on the
   serving event loop. They now use a separate bounded logging executor.

The historical 7/17-minute handler-queue messages alone do not distinguish
event-loop starvation from worker/lock/DB pressure. The stack capture is the
direct evidence. The sampled PostgreSQL state (~39 connections, no lock wait)
does not prove database pool exhaustion. CPU/RSS alone is also insufficient.

## Safeguards and preserved dependencies

- Only app0 is removed from the ordinary generation upstream. Admin APIs,
  image-task queries and media fallback stay on app0. Imports stay on importer.
- app0 is not generation-free: tasks submitted through /api/image-tasks/...
  retain their existing execution and state ownership there. Ordinary
  /v1/images/generations and /v1/images/edits requests go to app1–app7.
- Shard count stays eight and app0 stays shard zero. Singleton registration,
  retention, backup and maintenance ownership are unchanged. Account selection
  retains its existing cross-shard fallback.
- Admission caps requests before upload body/auth/filter processing. Image
  execution separately bounds active plus queued work: 40 queued per process,
  10-second queue wait. Overload/deadline failures return 503/504 rather than
  accumulating abandoned work for minutes.
- Execution follows existing configured image stream/poll timeouts, not an
  arbitrary 40-second generation cutoff. Disconnected queued callbacks are
  skipped; running threads retain their slots until they actually finish.
  Streams close their iterator chain; premature close is not logged as success.
- Media executor: four workers plus 16 queued, two-second queue wait,
  30-second response deadline. Dashboard: two workers plus four queued,
  two-second queue wait, 15-second execution deadline. Logging is independent.
- Admin auth and internal maintenance/configuration reads are off-loop.
  /version remains on the real serving loop, not a falsely healthy sidecar.
- PostgreSQL defaults: connect 10 seconds, statement 60 seconds, lock 10 seconds;
  existing Compose checkout cap is 10 seconds. Environment overrides remain.
- A watchdog thread logs stack locations only after a five-second heartbeat
  gap (at most once per 30 seconds). A continuous stall of at least 180 seconds
  exits with code 70 for Docker unless-stopped recovery. This is neither a
  periodic restart nor an image-load-triggered restart.

## Verification

After the media fix, 30 samples at ten-second intervals with real ongoing
production image traffic (up to 20 active requests) gave:

| Route | HTTP 200 | Median | Maximum |
|---|---:|---:|---:|
| /api/dashboard | 30/30 | 2.253 s | 9.622 s |
| app0 /version | 30/30 | 0.140 s | 0.672 s |
| /api/image-tasks | 30/30 | 0.330 s | 1.092 s |
| /api/account-import-jobs | 30/30 | 0.188 s | 0.537 s |

No watchdog stall/exit occurred in that window; restart count stayed zero
after targeted recreation. Local image, gateway image, thumbnail and download
returned 200; a deliberately missing image returned 404 in 2.506 seconds.
No user image payload, account identifier or key is included in this report.
Import listing is a read-only smoke test: no dummy accounts were imported into
production. Import mutations and task state transitions have local tests.

Regression command, from src_extract:

    PYTHONPATH=.:../GPT-Register-Tool-main python -m pytest -q --tb=short \
      --ignore-glob='tests/*browser.py' \
      --ignore=tests/test_registration_preflight_report.py \
      --deselect=tests/test_replenishment_status.py::test_proxy_test_uses_registration_preflight_and_redacts_credentials

Result: 798 passed, 12 skipped, 1 deselected; one Starlette/httpx deprecation
warning. Browser/config-dependent exclusions are explicit, not counted as
passing. New tests cover queue limits, cancellation slots, streaming cleanup,
event-loop responsiveness, catalog bypass, retention scheduling, role routing
and continuous-stall recovery.

Before publishing, the exact staged snapshot was exported and tested
independently: 789 passed, 12 skipped, 1 deselected, with the same warning.
The nine registration-asset tests and preexisting frontend cache changes
remain local and are deliberately excluded from this reliability commit.

The exact old-base production overlay was separately assembled in an isolated
temporary checkout: 777 passed, 12 skipped, 1 deselected, with the same warning.
A disposable subprocess with a deliberately stale heartbeat exited with code
70 in 1.08 seconds; no live service was stalled for this test. Canary app1
remained healthy, handled generation HTTP 200 responses, and reported active
image work with zero queued requests after restoring its gateway membership.

## Production rollout and rollback

Deployment directory: /www/wwwroot/chatgpt2api-31000.
Backups/scripts: /www/wwwroot/chatgpt2api-31000/.hotfix-admin-20261019.

Production was based on 3e9e653; local checkout had advanced to fc04dfe. The
overlay preserves the old production base instead of deploying unrelated
extra-image archival changes. Initial and media-patch source hashes were
verified before replacement. Original image and source files are retained.

docker-compose.override.yml pins patched services to a local tag with
pull_policy: never. Ordinary Compose recreation preserves it. A future release
must explicitly replace/remove this override after including the fix, or the
services will remain pinned to the hotfix.

The rolling script marks one worker down in NGINX, validates and gracefully
reloads, waits for three zero-active/zero-queue samples, recreates only that
service, verifies health/new queue configuration, then restores routing.
Drain timeout aborts instead of terminating live image work. Per-step Compose
and NGINX snapshots and build logs are retained. Importer/PostgreSQL/gateway
containers are not recreated.

Single-node rollback must use that node's saved snapshot and recreate only
that node. Snapshots contain state at their rollout step; do not use an early
snapshot to restart the entire stack. Keep app0 isolated during code rollback
unless deliberately doing a separate full-routing rollback.

Final rollout completed at approximately 14:12 Beijing: app0–app7 all use
chatgpt2api-31000:admin-reliability-20261019-v3, all healthy, restart counts zero.
Importer, PostgreSQL and gateway retained their original start times. All
seven generation workers are back in the pool, app0 remains excluded, NGINX
configuration validates, and all workers report queue_limit=40.

The installed rollback-service.sh restores ONE service to the saved original
image, preserves other services' image settings, and keeps admin isolation:

    sh /www/wwwroot/chatgpt2api-31000/.hotfix-admin-20261019/rollback-service.sh app0

For an image worker replace app0 with app1 through app7; it drains first. The
helper has been syntax-checked but deliberately not executed on healthy live
services. Do not use the initial v1/v2 whole-override rollback scripts after
the full worker rollout; use this per-service helper instead.

The four final critical source hashes match the separately tested production
overlay. A real DB session reports statement_timeout=1min and lock_timeout=10s.
An all-owner persisted task read found no queued/running image tasks before
the final admin recreation. Recent actual image log samples showed 40/40
successes at 14:08:33, then 38 successes and two text-review outcomes in the
next 40-record sample at 14:09:32; these are snapshots, not a global SLA.

Final post-rollout observation: 12 additional samples across two minutes with
29–70 active image requests, every route successful in all samples:

| Route | HTTP 200 | Median | Maximum |
|---|---:|---:|---:|
| /api/dashboard | 12/12 | 2.236 s | 5.893 s |
| app0 /version | 12/12 | 0.148 s | 0.587 s |
| /api/image-tasks | 12/12 | 0.417 s | 0.781 s |
| /api/account-import-jobs | 12/12 | 0.174 s | 0.427 s |

All dashboard samples reported all eight instances responding. No app0
watchdog stall/exit or logging failure occurred in this final observation.
The retained source changes are local and the production overlay is deployed;
no GitHub push was performed as part of this incident repair.

## Operational boundaries

- NGINX retains passive max_fails=2 / fail_timeout=10s checks. Docker unhealthy
  alone does not update membership or restart containers. The watchdog covers
  continuous event-loop stalls, not every possible unhealthy state. No active
  health-controller service was introduced.
- Do not enable retries of already-sent image POSTs: duplicates may consume
  quota or generate extra images. Existing non-idempotent retry behavior stays.
- Bounded awaits do not forcibly kill running Python/C calls. Underlying
  network/DB timeouts and retained slots remain essential. A permanently stuck
  worker can exhaust a finite pool and produce bounded 503s, not an unlimited
  queue. OS/kernel uninterruptible IO is outside watchdog guarantees.
- Remote/missing-image catalog scans still cost IO, now off-loop and bounded.
  Per-image catalog indexing/compaction is a separate optimization; no
  destructive catalog migration was made during this incident.
- Five-minute validation shows recovery under observed traffic, not a promise
  for every load level. Watch 499s, dashboard latency, worker queue depth,
  heartbeat gaps and pool failures after rollout.
