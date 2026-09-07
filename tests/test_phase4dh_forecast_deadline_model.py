import json
from copy import deepcopy
from pathlib import Path

import pytest

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash
from scripts.local.phase4dh_forecast_deadline_model import INPUT_SCHEMA, build_report, publish


def signed(payload):
    payload.pop("artifact_hash", None)
    payload["artifact_hash"] = canonical_hash(payload)
    return payload


def forecast(identifier="forecast-1", **overrides):
    value = {
        "forecast_id": identifier,
        "market_id": "market-1",
        "evidence_observed_at": "2026-08-26T00:00:00Z",
        "freshness_window_seconds": 900,
        "market_close_at": "2026-08-26T01:00:00Z",
        "ranking_budget_seconds": 120,
        "risk_budget_seconds": 180,
        "publication_buffer_seconds": 60,
    }
    value.update(overrides)
    return value


def fixture(forecasts=None, evaluated_at="2026-08-26T00:05:00Z"):
    return signed(
        {
            "schema": INPUT_SCHEMA,
            "evaluated_at": evaluated_at,
            "forecasts": forecasts or [forecast()],
        }
    )


def test_freshness_is_earlier_deadline():
    assignment = build_report(fixture())["assignments"][0]
    assert assignment["compute_deadline"] == "2026-08-26T00:15:00.000000Z"
    assert assignment["limiting_constraint"] == "EVIDENCE_FRESHNESS"
    assert assignment["remaining_compute_microseconds"] == 600_000_000


def test_market_close_reserves_are_earlier_deadline():
    item = forecast(freshness_window_seconds=7200)
    assignment = build_report(fixture([item]))["assignments"][0]
    assert assignment["compute_deadline"] == "2026-08-26T00:54:00.000000Z"
    assert assignment["limiting_constraint"] == "MARKET_CLOSE_RESERVES"


def test_equal_constraints_report_both():
    item = forecast(freshness_window_seconds=3240)
    assert build_report(fixture([item]))["assignments"][0]["limiting_constraint"] == "BOTH"


def test_elapsed_deadline_is_explicit_not_silently_extended():
    item = forecast(freshness_window_seconds=60)
    assignment = build_report(fixture([item]))["assignments"][0]
    assert assignment["deadline_already_elapsed"] is True
    assert assignment["remaining_compute_microseconds"] == -240_000_000


def test_assignments_sorted_by_deadline_then_id():
    rows = build_report(fixture([forecast("b"), forecast("a")]))["assignments"]
    assert [row["forecast_id"] for row in rows] == ["a", "b"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("freshness_window_seconds", 0),
        ("freshness_window_seconds", True),
        ("ranking_budget_seconds", -1),
        ("risk_budget_seconds", 31_536_001),
    ],
)
def test_invalid_duration_fails_closed(field, value):
    item = forecast(**{field: value})
    with pytest.raises(ValueError, match="DURATION"):
        build_report(fixture([item]))


def test_future_evidence_and_closed_market_fail_closed():
    with pytest.raises(ValueError, match="TIMELINE"):
        build_report(fixture([forecast(evidence_observed_at="2026-08-26T00:05:01Z")]))
    with pytest.raises(ValueError, match="TIMELINE"):
        build_report(fixture([forecast(market_close_at="2026-08-26T00:05:00Z")]))


def test_duplicate_forecast_id_fails_closed():
    with pytest.raises(ValueError, match="IDENTITY"):
        build_report(fixture([forecast(), forecast()]))


def test_tampering_fails_closed():
    payload = fixture()
    payload["evaluated_at"] = "2026-08-26T00:06:00Z"
    with pytest.raises(ValueError, match="SCHEMA_OR_HASH"):
        build_report(payload)


def test_deterministic_and_does_not_mutate_input():
    payload = fixture()
    before = deepcopy(payload)
    assert build_report(payload) == build_report(payload)
    assert payload == before


def test_atomic_publication(tmp_path):
    output = tmp_path / "nested" / "report.json"
    report = build_report(fixture())
    publish(output, report)
    assert json.loads(output.read_text()) == report
    assert not list(output.parent.glob(".*"))


def test_non_executable():
    report = build_report(fixture())
    assert report["forecast_records_created"] == 0
    assert report["execution_authorized"] is False


def test_source_has_no_connected_or_trading_surface():
    source = (
        Path(__file__).parents[1] / "scripts/local/phase4dh_forecast_deadline_model.py"
    ).read_text()
    for token in (
        "sqlite3",
        "requests",
        "subprocess",
        "systemctl",
        "exchange_client",
        "create_order",
        "/home/james",
    ):
        assert token not in source
