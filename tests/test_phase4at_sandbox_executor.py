from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest


def _module():
    path = Path(__file__).parents[1] / "scripts/local/phase4at_sandbox_executor.py"
    spec = importlib.util.spec_from_file_location("phase4at_tested", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


NOW = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)
BUILD_HASH = "b" * 64


def _write(module, path: Path, payload: dict) -> None:
    payload["artifact_hash"] = module._hash(payload)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _identity(path: Path, kind: str) -> dict:
    resolved = path.resolve(strict=True)
    stat = resolved.stat()
    return {
        "kind": kind,
        "resolved_path": str(resolved),
        "device": stat.st_dev,
        "inode": stat.st_ino,
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def _fixture(tmp_path: Path):
    module = _module()
    tmp_path.mkdir(parents=True, exist_ok=True)
    protected_dir = tmp_path / "protected"
    protected_dir.mkdir()
    production = protected_dir / "production.db"
    research = protected_dir / "research.db"
    for path, value in ((production, "production"), (research, "research")):
        connection = sqlite3.connect(path)
        connection.execute("CREATE TABLE sentinel(value TEXT)")
        connection.execute("INSERT INTO sentinel VALUES (?)", (value,))
        connection.commit()
        connection.close()

    sandbox_dir = tmp_path / "sandbox"
    sandbox_dir.mkdir()
    sandbox = sandbox_dir / "simulation.db"
    connection = sqlite3.connect(sandbox)
    connection.execute(
        "CREATE TABLE phase4al_disposable_marker "
        "(id INTEGER PRIMARY KEY, marker_schema TEXT NOT NULL, disposable INTEGER NOT NULL)"
    )
    connection.execute(
        "INSERT INTO phase4al_disposable_marker VALUES (1, ?, 1)", (module.MARKER_SCHEMA,)
    )
    connection.execute(
        "CREATE TABLE settlements "
        "(ticker TEXT PRIMARY KEY, result TEXT, settled_at TEXT, updated_at TEXT, payload TEXT)"
    )
    connection.execute(
        "INSERT INTO settlements VALUES (?, ?, NULL, ?, ?)",
        ("KXAT-1", "YES", "2026-08-25T00:00:00+00:00", "preserve"),
    )
    connection.commit()
    before = dict(
        zip(
            ("ticker", "result", "settled_at", "updated_at", "payload"),
            connection.execute("SELECT * FROM settlements").fetchone(),
            strict=True,
        )
    )
    connection.close()

    production_identity = {"resolved_path": str(production.resolve()), "kind": "PRODUCTION"}
    row = {
        "ticker": "KXAT-1",
        "readiness_result": "READY",
        "proposed_settled_at": "2026-08-25T11:30:00+00:00",
        "compare_and_swap_preconditions": {
            "result": "YES",
            "updated_at": "2026-08-25T00:00:00+00:00",
            "settlement_lineage_hash": module.settlement_lineage_hash(before),
        },
        "readiness_row_hash": "a" * 64,
    }
    readiness = {
        "schema": module.AK_SCHEMA,
        "readiness_state": "READY_FOR_SEPARATE_EXECUTOR_DESIGN",
        "production_database_identity": production_identity,
        "rows": [row],
        "execution_authorized": False,
    }
    readiness_path = tmp_path / "ak.json"
    _write(module, readiness_path, readiness)
    simulation = {
        "schema": module.AL_SCHEMA,
        "simulation_outcome": "SIMULATION_COMMITTED",
        "input_hashes": {"phase4ak_readiness": readiness["artifact_hash"]},
        "execution_authorized": False,
    }
    simulation_path = tmp_path / "al.json"
    _write(module, simulation_path, simulation)
    authorization = {
        "schema": module.AQ_SCHEMA,
        "attempt_id": "attempt-001",
        "authorization_valid_for_disposable_test_attempt": True,
        "effective_expiration": (NOW + timedelta(minutes=5)).isoformat(),
        "validated_bindings": {
            "readiness_envelope_hash": readiness["artifact_hash"],
            "simulation_report_hash": simulation["artifact_hash"],
            "production_database_identity_hash": module.canonical_hash(production_identity),
            "executor_build_identity_hash": BUILD_HASH,
        },
        "execution_authorized": False,
    }
    authorization_path = tmp_path / "aq.json"
    _write(module, authorization_path, authorization)
    protected = {
        "schema": module.PROTECTED_SCHEMA,
        "identities": [_identity(production, "PRODUCTION"), _identity(research, "RESEARCH")],
    }
    protected_path = tmp_path / "protected.json"
    _write(module, protected_path, protected)
    return {
        "module": module,
        "sandbox": sandbox,
        "production": production,
        "research": research,
        "readiness": readiness_path,
        "simulation": simulation_path,
        "authorization": authorization_path,
        "protected": protected_path,
    }


def _build(f, **overrides):
    return f["module"].build(
        overrides.get("sandbox", f["sandbox"]),
        f["protected"],
        f["readiness"],
        f["simulation"],
        f["authorization"],
        executor_build_identity_hash=overrides.get("build_hash", BUILD_HASH),
        now=overrides.get("now", NOW),
    )


def _mutate_json(f, key: str, mutate, *, rehash: bool = True) -> None:
    path = f[key]
    payload = json.loads(path.read_text())
    mutate(payload)
    if rehash:
        payload["artifact_hash"] = f["module"]._hash(payload)
    path.write_text(json.dumps(payload))


def test_commits_only_disposable_sandbox_and_emits_hash_protected_non_authorization(tmp_path: Path):
    f = _fixture(tmp_path)
    protected_before = (f["production"].read_bytes(), f["research"].read_bytes())
    receipt, proof = _build(f)
    connection = sqlite3.connect(f["sandbox"])
    assert (
        connection.execute("SELECT settled_at FROM settlements").fetchone()[0]
        == "2026-08-25T11:30:00+00:00"
    )
    connection.close()
    assert protected_before == (f["production"].read_bytes(), f["research"].read_bytes())
    assert receipt["affected_row_count"] == 1
    assert receipt["execution_authorized"] is False
    assert receipt["production_database_mutated"] is False
    assert receipt["artifact_hash"] == f["module"]._hash(receipt)
    assert proof["receipt_hash"] == receipt["artifact_hash"]
    assert proof["manifest_hash"] == f["module"]._hash(proof, "manifest_hash")


@pytest.mark.parametrize("key", ["readiness", "simulation", "authorization", "protected"])
def test_tampered_inputs_fail_closed(tmp_path: Path, key: str):
    f = _fixture(tmp_path)
    _mutate_json(f, key, lambda payload: payload.update({"tampered": True}), rehash=False)
    with pytest.raises(ValueError, match="SCHEMA_OR_HASH_INVALID"):
        _build(f)


@pytest.mark.parametrize(
    ("key", "mutate", "reason"),
    [
        ("readiness", lambda p: p.update(readiness_state="NOT_READY"), "READINESS_NOT_READY"),
        (
            "simulation",
            lambda p: p.update(simulation_outcome="ROLLED_BACK"),
            "SIMULATION_LINEAGE_INVALID",
        ),
        (
            "authorization",
            lambda p: p.update(authorization_valid_for_disposable_test_attempt=False),
            "TEST_AUTHORIZATION_INVALID",
        ),
        ("authorization", lambda p: p.update(attempt_id=""), "ATTEMPT_ID_INVALID"),
    ],
)
def test_invalid_state_fails_closed(tmp_path: Path, key: str, mutate, reason: str):
    f = _fixture(tmp_path)
    _mutate_json(f, key, mutate)
    with pytest.raises(ValueError, match=reason):
        _build(f)


def test_expiration_boundary_is_exclusive(tmp_path: Path):
    f = _fixture(tmp_path)
    expiration = NOW + timedelta(minutes=5)
    _build(f, now=expiration - timedelta(microseconds=1))
    f = _fixture(tmp_path / "equal")
    with pytest.raises(ValueError, match="AUTHORIZATION_EXPIRED"):
        _build(f, now=expiration)


def test_protected_identity_alias_and_parent_are_refused(tmp_path: Path):
    f = _fixture(tmp_path / "direct")
    with pytest.raises(ValueError, match="SANDBOX_MATCHES_PROTECTED_IDENTITY"):
        _build(f, sandbox=f["production"])
    f = _fixture(tmp_path / "parent")
    alias = f["production"].parent / "other.db"
    alias.write_bytes(f["sandbox"].read_bytes())
    with pytest.raises(ValueError, match="SANDBOX_IN_PROTECTED_DIRECTORY"):
        _build(f, sandbox=alias)


@pytest.mark.skipif(not hasattr(os, "link"), reason="hard links unavailable")
def test_hard_link_to_protected_identity_is_refused(tmp_path: Path):
    f = _fixture(tmp_path)
    alias_dir = tmp_path / "alias"
    alias_dir.mkdir()
    alias = alias_dir / "production-alias.db"
    os.link(f["production"], alias)
    with pytest.raises(ValueError, match="SANDBOX_MATCHES_PROTECTED_IDENTITY"):
        _build(f, sandbox=alias)


def test_protected_metadata_drift_is_refused(tmp_path: Path):
    f = _fixture(tmp_path)
    with f["production"].open("ab") as stream:
        stream.write(b"drift")
    with pytest.raises(ValueError, match="PROTECTED_IDENTITY_DRIFT"):
        _build(f)


def test_marker_and_database_precondition_failures_roll_back(tmp_path: Path):
    f = _fixture(tmp_path / "marker")
    connection = sqlite3.connect(f["sandbox"])
    connection.execute("UPDATE phase4al_disposable_marker SET disposable=0")
    connection.commit()
    connection.close()
    with pytest.raises(ValueError, match="DISPOSABLE_MARKER_INVALID"):
        _build(f)
    f = _fixture(tmp_path / "lineage")
    connection = sqlite3.connect(f["sandbox"])
    connection.execute("UPDATE settlements SET result='NO'")
    connection.commit()
    connection.close()
    with pytest.raises(ValueError, match="PRECONDITION_LINEAGE_CHANGED"):
        _build(f)


def test_binding_build_timestamp_and_authorization_flags_fail_closed(tmp_path: Path):
    f = _fixture(tmp_path / "binding")
    _mutate_json(
        f,
        "authorization",
        lambda p: p["validated_bindings"].update(production_database_identity_hash="0" * 64),
    )
    with pytest.raises(ValueError, match="PRODUCTION_IDENTITY_BINDING_MISMATCH"):
        _build(f)
    f = _fixture(tmp_path / "build")
    with pytest.raises(ValueError, match="BUILD_IDENTITY_MISMATCH"):
        _build(f, build_hash="c" * 64)
    f = _fixture(tmp_path / "timestamp")
    _mutate_json(f, "readiness", lambda p: p["rows"][0].update(proposed_settled_at="naive"))
    readiness = json.loads(f["readiness"].read_text())
    _mutate_json(
        f,
        "simulation",
        lambda p: p["input_hashes"].update(phase4ak_readiness=readiness["artifact_hash"]),
    )
    simulation = json.loads(f["simulation"].read_text())
    _mutate_json(
        f,
        "authorization",
        lambda p: p["validated_bindings"].update(
            readiness_envelope_hash=readiness["artifact_hash"],
            simulation_report_hash=simulation["artifact_hash"],
        ),
    )
    with pytest.raises(ValueError, match="PROPOSED_TIMESTAMP_INVALID"):
        _build(f)
    f = _fixture(tmp_path / "flag")
    _mutate_json(f, "simulation", lambda p: p.update(execution_authorized=True))
    simulation = json.loads(f["simulation"].read_text())
    _mutate_json(
        f,
        "authorization",
        lambda p: p["validated_bindings"].update(
            simulation_report_hash=simulation["artifact_hash"]
        ),
    )
    with pytest.raises(ValueError, match="PRODUCTION_AUTHORIZATION_PRESENT"):
        _build(f)


def test_cli_exposes_no_production_or_research_database_override():
    source = (Path(__file__).parents[1] / "scripts/local/phase4at_sandbox_executor.py").read_text()
    assert 'add_argument("--production-db"' not in source
    assert 'add_argument("--research-db"' not in source
    assert "SET_CANONICAL_SETTLED_AT_IF_NULL" in source
