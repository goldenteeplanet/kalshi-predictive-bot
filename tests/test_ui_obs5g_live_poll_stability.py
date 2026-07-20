from copy import deepcopy

from kalshi_predictor.ui.live_poll_stability import certify_live_poll_stability


def fixture(at: str):
    return {
        "generated_at": at, "execution_enabled": False,
        "collector": {"read_only": True, "database_writes": 0},
        "scheduler": {"service": "kalshi-r5-bounded.service", "timer": "kalshi-r5-bounded.timer", "legacy_watcher_enabled": False, "legacy_watcher_active": False},
        "phase_roadmap": [{}] * 20,
        "workstreams": [{"id": key} for key in ("pmb", "prov", "nyc_weather", "gh_liquidity", "readiness")],
        "r5_recovery9_certification": {"status": "PASSED", "rollback_verified": True},
        "prov14b": {"state": "WAITING"},
    }


def test_three_exact_polls_pass():
    report = certify_live_poll_stability([fixture("2026-07-19T13:00:00Z"), fixture("2026-07-19T13:00:30Z"), fixture("2026-07-19T13:01:00Z")])
    assert report["status"] == "PASSED"


def test_stale_scheduler_mapping_fails_visible():
    snapshots = [fixture("2026-07-19T13:00:00Z"), fixture("2026-07-19T13:00:30Z"), fixture("2026-07-19T13:01:00Z")]
    snapshots[1] = deepcopy(snapshots[1])
    snapshots[1]["scheduler"]["service"] = "kalshi-r5-watcher.service"
    report = certify_live_poll_stability(snapshots)
    assert report["status"] == "FAILED"
    assert "POLL_2_BOUNDED_SERVICE_FAILED" in report["failures"]
