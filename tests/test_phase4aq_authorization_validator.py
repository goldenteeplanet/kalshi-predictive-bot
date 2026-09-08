from __future__ import annotations

import importlib.util
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest


def _module():
    path = Path(__file__).parents[1] / "scripts/local/phase4aq_authorization_validator.py"
    spec = importlib.util.spec_from_file_location("phase4aq_tested", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


NOW = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)
BUILD_HASH = "b" * 64


def _fixture(tmp_path: Path):
    module = _module()
    tmp_path.mkdir(parents=True, exist_ok=True)
    rows = [{"ticker": "KXAUTH-1", "readiness_row_hash": "a" * 64}]
    readiness = {
        "schema": module.AK_SCHEMA,
        "readiness_state": "READY_FOR_SEPARATE_EXECUTOR_DESIGN",
        "production_database_identity": {"path": "/protected/production.db", "size": 1},
        "approval_expires_at": (NOW + timedelta(hours=1)).isoformat(),
        "rows": rows,
    }
    readiness["artifact_hash"] = module._hash(readiness)
    simulation = {
        "schema": module.AL_SCHEMA,
        "simulation_outcome": "SIMULATION_COMMITTED",
        "input_hashes": {"phase4ak_readiness": readiness["artifact_hash"]},
    }
    simulation["artifact_hash"] = module._hash(simulation)
    authorization = {
        "schema": module.AUTH_SCHEMA,
        "externally_supplied": True,
        "generated_by_guarded_tooling": False,
        "attempt_id": "attempt-001",
        "operator_identity": "operator-a",
        "second_reviewer_identity": None,
        "readiness_envelope_hash": readiness["artifact_hash"],
        "readiness_row_hashes": ["a" * 64],
        "simulation_report_hash": simulation["artifact_hash"],
        "production_database_identity_hash": module.canonical_hash(
            readiness["production_database_identity"]
        ),
        "executor_build_identity_hash": BUILD_HASH,
        "expires_at": (NOW + timedelta(minutes=30)).isoformat(),
        "revoked": False,
        "revoked_at": None,
        "scope": "ONE_DISPOSABLE_SIMULATION_ATTEMPT",
        "exchange_authorized": False,
        "orders_authorized": False,
        "paper_orders_authorized": False,
        "production_execution_authorized": False,
    }
    authorization["artifact_hash"] = module._hash(authorization)
    paths = {}
    for name, payload in (
        ("authorization", authorization),
        ("readiness", readiness),
        ("simulation", simulation),
    ):
        paths[name] = tmp_path / f"{name}.json"
        paths[name].write_text(json.dumps(payload))
    return module, paths, authorization, readiness, simulation


def _rehash_write(fixture, name, payload):
    module, paths, *_ = fixture
    payload["artifact_hash"] = module._hash(payload)
    paths[name].write_text(json.dumps(payload))


def _build(fixture, **overrides):
    module, paths, *_ = fixture
    return module.build(
        paths["authorization"],
        paths["readiness"],
        paths["simulation"],
        executor_build_identity_hash=overrides.get("build_hash", BUILD_HASH),
        previous_attempt_ids=overrides.get("previous", []),
        now=overrides.get("now", NOW),
    )


