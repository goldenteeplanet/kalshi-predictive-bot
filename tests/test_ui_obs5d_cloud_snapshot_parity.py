from __future__ import annotations

from datetime import UTC, datetime

from kalshi_predictor.ui.cloud_snapshot_parity import certify_cloud_snapshot_parity


NOW = datetime(2026, 7, 19, 13, 0, tzinfo=UTC)


def base_snapshot():
    return {
        "generated_at": "2026-07-19T12:59:30Z",
        "execution_enabled": False,
        "writer": {"safe_to_start_write": True, "lock_status": "CLEAR"},
        "scheduler": {"service": "kalshi-r5-bounded.service"},
        "prov14b": {"state": "QUEUED"},
        "phase_roadmap": [{"number": n} for n in range(1, 21)],
        "reports": [{"phase": "R5-RECOVERY-9"}],
        "workstreams": [
            {"id": "pmb"}, {"id": "prov", "current_phase": "PROV-14B"},
            {"id": "nyc_weather"}, {"id": "gh_liquidity"}, {"id": "readiness"},
        ],
    }


def authoritative():
    return {
        "execution_enabled": False, "lock_status": "CLEAR", "safe_to_start_write": True,
        "bounded_service": "kalshi-r5-bounded.service", "bounded_timer_enabled": True,
        "bounded_timer_active": False, "legacy_enabled": False, "legacy_active": False,
    }


def test_exact_snapshot_passes_with_isolation_warning():
    report = certify_cloud_snapshot_parity(base_snapshot(), authoritative(), reference_time=NOW)
    assert report["parity_passed"] is True
    assert report["deployment_ready"] is True
    assert report["deployment_performed"] is False
    assert report["warnings"] == ["BOUNDED_TIMER_INTENTIONALLY_ISOLATED"]


def test_stale_legacy_snapshot_blocks_deployment():
    snapshot = base_snapshot()
    snapshot["generated_at"] = "2026-07-19T12:00:00Z"
    snapshot["scheduler"]["service"] = "kalshi-r5-watcher.service"
    snapshot.pop("phase_roadmap")
    report = certify_cloud_snapshot_parity(snapshot, authoritative(), reference_time=NOW)
    assert report["parity_passed"] is False
    assert "CAPTURE_STALE" in report["failures"]
    assert "SCHEDULER_SERVICE_STALE" in report["failures"]
    assert "TWENTY_PHASE_ROADMAP_MISSING" in report["failures"]


def test_execution_enablement_is_always_critical_failure():
    state = authoritative()
    state["execution_enabled"] = True
    report = certify_cloud_snapshot_parity(base_snapshot(), state, reference_time=NOW)
    assert report["deployment_ready"] is False
    assert "AUTHORITATIVE_EXECUTION_NOT_DISABLED" in report["failures"]
