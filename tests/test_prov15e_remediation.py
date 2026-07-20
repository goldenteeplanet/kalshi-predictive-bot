import copy
from datetime import UTC, datetime

import pytest

from kalshi_predictor.provenance.remediation import simulate_exact_attribution_repairs

NOW = datetime(2026, 7, 17, 22, 0, tzinfo=UTC)


def test_prov15e_exact_repairs_produce_passing_before_after_certification():
    events = [_event("crypto", "crypto_v2"), _event("weather", "weather_v2")]
    source = copy.deepcopy(events)
    report = simulate_exact_attribution_repairs(
        events,
        repairs_by_event_key={
            "crypto": _repair("crypto_prices", 1, observation_age=60, snapshot_id=11),
            "weather": _repair("weather_forecasts", 2, observation_age=300, snapshot_id=12),
        },
        expected_model_versions={"crypto_v2": ["2.0.0"], "weather_v2": ["2.0.0"]},
        generated_at=NOW,
    )
    assert report["certification"] == {
        "before_passed": False, "after_passed": True,
        "exact_repairs_only": True, "source_rows_mutated": False,
    }
    assert report["delta"]["events_passed"] == 2
    assert report["delta"]["events_failed"] == -2
    assert report["delta"]["coverage_ratio"] == 0.0
    assert events == source


def test_prov15e_measures_model_revision_coverage_improvement():
    event = _event("crypto", "crypto_v2")
    event["model_version"] = "MISSING"
    report = simulate_exact_attribution_repairs(
        [event], repairs_by_event_key={"crypto": {
            **_repair("crypto_prices", 1, observation_age=60, snapshot_id=11),
            "model_version": "2.0.0",
        }}, expected_model_versions={"crypto_v2": ["2.0.0"]}, generated_at=NOW,
    )
    assert report["before"]["summary"]["coverage_ratio"] == 0.0
    assert report["after"]["summary"]["coverage_ratio"] == 1.0
    assert report["delta"]["coverage_ratio"] == 1.0


def test_prov15e_rejects_fuzzy_incomplete_or_unknown_repairs():
    event = _event("crypto", "crypto_v2")
    with pytest.raises(ValueError, match="exact table and id"):
        simulate_exact_attribution_repairs(
            [event], repairs_by_event_key={"crypto": {"source_observation_ref": {}}},
            expected_model_versions={}, generated_at=NOW,
        )
    with pytest.raises(ValueError, match="unsupported repair fields"):
        simulate_exact_attribution_repairs(
            [event], repairs_by_event_key={"crypto": {"ticker_guess": "X"}},
            expected_model_versions={}, generated_at=NOW,
        )
    with pytest.raises(ValueError, match="not found"):
        simulate_exact_attribution_repairs(
            [event], repairs_by_event_key={"missing": {}},
            expected_model_versions={}, generated_at=NOW,
        )


def _event(key, model):
    return {
        "event_key": key, "ticker": key.upper(), "model_name": model,
        "model_version": "2.0.0", "event_at": NOW.isoformat(),
        "source_observation_ref": None, "market_snapshot_ref": None,
    }


def _repair(table, observation_id, *, observation_age, snapshot_id):
    from datetime import timedelta
    return {
        "source_observation_ref": {
            "table": table, "id": observation_id,
            "observed_at": (NOW - timedelta(seconds=observation_age)).isoformat(),
        },
        "market_snapshot_ref": {
            "table": "market_snapshots", "id": snapshot_id,
            "captured_at": (NOW - timedelta(seconds=30)).isoformat(),
        },
    }
