import json
from copy import deepcopy
from pathlib import Path

import pytest

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash
from scripts.local.phase4dm_ranking_drift_detector import INPUT_SCHEMA, build_report, publish

H1, H2, H3 = "1" * 64, "2" * 64, "3" * 64


def signed(payload):
    payload.pop("artifact_hash", None)
    payload["artifact_hash"] = canonical_hash(payload)
    return payload


def snapshot():
    return {
        "model_id": "model-1",
        "model_hash": H1,
        "dependency_hashes": {"forecast": H2},
        "arithmetic_mode": "DECIMAL",
        "sort_spec_hash": H3,
        "inputs_fresh": True,
        "ranking": [
            {"rank": 1, "candidate_id": "a", "score": "2"},
            {"rank": 2, "candidate_id": "b", "score": "1"},
        ],
    }


def fixture():
    return signed({"schema": INPUT_SCHEMA, "baseline": snapshot(), "current": snapshot()})


def drift(payload):
    payload["current"]["ranking"] = [
        {"rank": 1, "candidate_id": "b", "score": "3"},
        {"rank": 2, "candidate_id": "a", "score": "2"},
    ]


def test_identical_snapshots_have_no_drift():
    report = build_report(fixture())
    assert report["status"] == "NO_RANKING_DRIFT"
    assert report["safe_to_ignore"] is True


@pytest.mark.parametrize(
    "mutation,reason",
    [
        (lambda p: p["current"].update({"model_hash": "9" * 64}), "MODEL_IDENTITY_CHANGED"),
        (
            lambda p: p["current"]["dependency_hashes"].update({"forecast": "9" * 64}),
            "DEPENDENCY_CHANGED",
        ),
        (
            lambda p: p["current"].update({"arithmetic_mode": "BINARY_FLOAT"}),
            "ARITHMETIC_BEHAVIOR_CHANGED",
        ),
        (lambda p: p["current"].update({"sort_spec_hash": "9" * 64}), "ORDERING_SPEC_CHANGED"),
        (lambda p: p["current"].update({"inputs_fresh": False}), "STALE_INPUT_STATE"),
    ],
)
def test_drift_is_attributed_to_explicit_cause(mutation, reason):
    payload = fixture()
    drift(payload)
    mutation(payload)
    signed(payload)
    report = build_report(payload)
    assert report["status"] == "ATTRIBUTED_DRIFT"
    assert reason in report["reasons"]
    assert report["safe_to_ignore"] is False


def test_unexplained_ranking_difference_is_flagged():
    payload = fixture()
    drift(payload)
    signed(payload)
    report = build_report(payload)
    assert report["status"] == "UNATTRIBUTED_DRIFT"
    assert report["reasons"] == ["UNATTRIBUTED_RANKING_DRIFT"]


def test_metadata_change_without_output_drift_is_not_hidden():
    payload = fixture()
    payload["current"]["model_hash"] = "9" * 64
    signed(payload)
    report = build_report(payload)
    assert report["status"] == "NO_RANKING_DRIFT"
    assert report["reasons"] == ["MODEL_IDENTITY_CHANGED"]


def test_invalid_rank_sequence_fails_closed():
    payload = fixture()
    payload["current"]["ranking"][0]["rank"] = 2
    signed(payload)
    with pytest.raises(ValueError, match="RANK_SEQUENCE"):
        build_report(payload)


def test_duplicate_candidate_fails_closed():
    payload = fixture()
    payload["current"]["ranking"][1]["candidate_id"] = "a"
    signed(payload)
    with pytest.raises(ValueError, match="CANDIDATE_ID"):
        build_report(payload)


def test_tampering_fails_closed():
    payload = fixture()
    payload["current"]["inputs_fresh"] = False
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
        Path(__file__).parents[1] / "scripts/local/phase4dm_ranking_drift_detector.py"
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