def test_valid_external_authorization_is_disposable_only(tmp_path: Path):
    report, manifest = _build(_fixture(tmp_path))
    assert report["authorization_valid_for_disposable_test_attempt"] is True
    assert report["authorization_valid_for_production"] is False
    assert report["execution_authorized"] is False
    assert manifest["authorization_generated_by_validator"] is False


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("externally_supplied", False, "AUTHORIZATION_NOT_EXTERNALLY_SUPPLIED"),
        ("generated_by_guarded_tooling", True, "AUTHORIZATION_NOT_EXTERNALLY_SUPPLIED"),
        ("revoked", True, "AUTHORIZATION_REVOKED"),
        ("scope", "PRODUCTION", "AUTHORIZATION_SCOPE_INVALID"),
        ("exchange_authorized", True, "PROHIBITED_AUTHORIZATION_SCOPE_PRESENT"),
        ("orders_authorized", True, "PROHIBITED_AUTHORIZATION_SCOPE_PRESENT"),
        ("operator_identity", "", "OPERATOR_IDENTITY_INVALID"),
        ("second_reviewer_identity", "operator-a", "SECOND_REVIEWER_IDENTITY_INVALID"),
        ("readiness_row_hashes", [], "READINESS_ROW_BINDING_MISMATCH"),
        ("executor_build_identity_hash", "c" * 64, "EXECUTOR_BUILD_IDENTITY_HASH_MISMATCH"),
    ],
)
def test_binding_scope_identity_and_revocation_fail_closed(
    tmp_path: Path, field: str, value, reason: str
):
    fixture = _fixture(tmp_path)
    authorization = fixture[2]
    authorization[field] = value
    _rehash_write(fixture, "authorization", authorization)
    report, _ = _build(fixture)
    assert report["authorization_valid_for_disposable_test_attempt"] is False
    assert reason in report["reason_codes"]


def test_exact_expiration_boundary_is_expired(tmp_path: Path):
    fixture = _fixture(tmp_path)
    authorization = fixture[2]
    authorization["expires_at"] = NOW.isoformat()
    _rehash_write(fixture, "authorization", authorization)
    report, _ = _build(fixture)
    assert "AUTHORIZATION_EXPIRED" in report["reason_codes"]


def test_expiration_cannot_exceed_readiness(tmp_path: Path):
    fixture = _fixture(tmp_path)
    authorization = fixture[2]
    authorization["expires_at"] = (NOW + timedelta(hours=2)).isoformat()
    _rehash_write(fixture, "authorization", authorization)
    report, _ = _build(fixture)
    assert "AUTHORIZATION_EXCEEDS_EARLIEST_EXPIRATION" in report["reason_codes"]


def test_duplicate_attempt_and_duplicate_history_fail_closed(tmp_path: Path):
    fixture = _fixture(tmp_path)
    report, _ = _build(fixture, previous=["attempt-001"])
    assert "ATTEMPT_ID_ALREADY_USED" in report["reason_codes"]
    with pytest.raises(ValueError, match="HISTORY_DUPLICATED"):
        _build(fixture, previous=["x", "x"])


def test_unsuccessful_or_wrong_lineage_simulation_refused(tmp_path: Path):
    fixture = _fixture(tmp_path)
    simulation = fixture[4]
    simulation["simulation_outcome"] = "SIMULATION_ROLLED_BACK_PRECONDITION"
    simulation["input_hashes"]["phase4ak_readiness"] = "wrong"
    _rehash_write(fixture, "simulation", simulation)
    authorization = fixture[2]
    authorization["simulation_report_hash"] = simulation["artifact_hash"]
    _rehash_write(fixture, "authorization", authorization)
    report, _ = _build(fixture)
    assert "SIMULATION_NOT_SUCCESSFUL" in report["reason_codes"]
    assert "SIMULATION_READINESS_LINEAGE_MISMATCH" in report["reason_codes"]


@pytest.mark.parametrize("name", ["authorization", "readiness", "simulation"])
def test_tampering_is_rejected_before_validation(tmp_path: Path, name: str):
    fixture = _fixture(tmp_path)
    payload = json.loads(fixture[1][name].read_text())
    payload["tampered"] = True
    fixture[1][name].write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="HASH_MISMATCH"):
        _build(fixture)


def test_deterministic_hash_valid_non_authorizing_outputs(tmp_path: Path):
    fixture = _fixture(tmp_path)
    first, second = _build(fixture), _build(fixture)
    assert first == second
    report, manifest = first
    assert report["artifact_hash"] == fixture[0]._hash(report)
    assert manifest["manifest_hash"] == fixture[0]._hash(manifest, "manifest_hash")
    rendered = json.dumps(first, sort_keys=True).lower()
    assert 'execution_authorized": true' not in rendered
    assert 'exchange_authorized": true' not in rendered
    assert 'orders_authorized": true' not in rendered
