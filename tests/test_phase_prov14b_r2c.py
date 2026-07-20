from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import kalshi_predictor.phase_prov14b_r2c as r2c
from kalshi_predictor.phase_prov14b_r2c import (
    run_capture_certification_pipeline,
    write_pipeline_report,
)

AS_OF = datetime(2026, 7, 19, 18, 0, tzinfo=UTC)


def _write(path: Path, value) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = value if isinstance(value, str) else json.dumps(value)
    path.write_text(text, encoding="utf-8")
    return path


def _fixture(tmp_path: Path) -> tuple[dict, Path]:
    rollback = tmp_path / "rollback"
    rollback.mkdir(parents=True)
    _write(rollback / "cycle.py", "cycle")
    paths = {
        "backup_path": _write(tmp_path / "backup.json", {
            "path": "/mnt/backup/exact.db",
            "size_bytes": 100,
            "quick_check": "ok",
            "sha256": "a" * 64,
            "integrity_check": "ok",
            "execution_enabled": False,
            "finished_at": (AS_OF - timedelta(minutes=5)).isoformat(),
        }),
        "writer_monitor_path": _write(
            tmp_path / "writer.txt",
            "DB writer monitor: CLEAR\nCurrent writer PID: none\n"
            "Safe to start another write job: yes\n",
        ),
        "locks_path": _write(
            tmp_path / "locks.txt",
            "Database lock diagnostics: CLEAR\n"
            "Safe to start another write job: yes\nOpen DB holders: none visible\n",
        ),
        "services_path": _write(tmp_path / "services.json", {
            "bounded_service": "inactive",
            "bounded_timer": "inactive",
            "legacy_watcher": "inactive",
            "legacy_watcher_enabled": False,
            "other_writer": "inactive",
        }),
        "execution_path": _write(tmp_path / "execution.txt", "EXECUTION_ENABLED=false\n"),
        "cycle_path": _write(tmp_path / "cycle.json", {
            "after_event_id": 10,
            "weather_features_inserted": 1,
            "tickers": {"crypto_v2": ["BTC"], "weather_v2": ["WX"]},
            "summaries": {
                "crypto_v2": {"snapshots_scanned": 1, "forecasts_inserted": 1},
                "weather_v2": {"snapshots_scanned": 1, "forecasts_inserted": 1},
            },
        }),
        "attribution_path": _write(tmp_path / "attribution.json", _attribution()),
        "rollback_root": rollback,
        "rollback_paths": ["cycle.py"],
        "captured_at": AS_OF - timedelta(minutes=1),
    }
    return paths, rollback


def _attribution() -> dict:
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
    return {
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
    }


def _run(tmp_path: Path, **overrides):
    capture, rollback = _fixture(tmp_path)
    capture.update(overrides)
    return run_capture_certification_pipeline(
        capture_kwargs=capture,
        rollback_root=rollback,
        as_of=AS_OF,
        synthetic_preview=True,
    )


def test_one_command_pipeline_passes_and_has_zero_ci_exit(tmp_path: Path) -> None:
    report = _run(tmp_path)
    assert report["status"] == "PASSED"
    assert all(report["gates"].values())
    assert report["summary"]["ci_exit_code"] == 0
    assert report["summary"]["runtime_certified"] is False


def test_pipeline_is_deterministic(tmp_path: Path) -> None:
    capture, rollback = _fixture(tmp_path)
    kwargs = dict(
        capture_kwargs=capture,
        rollback_root=rollback,
        as_of=AS_OF,
        synthetic_preview=True,
    )
    first = run_capture_certification_pipeline(**kwargs)
    second = run_capture_certification_pipeline(**kwargs)
    assert first == second


def test_source_drift_between_stages_skips_certification(tmp_path: Path, monkeypatch) -> None:
    capture, rollback = _fixture(tmp_path)
    original = r2c.capture_runtime_evidence

    def drifting_capture(**kwargs):
        result = original(**kwargs)
        kwargs["cycle_path"].write_text("{}", encoding="utf-8")
        return result

    monkeypatch.setattr(r2c, "capture_runtime_evidence", drifting_capture)
    report = run_capture_certification_pipeline(
        capture_kwargs=capture,
        rollback_root=rollback,
        as_of=AS_OF,
    )
    assert report["gates"]["sources_unchanged_before_certification"] is False
    assert report["gates"]["certification_executed"] is False
    assert report["summary"]["ci_exit_code"] == 2


def test_rollback_drift_is_caught_by_certification(tmp_path: Path, monkeypatch) -> None:
    capture, rollback = _fixture(tmp_path)
    original = r2c.capture_runtime_evidence

    def drifting_capture(**kwargs):
        result = original(**kwargs)
        (rollback / "cycle.py").write_text("changed", encoding="utf-8")
        return result

    monkeypatch.setattr(r2c, "capture_runtime_evidence", drifting_capture)
    report = run_capture_certification_pipeline(
        capture_kwargs=capture,
        rollback_root=rollback,
        as_of=AS_OF,
    )
    assert report["gates"]["certification_executed"] is True
    assert report["gates"]["certification_passed"] is False
    assert report["certification"]["gates"][
        "rollback.files_present_and_hashes_match"
    ] is False


def test_failed_capture_skips_certification(tmp_path: Path) -> None:
    capture, rollback = _fixture(tmp_path)
    capture["writer_monitor_path"].write_text("DB writer monitor: BUSY\n", encoding="utf-8")
    report = run_capture_certification_pipeline(
        capture_kwargs=capture,
        rollback_root=rollback,
        as_of=AS_OF,
    )
    assert report["gates"]["capture_passed"] is False
    assert report["certification"] is None


def test_attribution_failure_returns_ci_failure(tmp_path: Path) -> None:
    capture, rollback = _fixture(tmp_path)
    value = _attribution()
    value["rows"][1]["source_observation_ref"] = None
    _write(capture["attribution_path"], value)
    report = run_capture_certification_pipeline(
        capture_kwargs=capture,
        rollback_root=rollback,
        as_of=AS_OF,
    )
    assert report["gates"]["certification_passed"] is False
    assert report["summary"]["ci_exit_code"] == 2


def test_combined_report_is_atomically_published(tmp_path: Path) -> None:
    report = _run(tmp_path / "inputs")
    output = tmp_path / "out" / "report.json"
    assert write_pipeline_report(report, output) == output
    assert json.loads(output.read_text())["report_sha256"] == report["report_sha256"]
    assert not output.with_suffix(".json.tmp").exists()
