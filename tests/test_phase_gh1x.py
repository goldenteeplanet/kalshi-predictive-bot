from __future__ import annotations

from kalshi_predictor.phase_gh1x import build_gh1x_census


def _row(window: str, ticker: str, blockers: list[str], *, advance: bool = False):
    return {
        "window_id": window,
        "ticker": ticker,
        "model_name": "weather_v2",
        "executable_edge": "0.01",
        "opportunity_score": "25",
        "liquidity_score": "40",
        "spread": "0.02",
        "time_to_close_minutes": "35",
        "blockers": blockers,
        "advance": advance,
    }


def _source(rows):
    return {
        "phase": "GH-1V",
        "execution_enabled": False,
        "thresholds_changed": False,
        "summary": {
            "multi_window_complete": True,
            "execution_remains_disabled": True,
            "evaluated": 90,
            "positive_edge": len(rows),
        },
        "windows": [
            {"window_id": f"w{i}", "source_path": f"report-{i}", "positive_edge": 1}
            for i in range(1, 4)
        ],
        "positive_edge_rows": rows,
    }


def test_exact_three_window_census_passes() -> None:
    report = build_gh1x_census(_source([_row("w1", "a", ["EDGE_BELOW_MINIMUM"])]))
    assert report["status"] == "PASSED"
    assert report["decision"] == "CLOSE_CANDIDATE_SET_AND_RETURN_TO_BOUNDED_DISCOVERY"


def test_duplicate_rows_are_deduplicated() -> None:
    row = _row("w1", "a", ["EDGE_BELOW_MINIMUM"])
    report = build_gh1x_census(_source([row, row]))
    assert report["counts"]["unique_positive_edge_candidates"] == 1
    assert report["counts"]["duplicate_positive_edge_rows"] == 1


def test_natural_passer_advances_without_threshold_change() -> None:
    report = build_gh1x_census(_source([_row("w1", "a", [], advance=True)]))
    assert report["decision"] == "ADVANCE_NATURAL_PASSERS"
    assert report["counts"]["naturally_advanced_candidates"] == 1


def test_execution_or_threshold_change_fails_closed() -> None:
    source = _source([])
    source["execution_enabled"] = True
    source["thresholds_changed"] = True
    report = build_gh1x_census(source)
    assert report["status"] == "FAILED"
    assert report["gates"]["execution_disabled"] is False
    assert report["gates"]["thresholds_unchanged"] is False


def test_report_is_deterministic() -> None:
    source = _source([_row("w1", "a", ["EDGE_BELOW_MINIMUM"])])
    assert build_gh1x_census(source) == build_gh1x_census(source)
