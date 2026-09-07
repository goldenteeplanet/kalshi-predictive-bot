import json
from copy import deepcopy
from pathlib import Path

import pytest

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash
from scripts.local.phase4df_forecast_cache_eligibility_gate import (
    INPUT_SCHEMA,
    build_report,
    publish,
)

H1 = "1" * 64
H2 = "2" * 64
H3 = "3" * 64
H4 = "4" * 64


def signed(payload):
    payload.pop("artifact_hash", None)
    payload["artifact_hash"] = canonical_hash(payload)
    return payload


def candidate(cache_id="cache-1"):
    return signed(
        {
            "cache_id": cache_id,
            "market_id": "market-1",
            "ticker": "TICKER",
            "model_id": "model-1",
            "model_hash": H1,
            "feature_set_hash": H2,
            "evidence_hash": H3,
            "evidence_observed_at": "2026-08-26T00:05:00Z",
            "created_at": "2026-08-26T00:06:00Z",
            "expires_at": "2026-08-26T00:20:00Z",
            "deterministic": True,
            "complete": True,
            "result_hash": H4,
        }
    )


def fixture(candidates=None, evaluated_at="2026-08-26T00:10:00Z"):
    return signed(
        {
            "schema": INPUT_SCHEMA,
            "request": {
                "request_id": "request-1",
                "market_id": "market-1",
                "ticker": "TICKER",
                "model_id": "model-1",
                "model_hash": H1,
                "feature_set_hash": H2,
                "evidence_hash": H3,
                "evaluated_at": evaluated_at,
                "minimum_evidence_observed_at": "2026-08-26T00:05:00Z",
            },
            "candidates": [candidate()] if candidates is None else candidates,
        }
    )


@pytest.mark.parametrize("evaluated_at", ["2026-08-26T00:06:00Z", "2026-08-26T00:20:00Z"])
def test_exact_candidate_reused_at_inclusive_time_boundaries(evaluated_at):
    report = build_report(fixture(evaluated_at=evaluated_at))
    assert report["disposition"] == "REUSE"
    assert report["selected_cache_id"] == "cache-1"


@pytest.mark.parametrize(
    "field,value,reason",
    [
        ("model_hash", "9" * 64, "MODEL_HASH_MISMATCH"),
        ("feature_set_hash", "9" * 64, "FEATURE_SET_HASH_MISMATCH"),
        ("evidence_hash", "9" * 64, "EVIDENCE_HASH_MISMATCH"),
        ("ticker", "WRONG", "TICKER_MISMATCH"),
        ("deterministic", False, "NONDETERMINISTIC_RESULT"),
        ("complete", False, "INCOMPLETE_RESULT"),
    ],
)
def test_mismatch_forces_recompute(field, value, reason):
    item = candidate()
    item[field] = value
    signed(item)
    report = build_report(fixture([item]))
    assert report["disposition"] == "RECOMPUTE"
    assert reason in report["candidate_decisions"][0]["reasons"]


def test_expired_and_not_yet_valid_force_recompute():
    assert (
        build_report(fixture(evaluated_at="2026-08-26T00:20:00.000001Z"))["disposition"]
        == "RECOMPUTE"
    )
    assert build_report(fixture(evaluated_at="2026-08-26T00:05:59Z"))["disposition"] == "RECOMPUTE"


def test_old_evidence_forces_recompute():
    item = candidate()
    item["evidence_observed_at"] = "2026-08-26T00:04:59Z"
    item["created_at"] = "2026-08-26T00:05:00Z"
    signed(item)
    report = build_report(fixture([item]))
    assert "EVIDENCE_TOO_OLD" in report["candidate_decisions"][0]["reasons"]


def test_no_candidate_recomputes_and_multiple_exact_refuse():
    assert build_report(fixture([]))["disposition"] == "RECOMPUTE"
    report = build_report(fixture([candidate("a"), candidate("b")]))
    assert report["disposition"] == "REFUSE"
    assert report["reasons"] == ["AMBIGUOUS_ELIGIBLE_CANDIDATES"]


def test_candidate_tampering_fails_closed():
    item = candidate()
    item["result_hash"] = "9" * 64
    with pytest.raises(ValueError, match="CANDIDATE_HASH"):
        build_report(fixture([item]))


def test_input_tampering_fails_closed():
    payload = fixture()
    payload["request"]["ticker"] = "WRONG"
    with pytest.raises(ValueError, match="SCHEMA_OR_HASH"):
        build_report(payload)


def test_invalid_candidate_timeline_fails_closed():
    item = candidate()
    item["created_at"], item["expires_at"] = item["expires_at"], item["created_at"]
    signed(item)
    with pytest.raises(ValueError, match="TIMELINE"):
        build_report(fixture([item]))


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


def test_non_executable_and_read_only():
    report = build_report(fixture())
    assert report["cache_writes"] == 0
    assert report["forecast_records_created"] == 0
    assert report["execution_authorized"] is False


def test_source_has_no_connected_or_trading_surface():
    source = (
        Path(__file__).parents[1] / "scripts/local/phase4df_forecast_cache_eligibility_gate.py"
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
