import json
from copy import deepcopy
from pathlib import Path

import pytest
from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

from scripts.local.phase4dv_forecast_ranking_handoff_contract import (
    INPUT_SCHEMA,
    build_report,
    publish,
)

H1, H2, H3, H4 = "1" * 64, "2" * 64, "3" * 64, "4" * 64


def signed(payload):
    payload.pop("artifact_hash", None)
    payload["artifact_hash"] = canonical_hash(payload)
    return payload


def candidate(identifier, market, forecast, market_probability, digest):
    return signed(
        {
            "candidate_id": identifier,
            "market_id": market,
            "forecast_probability": forecast,
            "market_probability": market_probability,
            "forecast_hash": digest,
        }
    )


def fixture(candidates=None):
    return signed(
        {
            "schema": INPUT_SCHEMA,
            "batch_id": "batch-1",
            "generated_at": "2026-08-26T00:00:00Z",
            "ranking_deadline": "2026-08-26T00:01:00Z",
            "model_hash": H1,
            "evidence_hash": H2,
            "candidates": candidates
            or [candidate("b", "m2", "0.70", "0.4", H3), candidate("a", "m1", "0.3", "0.2", H4)],
        }
    )


def test_minimal_handoff_is_lossless_and_canonical():
    report = build_report(fixture())
    assert report["lossless_for_ranking"] is True
    assert report["canonical_source_view_hash"] == report["reconstructed_view_hash"]
    assert [row["candidate_id"] for row in report["candidates"]] == ["a", "b"]
    assert report["candidates"][1]["forecast_probability"] == "0.7"
    assert report["source_parse_count"] == 1
    assert report["repeated_lineage_fields_per_candidate"] == 0


def test_input_order_does_not_change_handoff_candidates():
    payload = fixture()
    reverse = fixture(list(reversed(payload["candidates"])))
    assert build_report(payload)["candidates"] == build_report(reverse)["candidates"]


def test_exact_generated_deadline_boundary_is_allowed():
    payload = fixture()
    payload["ranking_deadline"] = payload["generated_at"]
    signed(payload)
    assert build_report(payload)["lineage"]["ranking_deadline"] == payload["generated_at"]


def test_deadline_before_generation_fails_closed():
    payload = fixture()
    payload["ranking_deadline"] = "2026-08-25T23:59:59.999999Z"
    signed(payload)
    with pytest.raises(ValueError, match="DEADLINE"):
        build_report(payload)


@pytest.mark.parametrize(
    "field,value",
    [
        ("forecast_probability", "-0.1"),
        ("forecast_probability", "1.1"),
        ("market_probability", "NaN"),
        ("market_probability", " 0.2"),
    ],
)
def test_invalid_probability_fails_closed(field, value):
    item = candidate("a", "m1", "0.3", "0.2", H3)
    item[field] = value
    signed(item)
    with pytest.raises(ValueError, match="PROBABILITY"):
        build_report(fixture([item]))


def test_candidate_tampering_fails_closed():
    item = candidate("a", "m1", "0.3", "0.2", H3)
    item["market_id"] = "changed"
    with pytest.raises(ValueError, match="CANDIDATE_HASH"):
        build_report(fixture([item]))


def test_extra_redundant_field_is_ambiguous_and_fails_closed():
    item = candidate("a", "m1", "0.3", "0.2", H3)
    item["model_hash"] = H1
    signed(item)
    with pytest.raises(ValueError, match="CANDIDATE_FIELDS"):
        build_report(fixture([item]))


def test_duplicate_candidate_fails_closed():
    with pytest.raises(ValueError, match="CANDIDATE_IDENTITY"):
        build_report(
            fixture(
                [candidate("a", "m1", "0.3", "0.2", H3), candidate("a", "m2", "0.4", "0.3", H4)]
            )
        )


def test_outer_tampering_fails_closed():
    payload = fixture()
    payload["batch_id"] = "changed"
    with pytest.raises(ValueError, match="SCHEMA_OR_HASH"):
        build_report(payload)


def test_deterministic_and_nonmutating():
    payload = fixture()
    before = deepcopy(payload)
    assert build_report(payload) == build_report(payload)
    assert payload == before


def test_atomic_publication(tmp_path):
    output = tmp_path / "nested" / "handoff.json"
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
        Path(__file__).parents[1] / "scripts/local/phase4dv_forecast_ranking_handoff_contract.py"
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
