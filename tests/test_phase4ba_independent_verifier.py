from __future__ import annotations

import importlib.util
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _module():
    return _load(
        "phase4ba_tested",
        Path(__file__).parents[1] / "scripts/local/phase4ba_independent_verifier.py",
    )


NOW = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)


def _fixture(tmp_path: Path):
    module = _module()
    az = _load(
        "phase4az_fixture_for_ba",
        Path(__file__).parents[1] / "scripts/local/phase4az_long_chain_history.py",
    )
    tmp_path.mkdir(parents=True, exist_ok=True)
    database = tmp_path / "production-copy.db"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE settlements(ticker TEXT PRIMARY KEY,result TEXT)")
    connection.execute("INSERT INTO settlements VALUES ('KXBA-1','YES')")
    connection.execute("CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT)")
    connection.execute("INSERT INTO metadata VALUES ('mode','paper')")
    connection.commit()
    connection.close()
    state = module.inspect_state(database)
    identity = {
        "schema": module.IDENTITY_SCHEMA,
        "resolved_path": str(database.resolve()),
        "metadata": module._metadata(database),
        "schema_hash": state["schema_hash"],
        "table_counts_hash": state["table_counts_hash"],
    }
    identity["artifact_hash"] = module._hash(identity)
    identity_path = tmp_path / "identity.json"
    identity_path.write_text(json.dumps(identity))
    history = tmp_path / "history"
    for index in range(3):
        payload = {
            "schema": f"fixture.ba.{index}.v1",
            "evaluated_at": f"2026-08-25T11:0{index}:00+00:00",
            "lineage_hash": f"{index + 1}" * 64,
        }
        payload["artifact_hash"] = az._hash(payload, "artifact_hash")
        source = tmp_path / f"source-{index}.json"
        source.write_text(json.dumps(payload))
        az.append(
            history,
            source,
            phase=f"4A{chr(ord('C') + index)}",
            timestamp_field="evaluated_at",
            retention=5,
        )
    return module, az, database, identity_path, history


def test_independent_verifier_reconstructs_chain_and_reads_current_state(tmp_path: Path):
    module, _, database, identity, history = _fixture(tmp_path)
    before = database.read_bytes()
    report, human = module.build(history, identity, now=NOW)
    assert report["verification_passed"] is True
    assert report["reason_codes"] == []
    assert report["production_metadata_before"] == report["production_metadata_after"]
    assert report["production_state"]["query_only_enforced"] is True
    assert report["database_access_mode"] == "READ_ONLY"
    assert database.read_bytes() == before
    assert "PASS" in human and report["artifact_hash"] in human
    assert report["artifact_hash"] == module._hash(report)


def test_chain_failure_has_precedence_and_prevents_database_access(tmp_path: Path):
    module, _, database, identity, history = _fixture(tmp_path)
    manifest = history / "manifest.json"
    payload = json.loads(manifest.read_text())
    payload["head_entry_hash"] = "0" * 64
    manifest.write_text(json.dumps(payload))
    database.unlink()
    report, human = module.build(history, identity, now=NOW)
    assert report["first_failure"] == "CHAIN_INVALID"
    assert report["production_metadata_before"] is None
    assert "FAIL" in human


def test_identity_and_state_drift_fail_closed(tmp_path: Path):
    module, _, database, identity, history = _fixture(tmp_path / "identity")
    payload = json.loads(identity.read_text())
    payload["metadata"]["size"] += 1
    payload["artifact_hash"] = module._hash(payload)
    identity.write_text(json.dumps(payload))
    report, _ = module.build(history, identity, now=NOW)
    assert report["first_failure"] == "PRODUCTION_IDENTITY_DRIFT"
    module, _, _, identity, history = _fixture(tmp_path / "state")
    payload = json.loads(identity.read_text())
    payload["schema_hash"] = "0" * 64
    payload["artifact_hash"] = module._hash(payload)
    identity.write_text(json.dumps(payload))
    report, _ = module.build(history, identity, now=NOW)
    assert report["first_failure"] == "PRODUCTION_STATE_DRIFT"


def test_identity_artifact_tampering_and_naive_time_fail_closed(tmp_path: Path):
    module, _, _, identity, history = _fixture(tmp_path / "tamper")
    payload = json.loads(identity.read_text())
    payload["schema_hash"] = "0" * 64
    identity.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="IDENTITY_SCHEMA_OR_HASH_INVALID"):
        module.build(history, identity, now=NOW)
    module, _, _, identity, history = _fixture(tmp_path / "time")
    with pytest.raises(ValueError, match="EVALUATION_TIMEZONE_MISSING"):
        module.build(history, identity, now=datetime(2026, 8, 25, 12, 0))


def test_failure_precedence_is_fixed():
    module = _module()
    assert module.PRECEDENCE == (
        "CHAIN_INVALID",
        "PRODUCTION_IDENTITY_DRIFT",
        "CONCURRENT_AUTHORITATIVE_WRITER",
        "PRODUCTION_STATE_DRIFT",
    )


def test_static_cli_has_no_mutation_service_lock_or_exchange_capability():
    source = (
        Path(__file__).parents[1] / "scripts/local/phase4ba_independent_verifier.py"
    ).read_text()
    assert "mode=ro" in source and "query_only=ON" in source
    assert "INSERT INTO" not in source and "UPDATE " not in source and "DELETE FROM" not in source
    assert "systemctl" not in source
    assert "writer_lock" not in source
    assert "--production-db" not in source
    assert "exchange" not in source.lower().replace("exchange_requests_made", "")
