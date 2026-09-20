"""Synthetic public dashboard contract for API and browser regressions."""
def dashboard_payload():
    ranges = {}
    date = "2026-09-21T00:00:00+08:00"
    for key, size in [("24h", 24), ("7d", 7), ("30d", 30)]:
        labels = [str(i) for i in range(size)]
        ranges[key] = dict(
            time_range=key,
            window=dict(requested=key, start_at=date, end_at=date,
                        bucket_unit="hour" if key == "24h" else "day", bucket_count=size),
            totals=dict(total=0, success=0, final_failed=0, success_rate=None, avg_success_duration_ms=None),
            switching=dict(requests=0, count=0, recovered=0, recovery_rate=None),
            buckets=[dict(label=label, start_at=date, end_at=date, total_calls=0, success_calls=0,
                          final_failed_calls=0, switch_count=0, switch_recovered=0,
                          success_rate=None, avg_success_duration_ms=None, switch_recovery_rate=None) for label in labels],
            trend=dict(labels=labels, success_requests=[0]*size, final_failed_requests=[0]*size,
                       success_rate=[None]*size, switch_count=[0]*size, model_success_requests={}, model_avg_success_duration_ms={}))
    return dict(
        status="ok", healthy=True, version="test",
        meta=dict(schema_version=5, generated_at=date, available_ranges=list(ranges)),
        metrics=dict(status="ready", ready=True, stale=False, source="test", source_revision=None,
                     last_ingested_at=None, checkpoint_at=None, failure_reason=None, freshness_ms=0, retention_days=30),
        runtime=dict(runtime_mode="docker", instance_name="app0", distribution="test", kernel_version="test",
                     architecture="x86_64", python_version="3.11", cpu_capacity=8, service_started_at=date,
                     service_uptime_seconds=1, process_cpu_percent=0, process_memory_bytes=0, process_memory_percent=0,
                     memory_scope="container", memory_percent=0, storage_percent=0,
                     network_rx_bytes_per_sec=0, network_tx_bytes_per_sec=0),
        operations=dict(active_requests=199, scope="cluster", expected_instances=8, responding_instances=8, complete=True),
        accounts=dict(total=7000, cumulative_total=7000, active=7000, limited=0, abnormal=0, disabled=0,
                      total_quota=14000, unlimited_quota_count=0, unknown_quota_count=0, total_success=0,
                      total_fail=0, by_type={}, healthy=True),
        storage=dict(application_database={}, image_storage=dict(enabled=True, mode="local", status="not_checked",
                                                                available=None, image_count=None, image_size_bytes=None)),
        ranges=ranges)
