import json
from pathlib import Path

from kalshi_predictor.phase_gh1v import write_multi_window_report


def test_gh1v_attributes_only_positive_edge_blockers(tmp_path: Path) -> None:
    source = tmp_path / "phase_gh1u_a"
    source.mkdir()
    (source / "gh1u_lead_time_atomic_activation.json").write_text(json.dumps({
        "pinned_tickers": {"weather_v2": ["KXTEMPNYCH-26JUL1611-T80.99"]},
        "immediate_evaluations": [
            {"ticker": "A", "model_name": "weather_v2", "executable_edge": "0.03",
             "opportunity_score": "25", "liquidity_score": "4", "spread": None,
             "time_to_close_minutes": "42", "blockers": ["LIQUIDITY_SCORE_BELOW_EXECUTABLE"],
             "advance": False},
            {"ticker": "B", "executable_edge": "-0.01", "blockers": ["EDGE_BELOW_MINIMUM"]},
        ],
    }), encoding="utf-8")
    path = write_multi_window_report(reports_dir=tmp_path, output_dir=tmp_path / "out")
    report = json.loads(path.read_text())
    assert report["summary"]["positive_edge"] == 1
    assert report["summary"]["positive_edge_blocker_counts"] == {
        "LIQUIDITY_SCORE_BELOW_EXECUTABLE": 1
    }
