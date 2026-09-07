import json
from pathlib import Path

import pytest

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash
from scripts.local.phase4dg_forecast_cache_poisoning_review import (
    ENTRY_SCHEMA,
    INPUT_SCHEMA,
    build_report,
    publish,
)

H1, H2, H3, H4 = "1" * 64, "2" * 64, "3" * 64, "4" * 64


def signed(payload):
    payload.pop("artifact_hash", None)
    payload["artifact_hash"] = canonical_hash(payload)
    return payload


def entry(entry_id="entry-1"):
    identity = {
        "schema": ENTRY_SCHEMA,
        "market_id": "market-1",
        "ticker": "TICKER",
        "model_id": "model-1",
        "model_hash": H1,
        "feature_set_hash": H2,
        "evidence_hash": H3,
        "result_hash": H4,
    }
    return signed(
        {
            **identity,
            "entry_id": entry_id,
            "cache_key": canonical_hash(identity),
            "evidence_observed_at": "2026-08-26T00:05:00Z",
            "expires_at": "2026-08-26T00:20:00Z",
        }
    )


def fixture(entries=None):
    return signed(
        {
            "schema": INPUT_SCHEMA,
            "expected": {
                "market_id": "market-1",
                "ticker": "TICKER",
                "model_id": "model-1",
                "model_hash": H1,
                "minimum_evidence_at": "2026-08-26T00:05:00Z",
            },
            "evaluated_at": "2026-08-26T00:10:00Z",
            "entries": [entry()] if entries is None else entries,
        }
    )


def resign_entry(item):
    identity = {
        key: item[key]
        for key in (
            "schema",
            "market_id",
            "ticker",
            "model_id",
            "model_hash",
            "feature_set_hash",
            "evidence_hash",
            "result_hash",
        )
    }
    item["cache_key"] = canonical_hash(identity)
    return signed(item)


def test_valid_entry_is_safe_without_consumption():
    report = build_report(fixture())
    assert report["status"] == "SAFE"
    assert report["cache_entries_consumed"] == 0


@pytest.mark.parametrize(
    "field,value,reason",
    [
        ("model_id", "wrong", "MODEL_ID_MISMATCH"),
        ("model_hash", "9" * 64, "MODEL_HASH_MISMATCH"),
        ("ticker", "WRONG", "TICKER_MISMATCH"),
        ("market_id", "wrong", "MARKET_ID_MISMATCH"),
    ],
)
def test_identity_substitution_detected(field, value, reason):
    item = entry()
    item[field] = value
    resign_entry(item)
    report = build_report(fixture([item]))
    assert report["status"] == "UNSAFE"
    assert reason in report["entry_findings"][0]["reasons"]


def test_stale_and_expired_entries_detected():
    item = entry()
    item["evidence_observed_at"] = "2026-08-26T00:04:59Z"
    item["expires_at"] = "2026-08-26T00:09:59Z"
    signed(item)
    reasons = build_report(fixture([item]))["entry_findings"][0]["reasons"]
    assert {"STALE_EVIDENCE", "EXPIRED_ENTRY"} <= set(reasons)


def test_schema_drift_detected():
    item = entry()
    item["schema"] = "phase4dg.cache-entry.v2"
    resign_entry(item)
    assert "SCHEMA_DRIFT" in build_report(fixture([item]))["entry_findings"][0]["reasons"]


def test_content_address_mismatch_detected():
    item = entry()
    item["cache_key"] = "9" * 64
    signed(item)
    assert (
        "CONTENT_ADDRESS_MISMATCH" in build_report(fixture([item]))["entry_findings"][0]["reasons"]
    )


def test_same_claimed_key_with_different_content_detects_collision_or_alias():
    first = entry("first")
    second = entry("second")
    second["result_hash"] = "9" * 64
    signed(second)
    report = build_report(fixture([first, second]))
    assert "CACHE_KEY_COLLISION_OR_ALIAS" in report["entry_findings"][1]["reasons"]


def test_artifact_tampering_detected_not_trusted():
    item = entry()
    item["result_hash"] = "9" * 64
    reasons = build_report(fixture([item]))["entry_findings"][0]["reasons"]
    assert "ARTIFACT_HASH_MISMATCH" in reasons
    assert "CONTENT_ADDRESS_MISMATCH" in reasons


def test_duplicate_entry_id_fails_closed():
    with pytest.raises(ValueError, match="ENTRY_ID"):
        build_report(fixture([entry(), entry()]))


def test_input_tampering_fails_closed():
    payload = fixture()
    payload["evaluated_at"] = "2027-01-01T00:00:00Z"
    with pytest.raises(ValueError, match="SCHEMA_OR_HASH"):
        build_report(payload)


def test_deterministic_report_and_atomic_publication(tmp_path):
    report = build_report(fixture())
    assert report == build_report(fixture())
    output = tmp_path / "nested" / "report.json"
    publish(output, report)
    assert json.loads(output.read_text()) == report
    assert not list(output.parent.glob(".*"))


def test_non_executable_and_read_only():
    report = build_report(fixture())
    assert report["cache_entries_written"] == 0
    assert report["forecast_records_created"] == 0
    assert report["execution_authorized"] is False


def test_source_has_no_connected_or_trading_surface():
    source = (
        Path(__file__).parents[1] / "scripts/local/phase4dg_forecast_cache_poisoning_review.py"
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
