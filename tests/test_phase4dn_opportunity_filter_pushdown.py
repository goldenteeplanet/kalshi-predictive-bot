import json
from copy import deepcopy
from pathlib import Path

import pytest

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash
from scripts.local.phase4dn_opportunity_filter_pushdown import INPUT_SCHEMA, build_report, publish


def signed(payload):
    payload.pop("artifact_hash", None)
    payload["artifact_hash"] = canonical_hash(payload)
    return payload


def candidate(identifier, **overrides):
    value = {
        "candidate_id": identifier,
        "market_open": True,
        "evidence_fresh": True,
        "hard_blocked": False,
        "max_possible_score": "10",
        "full_score": "8",
    }
    value.update(overrides)
    return value


def fixture(candidates=None):
    return signed(
        {
            "schema": INPUT_SCHEMA,
            "minimum_score": "5",
            "candidates": candidates
            or [
                candidate("eligible"),
                candidate("low", max_possible_score="4", full_score="3"),
                candidate("closed", market_open=False),
                candidate("stale", evidence_fresh=False),
                candidate("blocked", hard_blocked=True),
            ],
        }
    )


def test_safe_pushdown_preserves_all_baseline_eligible_candidates():
    report = build_report(fixture())
    assert report["baseline_eligible_ids"] == ["eligible"]
    assert report["false_rejection_ids"] == []
    assert report["full_eligibility_preserved"] is True
    assert report["expensive_computations_avoided"] == 4


def test_upper_bound_equal_to_minimum_is_not_rejected():
    item = candidate("boundary", max_possible_score="5", full_score="4")
    decision = build_report(fixture([item]))["candidate_decisions"][0]
    assert decision["pushdown_disposition"] == "KEEP_FOR_FULL_RANKING"
    assert decision["baseline_eligible"] is False


def test_full_score_equal_to_minimum_is_eligible():
    item = candidate("boundary", max_possible_score="5", full_score="5")
    report = build_report(fixture([item]))
    assert report["baseline_eligible_ids"] == ["boundary"]
    assert report["pushdown_survivor_ids"] == ["boundary"]


def test_multiple_cheap_rejection_reasons_are_retained():
    item = candidate(
        "bad",
        market_open=False,
        evidence_fresh=False,
        hard_blocked=True,
        max_possible_score="4",
        full_score="3",
    )
    reasons = build_report(fixture([item]))["candidate_decisions"][0]["reasons"]
    assert reasons == [
        "MARKET_CLOSED",
        "EVIDENCE_STALE",
        "HARD_BLOCKED",
        "PROVABLE_SCORE_UPPER_BOUND_BELOW_MINIMUM",
    ]


def test_invalid_upper_bound_fails_closed():
    with pytest.raises(ValueError, match="UPPER_BOUND_VIOLATED"):
        build_report(fixture([candidate("bad", max_possible_score="4", full_score="5")]))


@pytest.mark.parametrize(
    "field,value", [("market_open", 1), ("evidence_fresh", None), ("hard_blocked", "false")]
)
def test_non_boolean_policy_fails_closed(field, value):
    with pytest.raises(ValueError, match="BOOLEAN_POLICY"):
        build_report(fixture([candidate("bad", **{field: value})]))


@pytest.mark.parametrize(
    "field,value",
    [("minimum_score", "NaN"), ("full_score", "Infinity"), ("max_possible_score", " 5")],
)
def test_invalid_decimal_fails_closed(field, value):
    payload = fixture([candidate("bad", **({field: value} if field != "minimum_score" else {}))])
    if field == "minimum_score":
        payload["minimum_score"] = value
    signed(payload)
    with pytest.raises(ValueError, match="DECIMAL"):
        build_report(payload)


def test_duplicate_candidate_fails_closed():
    with pytest.raises(ValueError, match="CANDIDATE_ID"):
        build_report(fixture([candidate("a"), candidate("a")]))


def test_tampering_fails_closed():
    payload = fixture()
    payload["minimum_score"] = "99"
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


def test_non_executable():
    report = build_report(fixture())
    assert report["ranking_records_created"] == 0
    assert report["execution_authorized"] is False


def test_source_has_no_connected_or_trading_surface():
    source = (
        Path(__file__).parents[1] / "scripts/local/phase4dn_opportunity_filter_pushdown.py"
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
