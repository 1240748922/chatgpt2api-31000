from types import SimpleNamespace
from threading import Event

import pytest

from services.account_maintenance_policy import AccountMaintenancePolicy, evaluate_pressure
from services.maintenance_pressure import PressureSamples, measure_account_upstream
from services import maintenance_pressure, account_maintenance


def report(**updates):
    value = {"schema":1, "sampled_at":1000, "cpu_percent":20, "memory_percent":40,
             "database_pool_utilization":.25,
             "samples":{"database_ms":{"count":10,"p95":10,"mean":5,"age_seconds":0},
                        "upstream_error":{"count":10,"p95":0,"mean":0,"age_seconds":0}}}
    value.update(updates)
    return value


def load(active=300, reports=None):
    reports = reports if reports is not None else [report() for _ in range(8)]
    return {"cluster":{"expected":8,"responding":8}, "threadpool":{"image":{"active":active,"waiting":0}},
            "maintenance_health":reports}


def test_hundreds_of_healthy_generations_do_not_stop_sync():
    result = AccountMaintenancePolicy().decide(load(), now=1000, monotonic=100)
    assert result["allowed"] and result["mode"] == "normal"
    assert result["active_images"] == 300


@pytest.mark.parametrize("signal,value,reason", [("cpu_percent",98,"cpu"), ("memory_percent",96,"memory"), ("database_pool_utilization",1,"database_pool")])
def test_one_overloaded_replica_stops_new_batches_even_at_zero_requests(signal, value, reason):
    reports = [report() for _ in range(8)]
    reports[-1][signal] = value
    mode, codes = evaluate_pressure(load(0, reports), now=1000)
    assert mode == "paused" and reason in codes


@pytest.mark.parametrize("metric,value", [("database_ms",1500), ("writer_wait_ms",1500), ("upstream_ms",25000), ("upstream_error",.7)])
def test_latency_and_error_pressure_are_not_request_count_gates(metric, value):
    reports = [report() for _ in range(8)]
    reports[2]["samples"][metric] = {"count":10,"p95":value,"mean":value,"age_seconds":0}
    mode, reasons = evaluate_pressure(load(1, reports), now=1000)
    assert mode == "paused" and metric in reasons


def test_queue_only_pressure_reduces_but_does_not_starve_account_recovery():
    reports = [report() for _ in range(8)]
    reports[1]["samples"]["queue_ms"] = {"count":10,"p95":15000,"age_seconds":0}
    result = AccountMaintenancePolicy().decide(load(300, reports), now=1000, monotonic=100)
    assert result["mode"] == "reduced" and result["batch_size"] == 1


def test_cold_start_uses_one_small_probe_not_a_permanent_stop():
    reports = [report(samples={}, cpu_percent=None) for _ in range(8)]
    result = AccountMaintenancePolicy().decide(load(300, reports), now=1000, monotonic=100)
    assert result["mode"] == "probe" and result["batch_size"] == 1


def test_idle_replicas_do_not_keep_whole_cluster_at_probe_forever():
    reports = [report()] + [report(samples={}) for _ in range(7)]
    result = AccountMaintenancePolicy().decide(load(0, reports), now=1000, monotonic=100)
    assert result["mode"] == "normal"
    reports[-1]["memory_percent"] = 99
    assert evaluate_pressure(load(0, reports), now=1000)[0] == "paused"


@pytest.mark.parametrize("kind", ["missing", "old", "stale", "nonfinite"])
def test_incomplete_or_untrustworthy_telemetry_does_not_mean_idle(kind):
    value = load()
    if kind == "missing": value["cluster"]["responding"] = 7
    if kind == "old": value["maintenance_health"][1] = None
    if kind == "stale": value["maintenance_health"][1]["sampled_at"] = 900
    if kind == "nonfinite": value["maintenance_health"][1]["sampled_at"] = float("inf")
    assert evaluate_pressure(value, now=1000)[0] == "paused"


