import json
from copy import deepcopy
from pathlib import Path

import pytest

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash
from scripts.local.phase4dj_ranking_dependency_map import INPUT_SCHEMA, build_report, publish

H1, H2 = "1" * 64, "2" * 64


def signed(payload):
    payload.pop("artifact_hash", None)
    payload["artifact_hash"] = canonical_hash(payload)
    return payload


def dependency(identifier, digest, observed="2026-08-26T00:00:00Z", max_age=600):
    return {
        "input_id": identifier,
        "artifact_hash": digest,
        "observed_at": observed,
        "max_age_seconds": max_age,
        "invalidation_triggers": [
            "SOURCE_HASH_CHANGED",
            "SOURCE_STALE",
            f"{identifier.upper()}_CHANGED",
        ],
    }


def fixture():
    return signed(
        {
            "schema": INPUT_SCHEMA,
            "evaluated_at": "2026-08-26T00:10:00Z",
            "inputs": [dependency("forecast", H1), dependency("market", H2)],
            "ranking": {
                "ranking_id": "ranking-v1",
                "score_dependencies": ["forecast", "market"],
                "sort_keys": [
                    {"field": "score", "direction": "DESC", "nulls": "FORBIDDEN"},
                    {"field": "candidate_id", "direction": "ASC", "nulls": "FORBIDDEN"},
                ],
                "stable_sort_required": True,
            },
        }
    )


def test_dependency_map_ready_at_inclusive_freshness_boundary():
    report = build_report(fixture())
    assert report["disposition"] == "READY"
    assert report["stale_input_ids"] == []
    assert report["tie_breaker"]["field"] == "candidate_id"
    assert "RANKING_SPEC_CHANGED" in report["invalidation_triggers"]


def test_one_second_beyond_freshness_refuses():
    payload = fixture()
    payload["inputs"][0]["max_age_seconds"] = 599
    signed(payload)
    report = build_report(payload)
    assert report["disposition"] == "REFUSE_STALE_INPUT"
    assert report["stale_input_ids"] == ["forecast"]


@pytest.mark.parametrize(
    "mutation,error",
    [
        (lambda p: p["ranking"].update({"stable_sort_required": False}), "STABLE_SORT"),
        (lambda p: p["ranking"]["sort_keys"].pop(), "TIE_BREAKER"),
        (lambda p: p["ranking"]["sort_keys"][-1].update({"direction": "DESC"}), "TIE_BREAKER"),
        (lambda p: p["ranking"].update({"score_dependencies": ["forecast"]}), "SCORE_DEPENDENCIES"),
        (lambda p: p["inputs"][0]["invalidation_triggers"].remove("SOURCE_STALE"), "INVALIDATION"),
    ],
)
def test_incomplete_ranking_contract_fails_closed(mutation, error):
    payload = fixture()
    mutation(payload)
    signed(payload)
    with pytest.raises(ValueError, match=error):
        build_report(payload)


def test_duplicate_input_and_sort_fields_fail_closed():
    payload = fixture()
    payload["inputs"][1]["input_id"] = "forecast"
    signed(payload)
    with pytest.raises(ValueError, match="DEPENDENCY_ID"):
        build_report(payload)
    payload = fixture()
    payload["ranking"]["sort_keys"][1]["field"] = "score"
    signed(payload)
    with pytest.raises(ValueError, match="SORT_KEY"):
        build_report(payload)


def test_future_input_fails_closed():
    payload = fixture()
    payload["inputs"][0]["observed_at"] = "2026-08-26T00:10:01Z"
    signed(payload)
    with pytest.raises(ValueError, match="FRESHNESS_POLICY"):
        build_report(payload)


def test_tampering_fails_closed():
    payload = fixture()
    payload["ranking"]["ranking_id"] = "changed"
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
        Path(__file__).parents[1] / "scripts/local/phase4dj_ranking_dependency_map.py"
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
