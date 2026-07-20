from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from kalshi_predictor.benchmarking.pmb35a_diagnostics import build_pmb35a_diagnostics
from kalshi_predictor.prov14c_import import import_runtime_attribution_exports
from kalshi_predictor.readiness2b import build_remediation_roadmap
from kalshi_predictor.ui.live_roadmap_status import build_live_roadmap_status


def test_ui_obs5b_fails_visible_on_stale_running_heartbeat() -> None:
    phases = [{"number": i, "phase": f"P{i}", "status": "PASSED", "evidence": "x"} for i in range(1, 21)]
    payload = {
        "phase_roadmap": phases, "execution_enabled": False,
        "writer": {"safe_to_start_write": True, "lock_status": "CLEAR"},
        "scheduler": {"state": "RUNNING", "heartbeat": {"at": "2026-07-19T00:00:00Z", "interval_seconds": 15}, "legacy_watcher_enabled": False, "legacy_watcher_active": False},
        "r5_recovery9_certification": {"status": "PASSED", "rollback_verified": True},
    }
    report = build_live_roadmap_status(payload, reference_time=datetime(2026, 7, 19, 0, 1, tzinfo=UTC))
    assert report["phase_count"] == 20
    assert "BOUNDED_CYCLE_HEARTBEAT_STALE" in report["diagnostics"]


def test_prov14ca_rejects_missing_exact_reference(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"boundary": {"after_event_id": 1}, "rows": [{"event_id": 2}], "guardrails": {"execution_enabled": False}}))
    report = import_runtime_attribution_exports([path])
    assert report["summary"]["rejected"] == 1
    assert report["summary"]["ready_for_prov14c"] is False


def test_pmb35a_never_defaults_missing_weather_reference() -> None:
    report = build_pmb35a_diagnostics(Path("reports/phase_pmb34a/pmb34a_exact_shadow_field_source_mapping_preview.json"))
    assert report["fabricated_or_default_values_allowed"] is False
    assert report["summary"]["blocked_rows"] == 1
    assert report["summary"]["pmb35_deployment_unblocked"] is False


def test_readiness2b_preserves_disabled_execution() -> None:
    report = build_remediation_roadmap(
        Path("reports/phase_objective_status/objective_20_phase_status_20260719.json"),
        Path("reports/phase_readiness2/readiness2_failed_gate_attribution_fresh_20260719.json"),
        Path("reports/phase_gh1x/gh1x_liquidity_edge_risk_census.json"),
    )
    assert report["guardrails"]["execution_enabled"] is False
    assert all(step["activation_allowed"] is False for step in report["remediation_order"])