def test_pause_has_recovery_hold_and_then_resumes():
    policy = AccountMaintenancePolicy()
    bad = load()
    bad["maintenance_health"][0]["memory_percent"] = 99
    assert not policy.decide(bad, now=1000, monotonic=100)["allowed"]
    assert not policy.decide(load(), now=1001, monotonic=101)["allowed"]
    assert policy.decide(load(), now=1010, monotonic=111)["allowed"]


def test_samples_are_bounded_expire_and_reject_invalid_values():
    samples = PressureSamples(window_seconds=60, capacity=4)
    for i in range(10): samples.observe("database_ms",i,now=100)
    for value in [float("inf"), float("nan"), 10**1000, -1, "bad"]: samples.observe("database_ms",value,now=100)
    samples.observe("access_token", "not-allowed",now=100)
    view = samples.snapshot(now=100)
    assert view["database_ms"]["count"] == 4 and view["database_ms"]["p95"] == 9
    assert samples.snapshot(now=161) == {}


@pytest.mark.parametrize("status,expected", [(401,0),(400,0),(429,1),(503,1),(None,1)])
def test_credential_errors_do_not_masquerade_as_system_overload(monkeypatch,status,expected):
    samples = PressureSamples()
    monkeypatch.setattr(maintenance_pressure, "pressure_samples", samples)
    class Failure(Exception):
        status_code = status
    @measure_account_upstream
    def operation(): raise Failure()
    with pytest.raises(Failure): operation()
    assert samples.snapshot()["upstream_error"]["mean"] == expected


def test_reduced_batch_does_not_loop_through_entire_queue(monkeypatch):
    monkeypatch.setattr(account_maintenance, "account_maintenance_decision", lambda: {
        "allowed":True,"mode":"probe","batch_size":1,"reason_codes":["warming"]})
    calls = []
    service = SimpleNamespace(sync_accounts_and_quota=lambda tokens:calls.append(tokens))
    assert account_maintenance.sync_idle_batch(service,list("abcdef"),Event()) == 1
    assert calls == [["a"]]


def test_stale_and_malformed_samples_do_not_count_as_healthy_evidence():
    for sample in [{"count":10,"p95":10,"age_seconds":61}, {"count":"bad","age_seconds":0}, None]:
        reports = [report(samples={"database_ms": sample}) for _ in range(8)]
        assert evaluate_pressure(load(reports=reports), now=1000)[0] == "probe"
    assert AccountMaintenancePolicy().decide(None, now=1000, monotonic=10)["mode"] == "paused"


def test_legacy_rollback_and_invalid_setting(monkeypatch):
    from services import account_maintenance_policy as module, maintenance_load
    monkeypatch.setenv("CHATGPT2API_ACCOUNT_MAINTENANCE_POLICY", "idle")
    monkeypatch.setattr(maintenance_load, "maintenance_is_allowed", lambda: (False, {"image_active":300}))
    monkeypatch.setattr(module, "account_maintenance_policy", AccountMaintenancePolicy())
    result = module.account_maintenance_decision()
    assert result["policy"] == "idle" and not result["allowed"]
    monkeypatch.setenv("CHATGPT2API_ACCOUNT_MAINTENANCE_POLICY", "typo")
    result = module.account_maintenance_decision()
    assert result["policy"] == "invalid" and not result["allowed"]


def test_actual_watcher_runs_recovery_and_quota_with_healthy_high_concurrency(monkeypatch):
    from api import support
    from services import maintenance_load
    class OneCycle(Event):
        def wait(self, timeout=None):
            self.set()
    calls = []
    service = SimpleNamespace(
        list_limited_tokens=lambda:["limited"], list_expiring_access_tokens=lambda:["expiring"],
        list_unknown_quota_tokens=lambda:["unknown"],
        resume_pending_auth_verifications=lambda **kw:calls.append(("verify", kw["limit"])),
        renew_expiring_access_tokens=lambda tokens:calls.append(("renew", tokens)),
        sync_accounts_and_quota=lambda tokens:calls.append(("sync", tokens)),
    )
    policy = AccountMaintenancePolicy()
    decide = lambda: policy.decide(load(300), now=1000, monotonic=100)
    monkeypatch.setattr(support, "account_service", service)
    monkeypatch.setattr(support, "account_maintenance_decision", decide)
    monkeypatch.setattr(account_maintenance, "account_maintenance_decision", decide)
    monkeypatch.setattr(maintenance_load, "maintenance_is_allowed", lambda: pytest.fail("legacy gate called"))
    stopped = OneCycle()
    thread = support.start_account_lifecycle_watcher(stopped)
    thread.join(3)
    assert not thread.is_alive()
    assert calls == [("verify", 2), ("renew", ["expiring"]), ("sync", ["unknown"]), ("sync", ["limited"])]


