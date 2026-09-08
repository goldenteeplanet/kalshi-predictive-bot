from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

SCRIPT = Path(__file__).parents[1] / "scripts/local/phase4ey_residual_execution_risk_review.py"
SPEC = importlib.util.spec_from_file_location("phase4ey", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _seal(value):
    value.pop("artifact_hash", None)
    value["artifact_hash"] = canonical_hash(value)
    return value


def path(path_id):
    definition = MODULE.REQUIRED_PATHS[path_id]
    return {
        "path_id": path_id,
        "capability": definition["capability"],
        "current_state": "ABSENT_OR_DISABLED",
        "activation_prerequisites": sorted(definition["prerequisites"]),
        "control_owner": "human-operator",
        "evidence_hash": ("abcdef"[len(path_id) % 6]) * 64,
    }


def payload():
    return _seal(
        {
            "schema": MODULE.INPUT_SCHEMA,
            "review_scope_hash": "a" * 64,
            "paths": [path(path_id) for path_id in MODULE.REQUIRED_PATHS],
        }
    )


def test_complete_residual_inventory_passes_review():
    report = MODULE.build_report(payload())
    assert report["required_path_ids"] == sorted(MODULE.REQUIRED_PATHS)
    assert report["required_capabilities"] == sorted(MODULE.REQUIRED_CAPABILITIES)
    assert report["residual_path_count"] == 5
    assert report["all_residual_paths_documented"] is True
    assert report["review_complete"] is True
    assert all(
        item["risk_disposition"] == "DOCUMENTED_NOT_AUTHORIZED" for item in report["residual_paths"]
    )


@pytest.mark.parametrize("path_id", MODULE.REQUIRED_PATHS)
def test_each_required_path_has_exact_prerequisites(path_id):
    report = MODULE.build_report(payload())
    item = next(value for value in report["residual_paths"] if value["path_id"] == path_id)
    assert set(item["activation_prerequisites"]) == MODULE.REQUIRED_PATHS[path_id]["prerequisites"]


def test_missing_path_fails_closed():
    value = payload()
    value["paths"].pop()
    _seal(value)
    with pytest.raises(ValueError, match="PATH_SET"):
        MODULE.build_report(value)


def test_duplicate_path_fails_closed():
    value = payload()
    value["paths"][-1] = value["paths"][0]
    _seal(value)
    with pytest.raises(ValueError, match="PATH_ID"):
        MODULE.build_report(value)


def test_unknown_path_fails_closed():
    value = payload()
    value["paths"][0]["path_id"] = "unknown"
    _seal(value)
    with pytest.raises(ValueError, match="PATH_ID"):
        MODULE.build_report(value)


@pytest.mark.parametrize(
    "field",
    [
        "path_id",
        "capability",
        "current_state",
        "activation_prerequisites",
        "control_owner",
        "evidence_hash",
    ],
)
def test_missing_path_field_fails_closed(field):
    value = payload()
    value["paths"][0].pop(field)
    _seal(value)
    with pytest.raises(ValueError, match="PATH_FIELDS"):
        MODULE.build_report(value)


def test_wrong_capability_fails_closed():
    value = payload()
    value["paths"][0]["capability"] = "EXCHANGE_ACCESS"
    _seal(value)
    with pytest.raises(ValueError, match="CAPABILITY"):
        MODULE.build_report(value)


@pytest.mark.parametrize("state", ["ENABLED", "PRESENT", "AUTHORIZED", "UNKNOWN"])
def test_any_nondisabled_state_fails_closed(state):
    value = payload()
    value["paths"][0]["current_state"] = state
    _seal(value)
    with pytest.raises(ValueError, match="CURRENT_STATE_UNSAFE"):
        MODULE.build_report(value)


def test_missing_prerequisite_fails_closed():
    value = payload()
    value["paths"][0]["activation_prerequisites"].pop()
    _seal(value)
    with pytest.raises(ValueError, match="PREREQUISITES"):
        MODULE.build_report(value)


def test_extra_prerequisite_fails_closed():
    value = payload()
    value["paths"][0]["activation_prerequisites"].append("UNDECLARED")
    _seal(value)
    with pytest.raises(ValueError, match="PREREQUISITES"):
        MODULE.build_report(value)


def test_duplicate_prerequisite_fails_closed():
    value = payload()
    value["paths"][0]["activation_prerequisites"].append(
        value["paths"][0]["activation_prerequisites"][0]
    )
    _seal(value)
    with pytest.raises(ValueError, match="PREREQUISITES"):
        MODULE.build_report(value)


def test_empty_owner_fails_closed():
    value = payload()
    value["paths"][0]["control_owner"] = ""
    _seal(value)
    with pytest.raises(ValueError, match="CONTROL_OWNER"):
        MODULE.build_report(value)


def test_bad_evidence_hash_fails_closed():
    value = payload()
    value["paths"][0]["evidence_hash"] = "bad"
    _seal(value)
    with pytest.raises(ValueError, match="EVIDENCE_HASH"):
        MODULE.build_report(value)


def test_bad_scope_hash_fails_closed():
    value = payload()
    value["review_scope_hash"] = "bad"
    _seal(value)
    with pytest.raises(ValueError, match="SCOPE_HASH"):
        MODULE.build_report(value)


def test_input_tampering_fails_closed():
    value = payload()
    value["paths"][0]["control_owner"] = "changed"
    with pytest.raises(ValueError, match="SCHEMA_OR_HASH"):
        MODULE.build_report(value)


def test_input_order_is_normalized():
    first = MODULE.build_report(payload())
    value = payload()
    value["paths"].reverse()
    _seal(value)
    second = MODULE.build_report(value)
    assert first["residual_paths"] == second["residual_paths"]


def test_atomic_publication(tmp_path):
    report = MODULE.build_report(payload())
    output = tmp_path / "review.json"
    MODULE.publish(output, report)
    assert json.loads(output.read_text()) == report
    assert not list(tmp_path.glob(".*"))


def test_report_never_grants_capability_or_mutates():
    report = MODULE.build_report(payload())
    assert report["human_authorization_granted"] is False
    assert report["credentials_loaded"] is False
    assert report["writer_access_granted"] is False
    assert report["exchange_access_granted"] is False
    assert report["paper_order_creation_enabled"] is False
    assert report["paper_order_creation_authorized"] is False
    assert report["paper_orders_created"] == 0
    assert report["production_database_mutated"] is False
    assert report["services_controlled"] is False
    assert report["exchange_requests_made"] is False
    assert report["execution_authorized"] is False


def test_source_has_no_connected_or_mutating_surface():
    source = SCRIPT.read_text()
    for token in (
        "import sqlite3",
        "import requests",
        "import subprocess",
        "systemctl ",
        "exchange_client.",
        "create_order(",
        "insert_order(",
        "/home/james",
    ):
        assert token not in source
