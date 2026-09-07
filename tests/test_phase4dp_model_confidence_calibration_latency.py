import json
from copy import deepcopy
from pathlib import Path

import pytest

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash
from scripts.local.phase4dp_model_confidence_calibration_latency import (
    INPUT_SCHEMA,
    build_report,
    publish,
)

H1, H2, H3 = "1" * 64, "2" * 64, "3" * 64


def signed(payload):
    payload.pop("artifact_hash", None)
    payload["artifact_hash"] = canonical_hash(payload)
    return payload


def request(identifier, **overrides):
    value = {
        "request_id": identifier,
        "decision_at": "2026-08-26T00:10:00Z",
        "model_hash": H1,
        "calibration_version": "v1",
        "segment": "rain",
        "training_data_hash": H2,
        "trained_through": "2026-08-26T00:10:00Z",
        "valid_until": "2026-08-26T01:00:00Z",
        "calibration_hash": H3,
        "work_units": 100,
    }
    value.update(overrides)
    return value


def fixture(requests=None):
    return signed({"schema": INPUT_SCHEMA, "requests": requests or [request("a"), request("b")]})


def test_identical_safe_calibrations_reuse_and_save_work():
    report = build_report(fixture())
    assert report["baseline_work_units"] == 200
    assert report["reusable_work_units_saved"] == 100
    assert report["optimized_work_units"] == 100
    assert report["no_lookahead_preserved"] is True


def test_training_cutoff_equal_to_decision_is_allowed():
    assert build_report(fixture())["request_decisions"][0]["eligible_for_reuse"] is True


def test_one_microsecond_of_lookahead_is_ineligible():
    item = request("a", trained_through="2026-08-26T00:10:00.000001Z")
    decision = build_report(fixture([item]))["request_decisions"][0]
    assert decision["eligible_for_reuse"] is False
    assert decision["reasons"] == ["LOOKAHEAD_TRAINING_DATA"]


def test_expired_calibration_is_ineligible_at_one_microsecond_past():
    item = request("a", valid_until="2026-08-26T00:09:59.999999Z")
    decision = build_report(fixture([item]))["request_decisions"][0]
    assert decision["reasons"] == ["CALIBRATION_EXPIRED"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("model_hash", "9" * 64),
        ("calibration_version", "v2"),
        ("segment", "snow"),
        ("training_data_hash", "9" * 64),
        ("calibration_hash", "9" * 64),
    ],
)
def test_identity_changes_break_reuse(field, value):
    report = build_report(fixture([request("a"), request("b", **{field: value})]))
    assert report["reuse_groups"] == []
    assert report["optimized_work_units"] == 200


def test_ambiguous_cost_for_same_identity_fails_closed():
    with pytest.raises(ValueError, match="REUSE_COST_AMBIGUOUS"):
        build_report(fixture([request("a", work_units=100), request("b", work_units=101)]))


@pytest.mark.parametrize("work", [0, -1, True])
def test_invalid_work_units_fail_closed(work):
    with pytest.raises(ValueError, match="WORK_UNITS"):
        build_report(fixture([request("a", work_units=work)]))


def test_duplicate_request_fails_closed():
    with pytest.raises(ValueError, match="REQUEST_ID"):
        build_report(fixture([request("a"), request("a")]))


def test_tampering_fails_closed():
    payload = fixture()
    payload["requests"][0]["segment"] = "changed"
    with pytest.raises(ValueError, match="SCHEMA_OR_HASH"):
        build_report(payload)


def test_deterministic_and_nonmutating():
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


def test_non_executable_and_no_wall_clock_gate():
    report = build_report(fixture())
    assert report["wall_clock_used_as_gate"] is False
    assert report["calibration_records_created"] == 0
    assert report["execution_authorized"] is False


def test_source_has_no_connected_or_trading_surface():
    source = (
        Path(__file__).parents[1] / "scripts/local/phase4dp_model_confidence_calibration_latency.py"
    ).read_text()
    for token in (
        "sqlite3",
        "import requests",
        "from requests",
        "subprocess",
        "systemctl",
        "exchange_client",
        "create_order",
        "/home/james",
    ):
        assert token not in source
