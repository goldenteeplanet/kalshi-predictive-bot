import json
from copy import deepcopy
from pathlib import Path

import pytest

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash
from scripts.local.phase4di_late_forecast_refusal import INPUT_SCHEMA, build_report, publish

H1 = "1" * 64


def signed(payload):
    payload.pop("artifact_hash", None)
    payload["artifact_hash"] = canonical_hash(payload)
    return payload


def forecast(identifier="forecast-1", deadline="2026-08-26T00:05:01Z", **overrides):
    value = {
        "forecast_id": identifier,
        "compute_deadline": deadline,
        "estimated_compute_microseconds": 700_000,
        "uncertainty_microseconds": 200_000,
        "safety_margin_microseconds": 100_000,
    }
    value.update(overrides)
    return value


def fixture(forecasts=None):
    return signed(
        {
            "schema": INPUT_SCHEMA,
            "evaluated_at": "2026-08-26T00:05:00Z",
            "deadline_report_hash": H1,
            "forecasts": forecasts or [forecast()],
        }
    )


def test_exact_boundary_is_accepted():
    decision = build_report(fixture())["decisions"][0]
    assert decision["disposition"] == "ACCEPT_OFFLINE_COMPUTE"
    assert decision["slack_microseconds"] == 0


def test_one_microsecond_short_is_refused():
    decision = build_report(fixture([forecast(deadline="2026-08-26T00:05:00.999999Z")]))[
        "decisions"
    ][0]
    assert decision["disposition"] == "REFUSE_LATE_FORECAST"
    assert decision["slack_microseconds"] == -1
    assert decision["reasons"] == ["INSUFFICIENT_RESERVED_COMPUTE_TIME"]


def test_elapsed_deadline_is_distinguished():
    decision = build_report(fixture([forecast(deadline="2026-08-26T00:04:59Z")]))["decisions"][0]
    assert decision["reasons"] == ["DEADLINE_ELAPSED"]


def test_zero_compute_envelope_at_now_is_accepted():
    item = forecast(
        deadline="2026-08-26T00:05:00Z",
        estimated_compute_microseconds=0,
        uncertainty_microseconds=0,
        safety_margin_microseconds=0,
    )
    assert build_report(fixture([item]))["decisions"][0]["disposition"] == "ACCEPT_OFFLINE_COMPUTE"


def test_mixed_decisions_and_stable_order():
    report = build_report(fixture([forecast("b"), forecast("a", deadline="2026-08-26T00:05:00Z")]))
    assert [row["forecast_id"] for row in report["decisions"]] == ["a", "b"]
    assert (report["accepted_count"], report["refused_count"]) == (1, 1)


@pytest.mark.parametrize(
    "field,value",
    [
        ("estimated_compute_microseconds", -1),
        ("uncertainty_microseconds", True),
        ("safety_margin_microseconds", 31_536_000_000_001),
    ],
)
def test_invalid_duration_fails_closed(field, value):
    with pytest.raises(ValueError, match="DURATION"):
        build_report(fixture([forecast(**{field: value})]))


def test_total_duration_overflow_fails_closed():
    item = forecast(estimated_compute_microseconds=31_536_000_000_000, uncertainty_microseconds=1)
    with pytest.raises(ValueError, match="TOTAL_DURATION"):
        build_report(fixture([item]))


def test_duplicate_id_fails_closed():
    with pytest.raises(ValueError, match="FORECAST_ID"):
        build_report(fixture([forecast(), forecast()]))


def test_tampering_fails_closed():
    payload = fixture()
    payload["forecasts"][0]["estimated_compute_microseconds"] = 1
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
        Path(__file__).parents[1] / "scripts/local/phase4di_late_forecast_refusal.py"
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
