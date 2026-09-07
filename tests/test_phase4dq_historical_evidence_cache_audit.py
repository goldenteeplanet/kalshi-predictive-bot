import json
from copy import deepcopy
from pathlib import Path

import pytest
from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

from scripts.local.phase4dq_historical_evidence_cache_audit import (
    ENTRY_SCHEMA,
    INPUT_SCHEMA,
    build_report,
    publish,
)

H1 = "1" * 64


def signed(payload):
    payload.pop("artifact_hash", None)
    payload["artifact_hash"] = canonical_hash(payload)
    return payload


def entry(identifier="entry-1", **overrides):
    value = {
        "schema": ENTRY_SCHEMA,
        "entry_id": identifier,
        "source_id": "source-1",
        "series_id": "series-1",
        "observed_at": "2026-08-26T00:00:00Z",
        "available_at": "2026-08-26T00:00:01Z",
        "content_hash": H1,
        "invalidation_generation": 3,
    }
    value.update(overrides)
    return signed(value)


def fixture(entries=None, max_age=600):
    return signed(
        {
            "schema": INPUT_SCHEMA,
            "decision_at": "2026-08-26T00:10:00Z",
            "max_age_seconds": max_age,
            "expected_source_id": "source-1",
            "expected_series_id": "series-1",
            "current_invalidation_generation": 3,
            "entries": entries or [entry()],
        }
    )


def test_valid_entry_is_eligible_at_exact_freshness_boundary():
    report = build_report(fixture())
    assert report["eligible_entry_ids"] == ["entry-1"]
    assert report["no_lookahead_preserved"] is True


def test_one_second_stale_is_ineligible():
    decision = build_report(fixture(max_age=599))["entry_decisions"][0]
    assert decision["eligible"] is False
    assert decision["reasons"] == ["EVIDENCE_STALE"]


def test_one_microsecond_beyond_freshness_is_ineligible():
    item = entry(observed_at="2026-08-25T23:59:59.999999Z")
    assert "EVIDENCE_STALE" in build_report(fixture([item]))["entry_decisions"][0]["reasons"]


def test_observation_or_availability_after_decision_is_no_lookahead_failure():
    future_observed = entry(
        observed_at="2026-08-26T00:10:00.000001Z", available_at="2026-08-26T00:10:00.000001Z"
    )
    reasons = build_report(fixture([future_observed]))["entry_decisions"][0]["reasons"]
    assert {"LOOKAHEAD_OBSERVATION", "NOT_AVAILABLE_AT_DECISION"} <= set(reasons)
    late_available = entry(available_at="2026-08-26T00:10:00.000001Z")
    assert (
        "NOT_AVAILABLE_AT_DECISION"
        in build_report(fixture([late_available]))["entry_decisions"][0]["reasons"]
    )


def test_availability_at_exact_decision_boundary_is_allowed():
    item = entry(available_at="2026-08-26T00:10:00Z")
    assert build_report(fixture([item]))["entry_decisions"][0]["eligible"] is True


@pytest.mark.parametrize(
    "field,value,reason",
    [
        ("source_id", "wrong", "SOURCE_ID_MISMATCH"),
        ("series_id", "wrong", "SERIES_ID_MISMATCH"),
        ("invalidation_generation", 2, "INVALIDATION_GENERATION_MISMATCH"),
        ("schema", "phase4dq.evidence-entry.v2", "SCHEMA_DRIFT"),
    ],
)
def test_identity_invalidation_and_schema_drift_are_ineligible(field, value, reason):
    item = entry(**{field: value})
    decision = build_report(fixture([item]))["entry_decisions"][0]
    assert decision["eligible"] is False
    assert reason in decision["reasons"]


def test_entry_tampering_is_detected():
    item = entry()
    item["content_hash"] = "9" * 64
    decision = build_report(fixture([item]))["entry_decisions"][0]
    assert "ARTIFACT_HASH_MISMATCH" in decision["reasons"]


def test_impossible_entry_timeline_fails_closed():
    item = entry(observed_at="2026-08-26T00:00:02Z", available_at="2026-08-26T00:00:01Z")
    with pytest.raises(ValueError, match="TIMELINE"):
        build_report(fixture([item]))


def test_duplicate_entry_id_fails_closed():
    with pytest.raises(ValueError, match="ENTRY_ID"):
        build_report(fixture([entry(), entry()]))


def test_tampering_outer_input_fails_closed():
    payload = fixture()
    payload["decision_at"] = "2026-08-27T00:00:00Z"
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


def test_non_executable_and_does_not_consume_cache():
    report = build_report(fixture())
    assert report["cache_entries_consumed"] == 0
    assert report["cache_entries_written"] == 0
    assert report["execution_authorized"] is False


def test_source_has_no_connected_or_trading_surface():
    source = (
        Path(__file__).parents[1] / "scripts/local/phase4dq_historical_evidence_cache_audit.py"
    ).read_text()
    for token in (
        "sqlite3",
        "import requests",
        "subprocess",
        "systemctl",
        "exchange_client",
        "create_order",
        "/home/james",
    ):
        assert token not in source
