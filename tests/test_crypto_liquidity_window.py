import importlib.util
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path


def _module():
    path = Path(__file__).parents[1] / "scripts" / "crypto_liquidity_window_diagnosis.py"
    spec = importlib.util.spec_from_file_location("crypto_liquidity_window_diagnosis", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_diagnosis_finds_best_window_and_position_coverage():
    module = _module()
    captured = datetime.now(UTC)
    bounds = json.dumps(
        {
            "buckets": [
                {"kind": "lower_tail", "has_bid": False, "has_ask": True},
                {"kind": "interior", "has_bid": True, "has_ask": True},
                {"kind": "upper_tail", "has_bid": True, "has_ask": False},
            ]
        }
    )
    rows = [
        {
            "event_ticker": f"E{index}",
            "captured_at": captured.isoformat(),
            "close_time": (captured + timedelta(hours=hours)).isoformat(),
            "two_sided_coverage": coverage,
            "bounds_json": bounds,
        }
        for index, (hours, coverage) in enumerate(((3, 0.8), (3.5, 0.9), (9, 0.2)))
    ]
    result = module.diagnose(rows)
    assert result["recommended_window"]["window"] == "2_4H"
    assert result["scheduling_enabled"] is True
    positions = {row["position"]: row for row in result["bucket_position_diagnostics"]}
    assert positions["interior"]["two_sided_rate"] == 1.0
    assert positions["lower_tail"]["two_sided_rate"] == 0.0
    assert positions["lower_tail"]["no_side_only_upper_bound_rate"] == 1.0