def test_telemetry_failure_preserves_legacy_load_counters(monkeypatch):
    from services import cluster_monitor_service as cluster, log_service
    monkeypatch.setattr(log_service, "image_threadpool_snapshot", lambda: {"active":300,"waiting":0})
    def broken():
        raise RuntimeError("synthetic diagnostic failure")
    monkeypatch.setattr(maintenance_pressure, "local_pressure_snapshot", broken)
    result = cluster._local_image_load()
    assert result["threadpool"]["image"]["active"] == 300
    assert result["maintenance_health"] == {"schema":0}


def test_local_health_uses_existing_counters_without_acquiring_database(monkeypatch):
    from services import runtime_environment_service, application_database
    samples = PressureSamples()
    samples.observe("database_ms", 15)
    monkeypatch.setattr(maintenance_pressure, "pressure_samples", samples)
    monkeypatch.setattr(runtime_environment_service, "snapshot", lambda: {"process_cpu_percent":25, "memory_percent":40})
    monkeypatch.setattr(application_database, "database_pool_utilization", lambda: .5)
    result = maintenance_pressure.local_pressure_snapshot()
    assert result["cpu_percent"] == 25 and result["database_pool_utilization"] == .5
    assert result["samples"]["database_ms"]["p95"] == 15


def test_database_hooks_measure_without_recording_query_parameters(tmp_path, monkeypatch):
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError
    from services.application_database import create_database_engine
    samples = PressureSamples()
    monkeypatch.setattr(maintenance_pressure, "pressure_samples", samples)
    engine = create_database_engine(f"sqlite:///{(tmp_path / 'pressure.sqlite').as_posix()}", shared=False)
    try:
        with engine.connect() as connection:
            assert connection.execute(text("SELECT :value"), {"value":"synthetic-private-value"}).scalar_one() == "synthetic-private-value"
            with pytest.raises(DBAPIError):
                connection.execute(text("SELECT * FROM synthetic_nonexistent_table"))
        data = samples.snapshot()
        assert data["database_ms"]["count"] >= 1
        assert data["database_error"]["latest"] == 1
        assert "synthetic" not in str(data)
    finally:
        engine.dispose()


def test_database_pool_utilization_reads_only_existing_pool_counters(monkeypatch):
    from services import application_database
    engine = SimpleNamespace(_maintenance_pool_capacity=8, pool=SimpleNamespace(checkedout=lambda:6))
    monkeypatch.setattr(application_database, "_engines", {"synthetic":engine})
    assert application_database.database_pool_utilization() == .75


def test_cluster_health_cache_is_coalesced_and_cannot_be_mutated_by_viewer(monkeypatch):
    from services import cluster_monitor_service as cluster
    monkeypatch.setattr(cluster, "_load_cache", None)
    monkeypatch.setattr(cluster, "account_shard_settings", lambda:(1,0))
    clock = [10]
    monkeypatch.setattr(cluster, "time", SimpleNamespace(monotonic=lambda:clock[0]))
    calls = []
    def collect():
        calls.append(1)
        return {"maintenance_health":[{"schema":1}]}
    monkeypatch.setattr(cluster, "_collect_image_load_snapshot", collect)
    result = cluster.cluster_image_load_snapshot()
    result["maintenance_health"][0]["schema"] = 999
    assert cluster.cluster_image_load_snapshot()["maintenance_health"][0]["schema"] == 1
    assert len(calls) == 1
    clock[0] += 3
    cluster.cluster_image_load_snapshot()
    assert len(calls) == 2
