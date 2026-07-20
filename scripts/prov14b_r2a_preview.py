"""Generate the deterministic local PROV-14B-R2A preview artifact."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

from kalshi_predictor.phase_prov14b_r2a import build_certification_bundle, write_bundle

AS_OF = datetime(2026, 7, 19, 18, 0, tzinfo=UTC)
ROLLBACK_ROOT = Path("scripts")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


rollback_files = [
    {"path": name, "sha256": _sha(ROLLBACK_ROOT / name)}
    for name in ("prov14_bounded_cycle.py", "prov14_certify.py")
]
backup = {
    "path": "/mnt/kalshi-backup-02/prov14b_r2/PREVIEW.db",
    "size_bytes": 23_107_870_720,
    "quick_check": "ok",
    "sha256": "a" * 64,
    "integrity_check": "ok",
    "execution_enabled": False,
    "finished_at": (AS_OF - timedelta(minutes=10)).isoformat(),
}
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
    "tickers": {"crypto_v2": ["PREVIEW-BTC"], "weather_v2": ["PREVIEW-WX"]},
    "summaries": {
        "crypto_v2": {"snapshots_scanned": 1, "forecasts_inserted": 1, "skipped": 0},
        "weather_v2": {"snapshots_scanned": 1, "forecasts_inserted": 1, "skipped": 0},
    },
}
rows = [
    {
        "event_id": event_id,
        "model_name": model,
        "forecast_id": event_id + 100,
        "source_observation_ref": {"table": "preview_observations", "id": event_id + 200},
        "market_snapshot_id": event_id + 300,
        "feature_source_table": "preview_features",
        "feature_source_id": event_id + 400,
        "passed": True,
        "failures": [],
    }
    for event_id, model in ((101, "crypto_v2"), (102, "weather_v2"))
]
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
report = build_certification_bundle(
    backup=backup,
    rollback={"files": rollback_files},
    safety=safety,
    cycle=cycle,
    attribution=attribution,
    rollback_root=ROLLBACK_ROOT,
    as_of=AS_OF,
    synthetic_preview=True,
)
print(
    write_bundle(
        report,
        Path("reports/phase_prov14b_r2a/prov14b_r2a_certification_bundle_preview.json"),
    )
)
