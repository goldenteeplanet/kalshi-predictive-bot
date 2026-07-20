import json
from pathlib import Path

from kalshi_predictor.phase_nyc_w8 import write_nyc_w8_report


def _write_window(root: Path, number: int, *, offset: int = 540, applied: bool = False) -> None:
    target = f"2026-07-16T0{number}:00:00+00:00"
    baseline = "0.40"
    payload = {
        "feature_flag": "WEATHER_V2_KNYC_OBSERVATION_ENABLED=false",
        "execution_enabled": False,
        "database_writes": 0,
        "rows": [{
            "ticker": f"KXTEMPNYCH-26JUL160{number}-T78.99",
            "target_utc_time": target,
            "passed": True,
            "runtime_applied": applied,
            "baseline_probability": baseline,
            "runtime_probability": "0.45" if applied else baseline,
            "shadow_probability": "0.45",
            "shadow_change": "0.05",
            "provenance": {
                "evidence_source": "NOAA_KNYC",
                "evidence_role": "NON_SETTLEMENT_POINT_OBSERVATION",
                "settlement_source": "THE_WEATHER_COMPANY",
                "station_id": "KNYC",
                "target_utc_time": target,
                "offset_seconds": offset,
            },
        }],
    }
    directory = root / f"phase_nyc_w7_live_{number}"
    directory.mkdir()
    (directory / "nyc_w7_shadow_observation_runtime_report.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )


def test_nyc_w8_certifies_three_drift_free_windows(tmp_path: Path) -> None:
    for number in (3, 4, 5):
        _write_window(tmp_path, number)
    path = write_nyc_w8_report(reports_dir=tmp_path, output_dir=tmp_path / "out")
    report = json.loads(path.read_text(encoding="utf-8"))
    assert report["summary"]["distinct_live_windows"] == 3
    assert report["summary"]["live_shadow_census_passed"] is True
    assert report["database_writes"] == 0
    assert report["execution_enabled"] is False


def test_nyc_w8_blocks_alignment_drift_and_failed_rollback(tmp_path: Path) -> None:
    _write_window(tmp_path, 3)
    _write_window(tmp_path, 4, offset=901)
    _write_window(tmp_path, 5, applied=True)
    path = write_nyc_w8_report(reports_dir=tmp_path, output_dir=tmp_path / "out")
    summary = json.loads(path.read_text(encoding="utf-8"))["summary"]
    assert summary["live_shadow_census_passed"] is False
    assert summary["drift_blocker_counts"]["ALIGNMENT_DRIFT"] == 1
    assert summary["drift_blocker_counts"]["ROLLBACK_NOT_EXACT"] == 1
