from __future__ import annotations

import hashlib
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path

from kalshi_predictor.phase_prov14b_r2a import build_certification_bundle

AS_OF = datetime(2026, 7, 19, 18, 0, tzinfo=UTC)


def _evidence(tmp_path: Path):
    rollback_root = tmp_path / "rollback"
    rollback_root.mkdir()
    files = []
    for name, content in (("cycle.py", b"cycle"), ("certify.py", b"certify")):
        path = rollback_root / name
        path.write_bytes(content)
        files.append({"path": name, "sha256": hashlib.sha256(content).hexdigest()})
    backup = {
        "path": "/mnt/backup/prov14b-r2.db",
        "size_bytes": 23_107_870_720,
        "quick_check": "ok",
        "sha256": "a" * 64,
        "integrity_check": "ok",
        "execution_enabled": False,
        "finished_at": (AS_OF - timedelta(minutes=10)).isoformat(),
    }
    rollback = {"files": files}
    safety = {
        "captured_at": (AS_OF - timedelta(minutes=1)).isoformat(),
        "safe_to_start_write": True,
        "locks_clear": True,
        "execution_enabled": False,
        "services": {
            "bounded_service": "inactive",
            "bounded_timer": "inactive",
            "legacy_watcher": "inactive",
            "legacy_watcher_enabled": False,
            "other_writer": "inactive",
        },
    }
    cycle = {
        "after_event_id": 100,
        "weather_features_inserted": 1,
        "tickers": {"crypto_v2": ["BTC-A"], "weather_v2": ["WX-A"]},
        "summaries": {
            "crypto_v2": {"snapshots_scanned": 1, "forecasts_inserted": 1, "skipped": 0},
            "weather_v2": {"snapshots_scanned": 1, "forecasts_inserted": 1, "skipped": 0},
        },
    }
    rows = []
    for event_id, model in ((101, "crypto_v2"), (102, "weather_v2")):
        rows.append({
            "event_id": event_id,
            "model_name": model,
            "forecast_id": event_id + 100,
            "source_observation_ref": {"table": "observations", "id": event_id + 200},
            "market_snapshot_id": event_id + 300,
            "feature_source_table": "features",
            "feature_source_id": event_id + 400,
            "passed": True,
            "failures": [],
        })
    attribution = {
        "phase": "PROV-14",
        "boundary": {"after_event_id": 100},
        "summary": {
            "certification_passed": True,
            "events_failed": 0,
            "result_truncated": False,
            "model_counts": {"crypto_v2": 1, "weather_v2": 1},
        },
        "rows": rows,
        "guardrails": {"execution_enabled": False, "thresholds_changed": False},
    }
    return backup, rollback, safety, cycle, attribution, rollback_root


def _build(tmp_path: Path, mutate=None):
    values = list(_evidence(tmp_path))
    if mutate:
        mutate(values)
    return build_certification_bundle(
        backup=values[0],
        rollback=values[1],
        safety=values[2],
        cycle=values[3],
        attribution=values[4],
        rollback_root=values[5],
        as_of=AS_OF,
        synthetic_preview=True,
    )


def test_valid_bundle_passes_but_never_authorizes_activation(tmp_path: Path) -> None:
    report = _build(tmp_path)
    assert report["status"] == "PASSED"
    assert all(report["gates"].values())
    assert report["summary"]["runtime_certified"] is False
    assert report["summary"]["deployment_or_execution_authorized"] is False


def test_bundle_is_deterministic(tmp_path: Path) -> None:
    values = _evidence(tmp_path)
    kwargs = dict(
        backup=values[0], rollback=values[1], safety=values[2], cycle=values[3],
        attribution=values[4], rollback_root=values[5], as_of=AS_OF,
        synthetic_preview=True,
    )
    assert build_certification_bundle(**kwargs) == build_certification_bundle(**deepcopy(kwargs))


def test_missing_or_malformed_backup_fails_closed(tmp_path: Path) -> None:
    def mutate(values):
        values[0].pop("sha256")
        values[0]["quick_check"] = ["ok"]

    report = _build(tmp_path, mutate)
    assert report["status"] == "FAILED"
    assert report["gates"]["backup.metadata_complete"] is False
    assert report["gates"]["backup.quick_check_ok"] is False


def test_stale_safety_evidence_fails_closed(tmp_path: Path) -> None:
    report = _build(
        tmp_path,
        lambda values: values[2].update(
            {"captured_at": (AS_OF - timedelta(minutes=16)).isoformat()}
        ),
    )
    assert report["gates"]["safety.evidence_fresh"] is False


def test_writer_lock_execution_and_service_failures_are_visible(tmp_path: Path) -> None:
    def mutate(values):
        values[2].update({
            "safe_to_start_write": False,
            "locks_clear": False,
            "execution_enabled": True,
        })
        values[2]["services"]["bounded_timer"] = "active"
        values[2]["services"]["legacy_watcher_enabled"] = True

    report = _build(tmp_path, mutate)
    for gate in (
        "safety.writer_clear",
        "safety.locks_clear",
        "safety.execution_disabled",
        "safety.writer_services_inactive",
        "safety.legacy_watcher_disabled",
    ):
        assert report["gates"][gate] is False


def test_rollback_hash_mismatch_and_path_escape_fail_closed(tmp_path: Path) -> None:
    def mutate(values):
        values[1]["files"][0]["sha256"] = "0" * 64
        values[1]["files"][1]["path"] = "../outside.py"

    report = _build(tmp_path, mutate)
    assert report["gates"]["rollback.paths_safe_and_unique"] is False
    assert report["gates"]["rollback.files_present_and_hashes_match"] is False


def test_zero_weather_cycle_and_missing_exact_reference_fail_closed(tmp_path: Path) -> None:
    def mutate(values):
        values[3]["summaries"]["weather_v2"]["forecasts_inserted"] = 0
        values[4]["rows"][1]["market_snapshot_id"] = None

    report = _build(tmp_path, mutate)
    assert report["gates"]["cycle.both_models_inserted_forecasts"] is False
    assert report["gates"]["attribution.all_exact_references_present"] is False


def test_boundary_mismatch_and_failed_attribution_fail_closed(tmp_path: Path) -> None:
    def mutate(values):
        values[4]["boundary"]["after_event_id"] = 99
        values[4]["summary"]["certification_passed"] = False
        values[4]["summary"]["events_failed"] = 1

    report = _build(tmp_path, mutate)
    assert report["gates"]["attribution.boundary_matches_cycle"] is False
    assert report["gates"]["attribution.certification_passed"] is False
    assert report["gates"]["attribution.no_failed_or_truncated_events"] is False
