import json
from copy import deepcopy
from pathlib import Path

import pytest
from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

from scripts.local.phase4dx_forecast_pipeline_differential_replay import (
    INPUT_SCHEMA,
    build_report,
    publish,
)

H1 = "1" * 64


def signed(payload):
    payload.pop("artifact_hash", None)
    payload["artifact_hash"] = canonical_hash(payload)
    return payload


def output(category):
    if category in {"STALE", "REFUSAL"}:
        return {"status": "REFUSED", "results": [], "reasons": [category]}
    return {
        "status": "SUCCESS",
        "results": [{"candidate_id": category.lower(), "score": "1"}],
        "reasons": [],
    }


def fixture(fixtures=None):
    if fixtures is None:
        fixtures = [
            {
                "fixture_id": category.lower(),
                "category": category,
                "input_hash": H1,
                "legacy_output": output(category),
                "optimized_output": output(category),
                "legacy_work_units": 100,
                "optimized_work_units": 60,
            }
            for category in ("NORMAL", "BOUNDARY", "TIE", "STALE", "REFUSAL")
        ]
    return signed({"schema": INPUT_SCHEMA, "fixtures": fixtures})


def test_representative_corpus_has_exact_equivalence_and_savings():
    report = build_report(fixture())
    assert report["status"] == "PASS"
    assert report["exact_logical_equivalence"] is True
    assert report["covered_categories"] == ["BOUNDARY", "NORMAL", "REFUSAL", "STALE", "TIE"]
    assert report["work_units_saved"] == 200


def test_one_logical_difference_fails_gate():
    payload = fixture()
    payload["fixtures"][0]["optimized_output"]["results"][0]["score"] = "2"
    signed(payload)
    report = build_report(payload)
    assert report["status"] == "FAIL"
    assert report["mismatched_fixture_ids"] == ["normal"]


def test_reason_order_is_logically_significant():
    payload = fixture()
    stale = next(item for item in payload["fixtures"] if item["category"] == "STALE")
    stale["legacy_output"]["reasons"] = ["STALE", "OTHER"]
    stale["optimized_output"]["reasons"] = ["OTHER", "STALE"]
    signed(payload)
    assert build_report(payload)["status"] == "FAIL"


def test_missing_representative_category_fails_closed():
    payload = fixture()
    payload["fixtures"].pop()
    signed(payload)
    with pytest.raises(ValueError, match="CORPUS_INCOMPLETE"):
        build_report(payload)


def test_duplicate_fixture_and_unknown_category_fail_closed():
    payload = fixture()
    payload["fixtures"][1]["fixture_id"] = payload["fixtures"][0]["fixture_id"]
    signed(payload)
    with pytest.raises(ValueError, match="FIXTURE_ID"):
        build_report(payload)
    payload = fixture()
    payload["fixtures"][0]["category"] = "OTHER"
    signed(payload)
    with pytest.raises(ValueError, match="CATEGORY"):
        build_report(payload)


def test_malformed_refusal_and_success_fail_closed():
    payload = fixture()
    payload["fixtures"][0]["legacy_output"]["reasons"] = ["unexpected"]
    signed(payload)
    with pytest.raises(ValueError, match="SUCCESS_REASONS"):
        build_report(payload)
    payload = fixture()
    refusal = next(item for item in payload["fixtures"] if item["category"] == "REFUSAL")
    refusal["legacy_output"]["reasons"] = []
    signed(payload)
    with pytest.raises(ValueError, match="REFUSAL_OUTPUT"):
        build_report(payload)


@pytest.mark.parametrize("work", [-1, True])
def test_invalid_work_units_fail_closed(work):
    payload = fixture()
    payload["fixtures"][0]["optimized_work_units"] = work
    signed(payload)
    with pytest.raises(ValueError, match="WORK_UNITS"):
        build_report(payload)


def test_input_order_does_not_change_sorted_results():
    payload = fixture()
    reverse = fixture(list(reversed(payload["fixtures"])))
    assert build_report(payload)["fixture_results"] == build_report(reverse)["fixture_results"]


def test_tampering_fails_closed():
    payload = fixture()
    payload["fixtures"][0]["legacy_work_units"] = 1
    with pytest.raises(ValueError, match="SCHEMA_OR_HASH"):
        build_report(payload)


def test_deterministic_and_nonmutating():
    payload = fixture()
    before = deepcopy(payload)
    assert build_report(payload) == build_report(payload)
    assert payload == before


def test_atomic_publication(tmp_path):
    output_path = tmp_path / "nested" / "report.json"
    report = build_report(fixture())
    publish(output_path, report)
    assert json.loads(output_path.read_text()) == report
    assert not list(output_path.parent.glob(".*"))


def test_non_executable_and_no_wall_clock_gate():
    report = build_report(fixture())
    assert report["wall_clock_used_as_gate"] is False
    assert report["forecast_records_created"] == 0
    assert report["execution_authorized"] is False


def test_source_has_no_connected_or_trading_surface():
    source = (
        Path(__file__).parents[1]
        / "scripts/local/phase4dx_forecast_pipeline_differential_replay.py"
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
