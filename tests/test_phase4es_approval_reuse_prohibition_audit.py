from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

import pytest

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

SCRIPT = Path(__file__).parents[1] / "scripts/local/phase4es_approval_reuse_prohibition_audit.py"
SPEC = importlib.util.spec_from_file_location("phase4es", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _seal(value):
    value.pop("artifact_hash", None)
    value["artifact_hash"] = canonical_hash(value)
    return value


def terms():
    return {
        "database_identity_hash": "a" * 64,
        "forecast_hash": "b" * 64,
        "limit_price_cents": 47,
        "quantity": 1,
        "risk_hash": "c" * 64,
        "expires_at_utc": "2026-08-26T12:10:00.000Z",
    }


def changed(base, dimension):
    result = copy.deepcopy(base)
    replacements = {
        "database_identity_hash": "d" * 64,
        "forecast_hash": "e" * 64,
        "limit_price_cents": 48,
        "quantity": 2,
        "risk_hash": "f" * 64,
        "expires_at_utc": "2026-08-26T12:11:00.000Z",
    }
    result[dimension] = replacements[dimension]
    return result


def payload():
    baseline = terms()
    approval = _seal(
        {
            "approval_id": "approval-synthetic-1",
            "terms": baseline,
            "binding_hash": MODULE.binding_hash(baseline),
        }
    )
    return _seal(
        {
            "schema": MODULE.INPUT_SCHEMA,
            "approval": approval,
            "probes": [
                {"dimension": dimension, "candidate_terms": changed(baseline, dimension)}
                for dimension in MODULE.DIMENSIONS
            ],
        }
    )


def test_all_required_dimensions_prohibit_reuse():
    report = MODULE.build_report(payload())
    assert report["covered_dimensions"] == list(MODULE.DIMENSIONS)
    assert report["exact_replay_matches"] is True
    assert report["all_changes_prohibit_reuse"] is True
    assert all(item["binding_changed"] for item in report["probe_results"])
    assert all(item["reuse_allowed"] is False for item in report["probe_results"])


@pytest.mark.parametrize("dimension", MODULE.DIMENSIONS)
def test_each_changed_dimension_changes_binding_and_refuses_reuse(dimension):
    baseline = terms()
    approval = {"binding_hash": MODULE.binding_hash(baseline), "terms": baseline}
    candidate = changed(baseline, dimension)
    assert MODULE.binding_hash(candidate) != approval["binding_hash"]
    assert MODULE.approval_matches(approval, candidate) is False


def test_exact_terms_match_only_same_binding():
    baseline = terms()
    approval = {"binding_hash": MODULE.binding_hash(baseline), "terms": baseline}
    assert MODULE.approval_matches(approval, copy.deepcopy(baseline)) is True


def test_missing_probe_fails_closed():
    value = payload()
    value["probes"].pop()
    _seal(value)
    with pytest.raises(ValueError, match="PROBE_SET"):
        MODULE.build_report(value)


def test_duplicate_probe_dimension_fails_closed():
    value = payload()
    value["probes"][-1]["dimension"] = value["probes"][0]["dimension"]
    _seal(value)
    with pytest.raises(ValueError, match="PROBE_DIMENSION"):
        MODULE.build_report(value)


def test_unchanged_probe_fails_closed():
    value = payload()
    value["probes"][0]["candidate_terms"] = copy.deepcopy(value["approval"]["terms"])
    _seal(value)
    with pytest.raises(ValueError, match="MUTATION_SCOPE"):
        MODULE.build_report(value)


def test_probe_changing_two_dimensions_fails_closed():
    value = payload()
    probe = value["probes"][0]
    probe["candidate_terms"]["forecast_hash"] = "9" * 64
    _seal(value)
    with pytest.raises(ValueError, match="MUTATION_SCOPE"):
        MODULE.build_report(value)


@pytest.mark.parametrize("field", MODULE.DIMENSIONS)
def test_missing_approval_term_fails_closed(field):
    value = payload()
    value["approval"]["terms"].pop(field)
    _seal(value["approval"])
    _seal(value)
    with pytest.raises(ValueError, match="APPROVAL_TERMS_FIELDS"):
        MODULE.build_report(value)


@pytest.mark.parametrize(
    ("field", "bad", "code"),
    [
        ("database_identity_hash", "bad", "DATABASE"),
        ("forecast_hash", "bad", "FORECAST"),
        ("limit_price_cents", 0, "PRICE"),
        ("limit_price_cents", 100, "PRICE"),
        ("quantity", 0, "QUANTITY"),
        ("quantity", True, "QUANTITY"),
        ("risk_hash", "bad", "RISK"),
        ("expires_at_utc", "2026-08-26T12:10:00Z", "EXPIRATION"),
    ],
)
def test_invalid_approval_term_fails_closed(field, bad, code):
    value = payload()
    value["approval"]["terms"][field] = bad
    _seal(value["approval"])
    _seal(value)
    with pytest.raises(ValueError, match=code):
        MODULE.build_report(value)


def test_approval_binding_mismatch_fails_closed():
    value = payload()
    value["approval"]["binding_hash"] = "0" * 64
    _seal(value["approval"])
    _seal(value)
    with pytest.raises(ValueError, match="BINDING_MISMATCH"):
        MODULE.build_report(value)


def test_approval_artifact_tampering_fails_closed():
    value = payload()
    value["approval"]["approval_id"] = "changed"
    _seal(value)
    with pytest.raises(ValueError, match="APPROVAL_HASH"):
        MODULE.build_report(value)


def test_outer_input_tampering_fails_closed():
    value = payload()
    value["probes"][0]["candidate_terms"]["quantity"] = 3
    with pytest.raises(ValueError, match="SCHEMA_OR_HASH"):
        MODULE.build_report(value)


def test_probe_input_order_is_normalized():
    first = MODULE.build_report(payload())
    value = payload()
    value["probes"].reverse()
    _seal(value)
    second = MODULE.build_report(value)
    assert first["probe_results"] == second["probe_results"]


def test_atomic_publication(tmp_path):
    report = MODULE.build_report(payload())
    output = tmp_path / "audit.json"
    MODULE.publish(output, report)
    assert json.loads(output.read_text()) == report
    assert not list(tmp_path.glob(".*"))


def test_report_never_reuses_approval_or_authorizes():
    report = MODULE.build_report(payload())
    assert report["approval_reuse_authorized"] is False
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
