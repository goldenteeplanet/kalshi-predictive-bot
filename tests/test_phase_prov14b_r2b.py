from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from kalshi_predictor.phase_prov14b_r2a import build_certification_bundle
from kalshi_predictor.phase_prov14b_r2b import capture_runtime_evidence

AS_OF = datetime(2026, 7, 19, 18, 0, tzinfo=UTC)


def _json(path: Path, value) -> Path:
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def _fixture(tmp_path: Path) -> dict:
    tmp_path.mkdir(parents=True, exist_ok=True)
    rollback = tmp_path / "rollback"
    rollback.mkdir()
    (rollback / "cycle.py").write_text("cycle", encoding="utf-8")
    backup = _json(tmp_path / "backup.json", {
        "path": "/mnt/backup/exact.db",
        "size_bytes": 100,
        "quick_check": "ok",
        "sha256": "a" * 64,
        "integrity_check": "ok",
        "execution_enabled": False,
        "finished_at": (AS_OF - timedelta(minutes=5)).isoformat(),
    })
    writer = tmp_path / "writer.txt"
    writer.write_text(
        "DB writer monitor: CLEAR\nCurrent writer PID: none\n"
        "Safe to start another write job: yes\n",
        encoding="utf-8",
    )
    locks = tmp_path / "locks.txt"
    locks.write_text(
        "Database lock diagnostics: CLEAR\nSafe to start another write job: yes\n"
        "Open DB holders: none visible\n",
        encoding="utf-8",
    )
    services = _json(tmp_path / "services.json", {
        "bounded_service": "inactive",
        "bounded_timer": "inactive",
        "legacy_watcher": "inactive",
        "legacy_watcher_enabled": False,
        "other_writer": "inactive",
    })
    execution = tmp_path / "execution.txt"
    execution.write_text("EXECUTION_ENABLED=false\n", encoding="utf-8")
    cycle_value = {
        "after_event_id": 10,
        "weather_features_inserted": 1,
        "tickers": {"crypto_v2": ["BTC"], "weather_v2": ["WX"]},
        "summaries": {
            "crypto_v2": {"snapshots_scanned": 1, "forecasts_inserted": 1},
            "weather_v2": {"snapshots_scanned": 1, "forecasts_inserted": 1},
        },
    }
    cycle = _json(tmp_path / "cycle.json", cycle_value)
    rows = [
        {
            "event_id": event_id,
            "model_name": model,
            "forecast_id": event_id,
            "source_observation_ref": {"table": "source", "id": event_id},
            "market_snapshot_id": event_id,
            "feature_source_table": "features",
            "feature_source_id": event_id,
            "passed": True,
            "failures": [],
        }
        for event_id, model in ((11, "crypto_v2"), (12, "weather_v2"))
    ]
    attribution = _json(tmp_path / "attribution.json", {
        "phase": "PROV-14",
        "boundary": {"after_event_id": 10},
        "summary": {
            "certification_passed": True,
            "events_failed": 0,
            "result_truncated": False,
            "model_counts": {"crypto_v2": 1, "weather_v2": 1},
        },
        "rows": rows,
        "guardrails": {"execution_enabled": False, "thresholds_changed": False},
    })
    return {
        "backup_path": backup,
        "writer_monitor_path": writer,
        "locks_path": locks,
        "services_path": services,
        "execution_path": execution,
        "cycle_path": cycle,
        "attribution_path": attribution,
        "rollback_root": rollback,
        "rollback_paths": ["cycle.py"],
        "captured_at": AS_OF - timedelta(minutes=1),
    }


def test_exact_capture_maps_into_passing_r2a_bundle(tmp_path: Path) -> None:
    captured = capture_runtime_evidence(**_fixture(tmp_path))
    assert captured["status"] == "PASSED"
    assert captured["diagnostics"] == []
    inputs = captured["r2a_inputs"]
    report = build_certification_bundle(
        **inputs,
        rollback_root=tmp_path / "rollback",
        as_of=AS_OF,
    )
    assert report["status"] == "PASSED"
    assert report["summary"]["runtime_certified"] is True


def test_capture_is_deterministic_and_hashes_every_source(tmp_path: Path) -> None:
    kwargs = _fixture(tmp_path)
    first = capture_runtime_evidence(**kwargs)
    second = capture_runtime_evidence(**kwargs)
    assert first == second
    assert len(first["sources"]) == 7
    assert all(len(row["sha256"]) == 64 for row in first["sources"])


def test_secret_bearing_environment_dump_is_rejected(tmp_path: Path) -> None:
    kwargs = _fixture(tmp_path)
    kwargs["execution_path"].write_text(
        "EXECUTION_ENABLED=false\nAPI_SECRET=do-not-import\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="only EXECUTION_ENABLED"):
        capture_runtime_evidence(**kwargs)


def test_malformed_json_and_missing_source_are_rejected(tmp_path: Path) -> None:
    kwargs = _fixture(tmp_path)
    kwargs["cycle_path"].write_text("{broken", encoding="utf-8")
    with pytest.raises(ValueError, match="malformed JSON"):
        capture_runtime_evidence(**kwargs)
    kwargs = _fixture(tmp_path / "second")
    kwargs["locks_path"].unlink()
    with pytest.raises(ValueError, match="source is missing"):
        capture_runtime_evidence(**kwargs)


def test_unclear_writer_and_locks_produce_failed_capture(tmp_path: Path) -> None:
    kwargs = _fixture(tmp_path)
    kwargs["writer_monitor_path"].write_text("DB writer monitor: BUSY\n", encoding="utf-8")
    kwargs["locks_path"].write_text("Database lock diagnostics: BUSY\n", encoding="utf-8")
    report = capture_runtime_evidence(**kwargs)
    assert report["status"] == "FAILED"
    assert report["diagnostics"] == [
        "WRITER_CLEARANCE_NOT_PROVEN",
        "LOCK_CLEARANCE_NOT_PROVEN",
    ]


def test_stale_capture_is_preserved_then_rejected_by_r2a(tmp_path: Path) -> None:
    kwargs = _fixture(tmp_path)
    kwargs["captured_at"] = AS_OF - timedelta(minutes=16)
    captured = capture_runtime_evidence(**kwargs)
    assert captured["status"] == "PASSED"
    report = build_certification_bundle(
        **captured["r2a_inputs"],
        rollback_root=tmp_path / "rollback",
        as_of=AS_OF,
    )
    assert report["gates"]["safety.evidence_fresh"] is False


def test_rollback_escape_and_unexpected_service_field_are_rejected(tmp_path: Path) -> None:
    kwargs = _fixture(tmp_path)
    kwargs["rollback_paths"] = ["../outside.py"]
    with pytest.raises(ValueError, match="escapes root"):
        capture_runtime_evidence(**kwargs)
    kwargs = _fixture(tmp_path / "second")
    services = json.loads(kwargs["services_path"].read_text())
    services["dangerous_control"] = "enabled"
    kwargs["services_path"].write_text(json.dumps(services), encoding="utf-8")
    with pytest.raises(ValueError, match="unexpected fields"):
        capture_runtime_evidence(**kwargs)
