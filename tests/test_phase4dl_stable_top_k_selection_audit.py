import json
from copy import deepcopy
from pathlib import Path

import pytest
from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

from scripts.local.phase4dl_stable_top_k_selection_audit import INPUT_SCHEMA, build_report, publish


def signed(payload):
    payload.pop("artifact_hash", None)
    payload["artifact_hash"] = canonical_hash(payload)
    return payload


def fixture(k=3, candidates=None):
    return signed(
        {
            "schema": INPUT_SCHEMA,
            "k": k,
            "candidates": candidates
            or [
                {"candidate_id": "d", "score": "1"},
                {"candidate_id": "c", "score": "8"},
                {"candidate_id": "a", "score": "10"},
                {"candidate_id": "b", "score": "8.0"},
                {"candidate_id": "e", "score": "-1"},
            ],
        }
    )


@pytest.mark.parametrize("k", [1, 2, 3, 5])
def test_bounded_top_k_matches_full_sort(k):
    report = build_report(fixture(k))
    assert report["full_equivalence"] is True
    assert report["baseline_hash"] == report["bounded_hash"]
    assert [row["candidate_id"] for row in report["selected"]] == list("abcde")[:k]
    assert report["bounded_comparison_units"] <= report["comparison_unit_upper_bound"]


def test_exact_tie_boundary_uses_candidate_id():
    candidates = [{"candidate_id": value, "score": "1"} for value in reversed("abcde")]
    report = build_report(fixture(2, candidates))
    assert [row["candidate_id"] for row in report["selected"]] == ["a", "b"]


def test_decimal_precision_changes_selection_exactly():
    candidates = [
        {"candidate_id": "a", "score": "1.0000000000000000000"},
        {"candidate_id": "b", "score": "1.0000000000000000001"},
    ]
    assert build_report(fixture(1, candidates))["selected"][0]["candidate_id"] == "b"


@pytest.mark.parametrize("k", [0, -1, 6, True])
def test_invalid_k_fails_closed(k):
    with pytest.raises(ValueError, match="K_INVALID"):
        build_report(fixture(k))


@pytest.mark.parametrize("score", ["NaN", "Infinity", "", " 1"])
def test_invalid_score_fails_closed(score):
    with pytest.raises(ValueError, match="SCORE_INVALID"):
        build_report(fixture(1, [{"candidate_id": "a", "score": score}]))


def test_duplicate_candidate_fails_closed():
    candidates = [{"candidate_id": "a", "score": "1"}, {"candidate_id": "a", "score": "2"}]
    with pytest.raises(ValueError, match="CANDIDATE_ID"):
        build_report(fixture(1, candidates))


def test_input_order_does_not_change_selected_order():
    payload = fixture()
    reverse = fixture(candidates=list(reversed(payload["candidates"])))
    assert build_report(payload)["selected"] == build_report(reverse)["selected"]


def test_tampering_fails_closed():
    payload = fixture()
    payload["k"] = 1
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
    assert report["ranking_records_created"] == 0
    assert report["execution_authorized"] is False


def test_source_has_no_connected_or_trading_surface():
    source = (
        Path(__file__).parents[1] / "scripts/local/phase4dl_stable_top_k_selection_audit.py"
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
