from __future__ import annotations

import importlib.util
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest


def _module():
    path = Path(__file__).parents[1] / "scripts/local/phase4ax_expiration_revocation_gate.py"
    spec = importlib.util.spec_from_file_location("phase4ax_tested", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


NOW = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)


def _fixture(tmp_path: Path):
    module = _module()
    tmp_path.mkdir(parents=True, exist_ok=True)
    fields = (
        "proposal_hash",
        "review_hash",
        "readiness_hash",
        "approval_hash",
        "authorization_hash",
        "database_identity_hash",
        "executor_build_identity_hash",
    )
    bindings = {field: f"{index:x}" * 64 for index, field in enumerate(fields, start=1)}
    payload = {
        "schema": module.INPUT_SCHEMA,
        "deadlines": {name: (NOW + timedelta(minutes=5)).isoformat() for name in module.DEADLINES},
        "current_bindings": dict(bindings),
        "expected_bindings": dict(bindings),
        "revocations": [],
    }
    payload["artifact_hash"] = module._hash(payload)
    path = tmp_path / "gate-input.json"
    path.write_text(json.dumps(payload))
    return module, path


def _mutate(module, path: Path, mutate, *, rehash: bool = True):
    payload = json.loads(path.read_text())
    mutate(payload)
    if rehash:
        payload["artifact_hash"] = module._hash(payload)
    path.write_text(json.dumps(payload))


def test_all_current_unrevoked_and_before_deadlines_advances(tmp_path: Path):
    module, path = _fixture(tmp_path)
    verdict, proof = module.build(path, now=NOW)
    assert verdict["advancement_allowed"] is True
    assert verdict["reason_codes"] == []
    assert verdict["earliest_expiration"] == (NOW + timedelta(minutes=5)).isoformat()
    assert verdict["artifact_hash"] == module._hash(verdict)
    assert proof["verdict_hash"] == verdict["artifact_hash"]


@pytest.mark.parametrize("deadline", _module().DEADLINES)
def test_each_expiration_exact_boundary_refuses_and_just_before_passes(
    tmp_path: Path, deadline: str
):
    module, path = _fixture(tmp_path)
    boundary = NOW + timedelta(minutes=5)
    verdict, _ = module.build(path, now=boundary - timedelta(microseconds=1))
    assert verdict["advancement_allowed"] is True
    verdict, _ = module.build(path, now=boundary)
    assert f"{deadline.upper()}_EXPIRED" in verdict["reason_codes"]


@pytest.mark.parametrize(
    ("field", "reason"),
    [
        ("proposal_hash", "ARTIFACT_SUPERSEDED"),
        ("review_hash", "ARTIFACT_SUPERSEDED"),
        ("readiness_hash", "ARTIFACT_SUPERSEDED"),
        ("approval_hash", "ARTIFACT_SUPERSEDED"),
        ("authorization_hash", "ARTIFACT_SUPERSEDED"),
        ("database_identity_hash", "DATABASE_IDENTITY_CHANGED"),
        ("executor_build_identity_hash", "EXECUTOR_BUILD_SUPERSEDED"),
    ],
)
def test_each_supersession_or_identity_change_refuses(tmp_path: Path, field: str, reason: str):
    module, path = _fixture(tmp_path)
    _mutate(module, path, lambda p: p["current_bindings"].update({field: "f" * 64}))
    verdict, _ = module.build(path, now=NOW)
    assert reason in verdict["reason_codes"]
    assert verdict["advancement_allowed"] is False


def test_explicit_revocation_has_deterministic_highest_precedence(tmp_path: Path):
    module, path = _fixture(tmp_path)

    def mutate(payload):
        payload["revocations"] = [{"active": True, "reason": "operator-revoked"}]
        payload["current_bindings"]["proposal_hash"] = "f" * 64
        payload["current_bindings"]["database_identity_hash"] = "e" * 64
        payload["deadlines"]["proposal"] = NOW.isoformat()

    _mutate(module, path, mutate)
    verdict, proof = module.build(path, now=NOW)
    assert verdict["primary_reason"] == "EXPLICIT_REVOCATION"
    assert verdict["reason_codes"] == [
        reason for reason in module.PRECEDENCE if reason in set(verdict["reason_codes"])
    ]
    assert proof["deterministic_precedence_verified"] is True


def test_offset_equivalence_uses_utc_boundary(tmp_path: Path):
    module, path = _fixture(tmp_path)
    _mutate(
        module,
        path,
        lambda p: p["deadlines"].update(proposal="2026-08-25T07:00:00-05:00"),
    )
    verdict, _ = module.build(path, now=NOW)
    assert verdict["primary_reason"] == "PROPOSAL_EXPIRED"


def test_tampering_malformed_deadlines_bindings_revocations_and_time_fail_closed(tmp_path: Path):
    module, path = _fixture(tmp_path / "tamper")
    _mutate(module, path, lambda p: p.update(extra=True), rehash=False)
    with pytest.raises(ValueError, match="SCHEMA_OR_HASH_INVALID"):
        module.build(path, now=NOW)
    module, path = _fixture(tmp_path / "deadline")
    _mutate(module, path, lambda p: p["deadlines"].pop("review"))
    with pytest.raises(ValueError, match="DEADLINES_INVALID"):
        module.build(path, now=NOW)
    module, path = _fixture(tmp_path / "binding")
    _mutate(module, path, lambda p: p["current_bindings"].update(proposal_hash="short"))
    with pytest.raises(ValueError, match="BINDING_HASH_INVALID"):
        module.build(path, now=NOW)
    module, path = _fixture(tmp_path / "revocation")
    _mutate(module, path, lambda p: p.update(revocations="invalid"))
    with pytest.raises(ValueError, match="REVOCATIONS_INVALID"):
        module.build(path, now=NOW)
    module, path = _fixture(tmp_path / "time")
    with pytest.raises(ValueError, match="EVALUATION_TIMEZONE_MISSING"):
        module.build(path, now=datetime(2026, 8, 25, 12, 0))


def test_static_gate_is_artifact_only():
    source = (
        Path(__file__).parents[1] / "scripts/local/phase4ax_expiration_revocation_gate.py"
    ).read_text()
    assert "sqlite3" not in source
    assert "--production-db" not in source
    assert "systemctl" not in source
