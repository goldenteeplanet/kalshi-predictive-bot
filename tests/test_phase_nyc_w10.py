import json
from pathlib import Path

from kalshi_predictor.phase_nyc_w10 import write_nyc_w10_review


def _write(path: Path, payload: dict) -> Path:
    path.write_text(json.dumps(payload))
    return path


def test_w10_refuses_decision_before_three_windows(tmp_path: Path) -> None:
    w8 = _write(tmp_path / "w8.json", {
        "feature_flag": "WEATHER_V2_KNYC_OBSERVATION_ENABLED=false",
        "execution_enabled": False,
        "summary": {"certified_live_windows": 2, "live_shadow_census_passed": False,
                    "mean_absolute_shadow_change": "0.02", "drift_blocker_counts": {},
                    "gates": {"all_windows_drift_free": True,
                              "rollback_continuously_verified": True}},
    })
    w9 = _write(tmp_path / "w9.json", {
        "status": "WINDOW_CERTIFIED", "feature_flag_enabled": False,
        "execution_enabled": False,
    })
    report = json.loads(write_nyc_w10_review(
        w8_report=w8, w9_report=w9, output_dir=tmp_path / "out",
        operations_evidence={"w8_timer_active": True, "w9_timer_active": True,
                             "failed_runs": 0},
    ).read_text())
    assert report["summary"]["review_ready"] is False
    assert report["summary"]["automatic_action_taken"] is False


def test_w10_only_recommends_manual_review_after_all_gates(tmp_path: Path) -> None:
    w8 = _write(tmp_path / "w8.json", {
        "feature_flag": "WEATHER_V2_KNYC_OBSERVATION_ENABLED=false",
        "execution_enabled": False,
        "summary": {"certified_live_windows": 3, "live_shadow_census_passed": True,
                    "mean_absolute_shadow_change": "0.02", "drift_blocker_counts": {},
                    "gates": {"all_windows_drift_free": True,
                              "rollback_continuously_verified": True}},
    })
    w9 = _write(tmp_path / "w9.json", {
        "status": "COMPLETE", "feature_flag_enabled": False,
        "execution_enabled": False,
    })
    report = json.loads(write_nyc_w10_review(
        w8_report=w8, w9_report=w9, output_dir=tmp_path / "out",
        operations_evidence={"w8_timer_active": True, "w9_timer_active": True,
                             "failed_runs": 0},
    ).read_text())
    assert report["summary"]["decision"] == "ELIGIBLE_FOR_MANUAL_ACTIVATION_REVIEW"
    assert report["automatic_activation_permitted"] is False
