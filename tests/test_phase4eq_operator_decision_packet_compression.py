from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

SCRIPT = (
    Path(__file__).parents[1] / "scripts/local/phase4eq_operator_decision_packet_compression.py"
)
SPEC = importlib.util.spec_from_file_location("phase4eq", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _seal(value):
    value.pop("artifact_hash", None)
    value["artifact_hash"] = canonical_hash(value)
    return value


def payload():
    provenance = [
        {"role": "risk", "artifact_hash": "b" * 64, "locator": "artifact://risk/7"},
        {"role": "forecast", "artifact_hash": "a" * 64, "locator": "artifact://forecast/9"},
        {"role": "ranking", "artifact_hash": "c" * 64, "locator": "artifact://ranking/4"},
    ]
    value = {
        "schema": MODULE.INPUT_SCHEMA,
        "candidate_id": "candidate-7",
        "decision": {
            "market_ticker": "KXTEST-26AUG",
            "side": "YES",
            "quantity": 1,
            "limit_price_cents": 47,
            "expected_edge_bps": 325,
            "expires_at_utc": "2026-08-26T12:00:00Z",
            "eligible": True,
            "reason_codes": [],
            "binding_cap": "single_contract",
            "operator_review_required": True,
        },
        "provenance": provenance,
        "provenance_manifest_hash": canonical_hash(
            sorted(provenance, key=lambda item: item["role"])
        ),
    }
    return _seal(value)


def test_builds_minimal_packet_with_full_provenance_hashes():
    report = MODULE.build_report(payload())
    assert report["decision_critical"]["quantity"] == 1
    assert report["full_provenance"]["artifact_count"] == 3
    assert [item["role"] for item in report["full_provenance"]["artifacts"]] == [
        "forecast",
        "ranking",
        "risk",
    ]
    assert report["packet_field_count"] == 10
    assert report["artifact_hash"] == canonical_hash(
        {key: value for key, value in report.items() if key != "artifact_hash"}
    )


def test_ineligible_packet_is_allowed_only_at_zero_quantity():
    value = payload()
    value["decision"].update(eligible=False, quantity=0, reason_codes=["RISK_BLOCK", "STALE"])
    _seal(value)
    report = MODULE.build_report(value)
    assert report["decision_critical"]["reason_codes"] == ["RISK_BLOCK", "STALE"]


@pytest.mark.parametrize(
    "field", ["candidate_id", "decision", "provenance", "provenance_manifest_hash"]
)
def test_missing_input_field_fails_closed(field):
    value = payload()
    value.pop(field)
    _seal(value)
    with pytest.raises(ValueError, match="INPUT_FIELDS"):
        MODULE.build_report(value)


@pytest.mark.parametrize(
    ("field", "bad", "code"),
    [
        ("side", "BUY", "SIDE"),
        ("quantity", -1, "QUANTITY"),
        ("quantity", True, "QUANTITY"),
        ("limit_price_cents", 0, "PRICE"),
        ("limit_price_cents", 100, "PRICE"),
        ("expected_edge_bps", 10001, "EDGE"),
        ("expires_at_utc", "2026-08-26T12:00:00+00:00", "EXPIRATION"),
        ("eligible", "yes", "STATUS"),
        ("operator_review_required", 1, "STATUS"),
        ("binding_cap", "", "BINDING_CAP"),
    ],
)
def test_invalid_decision_fields_fail_closed(field, bad, code):
    value = payload()
    value["decision"][field] = bad
    _seal(value)
    with pytest.raises(ValueError, match=code):
        MODULE.build_report(value)


def test_eligibility_quantity_mismatch_fails_closed():
    value = payload()
    value["decision"]["quantity"] = 0
    _seal(value)
    with pytest.raises(ValueError, match="INCONSISTENT"):
        MODULE.build_report(value)


@pytest.mark.parametrize("reasons", [["DUP", "DUP"], [""], "BAD"])
def test_bad_reason_codes_fail_closed(reasons):
    value = payload()
    value["decision"]["reason_codes"] = reasons
    _seal(value)
    with pytest.raises(ValueError, match="REASON_CODES"):
        MODULE.build_report(value)


def test_duplicate_provenance_role_fails_closed():
    value = payload()
    value["provenance"][1]["role"] = "risk"
    value["provenance_manifest_hash"] = canonical_hash(value["provenance"])
    _seal(value)
    with pytest.raises(ValueError, match="ROLE_DUPLICATE"):
        MODULE.build_report(value)


@pytest.mark.parametrize("field", ["role", "artifact_hash", "locator"])
def test_missing_provenance_field_fails_closed(field):
    value = payload()
    value["provenance"][0].pop(field)
    _seal(value)
    with pytest.raises(ValueError, match="PROVENANCE_FIELDS"):
        MODULE.build_report(value)


def test_bad_provenance_digest_fails_closed():
    value = payload()
    value["provenance"][0]["artifact_hash"] = "bad"
    _seal(value)
    with pytest.raises(ValueError, match="PROVENANCE_HASH"):
        MODULE.build_report(value)


def test_manifest_mismatch_fails_closed():
    value = payload()
    value["provenance_manifest_hash"] = "f" * 64
    _seal(value)
    with pytest.raises(ValueError, match="MANIFEST_HASH_MISMATCH"):
        MODULE.build_report(value)


def test_input_order_is_irrelevant():
    first = payload()
    second = payload()
    second["provenance"].reverse()
    second["provenance_manifest_hash"] = canonical_hash(
        sorted(second["provenance"], key=lambda item: item["role"])
    )
    _seal(second)
    left = MODULE.build_report(first)
    right = MODULE.build_report(second)
    assert left["decision_critical"] == right["decision_critical"]
    assert left["full_provenance"] == right["full_provenance"]


def test_input_tampering_fails_closed():
    value = payload()
    value["decision"]["quantity"] = 2
    with pytest.raises(ValueError, match="SCHEMA_OR_HASH"):
        MODULE.build_report(value)


def test_atomic_publication(tmp_path):
    report = MODULE.build_report(payload())
    output = tmp_path / "packet.json"
    MODULE.publish(output, report)
    assert json.loads(output.read_text()) == report
    assert not list(tmp_path.glob(".*"))


def test_report_never_authorizes_or_mutates():
    report = MODULE.build_report(payload())
    assert report["operator_authorization_recorded"] is False
    assert report["paper_order_creation_authorized"] is False
    assert report["paper_orders_created"] == 0
    assert report["execution_authorized"] is False
    assert report["production_records_created"] == 0


def test_source_has_no_connected_or_mutating_surface():
    source = SCRIPT.read_text()
    for token in (
        "sqlite3",
        "requests",
        "subprocess",
        "systemctl",
        "exchange_client",
        "create_order",
        "insert_order",
        "/home/james",
    ):
        assert token not in source
