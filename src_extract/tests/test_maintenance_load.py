from __future__ import annotations

from services import maintenance_load


def test_cluster_image_load_allows_only_configured_low_load(monkeypatch):
    monkeypatch.setattr(maintenance_load, "configured_thresholds", lambda: (2, 0, 30))
    monkeypatch.setattr(
        maintenance_load,
        "_cluster_monitor_snapshot",
        lambda: {"threadpool": {"image": {"limit": 100, "active": 2, "waiting": 0}}},
    )

    allowed, load = maintenance_load.maintenance_is_allowed()

    assert allowed is True
    assert load["low_load"] is True
    assert load["image_active"] == 2


def test_cluster_image_load_defers_when_images_are_waiting(monkeypatch):
    monkeypatch.setattr(maintenance_load, "configured_thresholds", lambda: (2, 0, 30))
    monkeypatch.setattr(
        maintenance_load,
        "_cluster_monitor_snapshot",
        lambda: {"threadpool": {"image": {"limit": 100, "active": 1, "waiting": 1}}},
    )

    allowed, load = maintenance_load.maintenance_is_allowed()

    assert allowed is False
    assert load["low_load"] is False
    assert load["image_waiting"] == 1


def test_emergency_replenishment_bypasses_only_the_load_gate(monkeypatch):
    monkeypatch.setattr(
        maintenance_load,
        "cluster_image_load",
        lambda: {"ok": True, "low_load": False, "image_active": 50},
    )

    allowed, load = maintenance_load.maintenance_is_allowed(emergency=True)

    assert allowed is True
    assert load["bypassed"] is True
    assert load["bypass_reason"] == "account_pool_below_minimum"
