import json
from copy import deepcopy
from pathlib import Path

import pytest

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash
from scripts.local.phase4dk_incremental_ranking_update import INPUT_SCHEMA, build_report, publish


def signed(payload):
    payload.pop("artifact_hash", None)
    payload["artifact_hash"] = canonical_hash(payload)
    return payload


def fixture(updates=None):
    return signed(
        {
            "schema": INPUT_SCHEMA,
            "candidates": {"a": "10", "b": "8", "c": "8", "d": "1"},
            "previous_ranking": [
                {"rank": 1, "candidate_id": "a", "score": "10"},
                {"rank": 2, "candidate_id": "b", "score": "8"},
                {"rank": 3, "candidate_id": "c", "score": "8"},
                {"rank": 4, "candidate_id": "d", "score": "1"},
            ],
            "updates": updates or [{"candidate_id": "d", "operation": "UPSERT", "score": "9"}],
        }
    )


def test_changed_score_matches_full_ranking():
    report = build_report(fixture())
    assert report["full_equivalence"] is True
    assert report["incremental_ranking_hash"] == report["full_ranking_hash"]
    assert [row["candidate_id"] for row in report["ranking"]] == ["a", "d", "b", "c"]
    assert report["unchanged_candidate_ids"] == ["a", "b", "c"]


def test_insert_delete_and_tie_order():
    report = build_report(
        fixture(
            [
                {"candidate_id": "a", "operation": "DELETE", "score": None},
                {"candidate_id": "aa", "operation": "UPSERT", "score": "8.0"},
            ]
        )
    )
    assert [row["candidate_id"] for row in report["ranking"]] == ["aa", "b", "c", "d"]
    assert [row["rank"] for row in report["ranking"]] == [1, 2, 3, 4]


def test_negative_and_exact_decimal_scores():
    report = build_report(
        fixture([{"candidate_id": "d", "operation": "UPSERT", "score": "8.0000000000000000001"}])
    )
    assert [row["candidate_id"] for row in report["ranking"]][:2] == ["a", "d"]


def test_stale_previous_ranking_fails_closed():
    payload = fixture()
    payload["previous_ranking"][1], payload["previous_ranking"][2] = (
        payload["previous_ranking"][2],
        payload["previous_ranking"][1],
    )
    signed(payload)
    with pytest.raises(ValueError, match="PREVIOUS_RANKING_MISMATCH"):
        build_report(payload)


def test_duplicate_update_fails_closed():
    update = {"candidate_id": "d", "operation": "UPSERT", "score": "9"}
    with pytest.raises(ValueError, match="UPDATE_ID"):
        build_report(fixture([update, update]))


def test_delete_missing_or_scored_fails_closed():
    with pytest.raises(ValueError, match="DELETE_INVALID"):
        build_report(fixture([{"candidate_id": "z", "operation": "DELETE", "score": None}]))
    with pytest.raises(ValueError, match="DELETE_INVALID"):
        build_report(fixture([{"candidate_id": "a", "operation": "DELETE", "score": "1"}]))


@pytest.mark.parametrize("score", ["NaN", "Infinity", "", " 1"])
def test_invalid_score_fails_closed(score):
    with pytest.raises(ValueError, match="SCORE_INVALID"):
        build_report(fixture([{"candidate_id": "d", "operation": "UPSERT", "score": score}]))


def test_cannot_delete_all_candidates():
    payload = signed(
        {
            "schema": INPUT_SCHEMA,
            "candidates": {"a": "1"},
            "previous_ranking": [{"rank": 1, "candidate_id": "a", "score": "1"}],
            "updates": [{"candidate_id": "a", "operation": "DELETE", "score": None}],
        }
    )
    with pytest.raises(ValueError, match="EMPTY_RANKING"):
        build_report(payload)


def test_tampering_fails_closed():
    payload = fixture()
    payload["updates"][0]["score"] = "99"
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
        Path(__file__).parents[1] / "scripts/local/phase4dk_incremental_ranking_update.py"
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
