"""Generate a deterministic synthetic PROV-14B-R2B capture preview."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from kalshi_predictor.phase_prov14b_r2b import capture_runtime_evidence, write_capture

AS_OF = datetime(2026, 7, 19, 18, 0, tzinfo=UTC)
ROOT = Path("reports/phase_prov14b_r2b")
FIXTURES = ROOT / "fixtures"
FIXTURES.mkdir(parents=True, exist_ok=True)


def _json(name: str, value: dict) -> Path:
    path = FIXTURES / name
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


backup = _json("backup.json", {
    "path": "/mnt/kalshi-backup-02/prov14b_r2/PREVIEW.db",
    "size_bytes": 23_107_870_720,
    "quick_check": "ok",
    "sha256": "a" * 64,
    "integrity_check": "ok",
    "execution_enabled": False,
    "finished_at": (AS_OF - timedelta(minutes=5)).isoformat(),
})
services = _json("services.json", {
    "bounded_service": "inactive",
    "bounded_timer": "inactive",
    "legacy_watcher": "inactive",
    "legacy_watcher_enabled": False,
    "other_writer": "inactive",
})
cycle = _json("cycle.json", {
    "after_event_id": 100,
    "weather_features_inserted": 1,
    "tickers": {"crypto_v2": ["PREVIEW-BTC"], "weather_v2": ["PREVIEW-WX"]},
    "summaries": {
        "crypto_v2": {"snapshots_scanned": 1, "forecasts_inserted": 1},
        "weather_v2": {"snapshots_scanned": 1, "forecasts_inserted": 1},
    },
})
rows = [
    {
        "event_id": event_id,
        "model_name": model,
        "forecast_id": event_id,
        "source_observation_ref": {"table": "preview_source", "id": event_id},
        "market_snapshot_id": event_id,
        "feature_source_table": "preview_features",
        "feature_source_id": event_id,
        "passed": True,
        "failures": [],
    }
    for event_id, model in ((101, "crypto_v2"), (102, "weather_v2"))
]
attribution = _json("attribution.json", {
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
})
writer = FIXTURES / "writer_monitor.txt"
writer.write_text(
    "DB writer monitor: CLEAR\nCurrent writer PID: none\n"
    "Safe to start another write job: yes\n",
    encoding="utf-8",
)
locks = FIXTURES / "db_locks.txt"
locks.write_text(
    "Database lock diagnostics: CLEAR\nSafe to start another write job: yes\n"
    "Open DB holders: none visible\n",
    encoding="utf-8",
)
execution = FIXTURES / "execution.txt"
execution.write_text("EXECUTION_ENABLED=false\n", encoding="utf-8")

report = capture_runtime_evidence(
    backup_path=backup,
    writer_monitor_path=writer,
    locks_path=locks,
    services_path=services,
    execution_path=execution,
    cycle_path=cycle,
    attribution_path=attribution,
    rollback_root=Path("scripts"),
    rollback_paths=["prov14_bounded_cycle.py", "prov14_certify.py"],
    captured_at=AS_OF - timedelta(minutes=1),
)
print(write_capture(report, ROOT / "prov14b_r2b_runtime_evidence_capture_preview.json"))
