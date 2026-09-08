import json
from copy import deepcopy
from pathlib import Path

import pytest

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash
from scripts.local.phase4do_near_money_selection_optimization import (
    INPUT_SCHEMA,
    build_report,
    publish,
)


def signed(payload):
    payload.pop("artifact_hash", None)
    payload["artifact_hash"] = canonical_hash(payload)
    return payload


def candidate(identifier, distance, last):
    return {"candidate_id": identifier, "distance_to_money": distance, "last_selected_cycle": last}


def fixture(candidates=None, limit=3):
    return signed(
        {
            "schema": INPUT_SCHEMA,
            "current_cycle": 10,
            "near_money_threshold": "0.1",
            "starvation_cycles": 5,
            "selection_limit": limit,
            "candidates": candidates
            or [
                candidate("near", "0.05", 9),
                candidate("boundary", "0.1", 9),
                candidate("starved", "0.9", 5),
                candidate("far", "0.2", 9),
            ],
        }
    )


def test_near_money_and_starved_candidates_are_all_selected():
    report = build_report(fixture())
    assert {row["candidate_id"] for row in report["selected"]} == {"near", "boundary", "starved"}
    assert report["near_money_coverage_complete"] is True
    assert report["starvation_coverage_complete"] is True
    assert report["deferred_candidate_ids"] == ["far"]


def test_exact_threshold_and_starvation_boundaries_are_inclusive():
    rows = build_report(fixture())["selected"]
    assert next(row for row in rows if row["candidate_id"] == "boundary")["near_money"] is True
    assert next(row for row in rows if row["candidate_id"] == "starved")["starvation_due"] is True


def test_one_beyond_boundaries_is_not_mandatory_and_fill_is_deterministic():
    items = [candidate("b", "0.1001", 6), candidate("a", "0.1001", 6), candidate("c", "0.2", 9)]
    report = build_report(fixture(items, limit=1))
    assert [row["candidate_id"] for row in report["selected"]] == ["a"]


def test_mandatory_coverage_over_capacity_fails_closed():
    with pytest.raises(ValueError, match="MANDATORY_COVERAGE"):
        build_report(fixture(limit=2))


def test_input_order_does_not_change_selection():
    payload = fixture()
    reversed_payload = fixture(list(reversed(payload["candidates"])))
    assert build_report(payload)["selected"] == build_report(reversed_payload)["selected"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("current_cycle", -1),
        ("current_cycle", True),
        ("starvation_cycles", 0),
        ("selection_limit", 0),
    ],
)
def test_invalid_policy_fails_closed(field, value):
    payload = fixture()
    payload[field] = value
    signed(payload)
    with pytest.raises(ValueError, match="POLICY"):
        build_report(payload)


def test_future_last_selected_cycle_fails_closed():
    with pytest.raises(ValueError, match="LAST_SELECTED"):
        build_report(fixture([candidate("bad", "0.1", 11)], limit=1))


@pytest.mark.parametrize("distance", ["-0.1", "NaN", "Infinity", " 0.1"])
def test_invalid_distance_fails_closed(distance):
    with pytest.raises(ValueError, match="DECIMAL"):
        build_report(fixture([candidate("bad", distance, 1)], limit=1))


def test_duplicate_candidate_fails_closed():
    with pytest.raises(ValueError, match="CANDIDATE_ID"):
        build_report(fixture([candidate("a", "0.1", 1), candidate("a", "0.2", 2)], limit=1))


def test_tampering_fails_closed():
    payload = fixture()
    payload["selection_limit"] = 1
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
    assert report["selection_records_created"] == 0
    assert report["execution_authorized"] is False


def test_source_has_no_connected_or_trading_surface():
    source = (
        Path(__file__).parents[1] / "scripts/local/phase4do_near_money_selection_optimization.py"
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
