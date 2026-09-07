from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sqlite3
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
        "phase4al_tested",
        Path(__file__).parents[1] / "scripts/local/phase4al_offline_protocol_simulation.py",
    )


def _fixture(tmp_path: Path, *, ready: bool = True):
    helper = _load(
        "phase4ak_fixture_for_4al",
        Path(__file__).with_name("test_phase4ak_readiness_envelope.py"),
    )
    tmp_path.mkdir(parents=True, exist_ok=True)
    f = helper._fixture(tmp_path, approved=ready)
    envelope, handoff = helper._build(f)
    readiness, handoff_path = tmp_path / "phase4ak.json", tmp_path / "phase4ak-handoff.json"
    readiness.write_text(json.dumps(envelope))
    handoff_path.write_text(json.dumps(handoff))
    sim_dir = tmp_path / "disposable"
    sim_dir.mkdir()
    simulation = sim_dir / "simulation.db"
    shutil.copy2(f["database"], simulation)
    connection = sqlite3.connect(simulation)
    connection.execute(
        "CREATE TABLE phase4al_disposable_marker "
        "(id INTEGER PRIMARY KEY, marker_schema TEXT NOT NULL, disposable INTEGER NOT NULL)"
    )
    connection.execute(
        "INSERT INTO phase4al_disposable_marker VALUES (1, ?, 1)",
        (_module().MARKER_SCHEMA,),
    )
    connection.commit()
    connection.close()
    f.update(
        {
            "al": _module(),
            "readiness": readiness,
            "handoff": handoff_path,
            "simulation": simulation,
        }
    )
    return f


def _build(f, **overrides):
    return f["al"].build(
        overrides.get("production", f["database"]),
        overrides.get("simulation", f["simulation"]),
        f["readiness"],
        f["handoff"],
        now=overrides.get("now", f["now"]),
        failure_stage=overrides.get("failure_stage"),
    )


def _settled_at(path: Path):
    connection = sqlite3.connect(path)
    value = connection.execute(
        "SELECT settled_at FROM settlements WHERE ticker='KXTEST-1'"
    ).fetchone()[0]
    connection.close()
    return value


def test_successful_disposable_simulation(tmp_path: Path):
    f = _fixture(tmp_path)
    before = f["database"].read_bytes()
    report, proof = _build(f)
    assert report["simulation_outcome"] == "SIMULATION_COMMITTED"
    assert report["affected_row_count"] == 1
    assert report["unrelated_state_preserved"] is True
    assert _settled_at(f["simulation"]) == "2026-08-25T19:00:00+00:00"
    assert f["database"].read_bytes() == before
    assert proof["proof_state"] == "SIMULATION_SUCCESS"
    assert proof["production_execution_authorized"] is False


@pytest.mark.parametrize("stage", _module().FAILURE_STAGES)
def test_every_injected_failure_preserves_disposable_state(tmp_path: Path, stage: str):
    f = _fixture(tmp_path)
    before = f["simulation"].read_bytes()
    report, proof = _build(f, failure_stage=stage)
    assert report["simulation_outcome"] == "SIMULATION_ROLLED_BACK_PRECONDITION"
    assert report["rollback_verified"] is True
    assert _settled_at(f["simulation"]) is None
    assert proof["proof_state"] == "TRANSACTION_ROLLBACK_VERIFIED"
    if stage in {"BEFORE_TRANSACTION", "AFTER_INITIAL_PRECONDITION"}:
        assert f["simulation"].read_bytes() == before


def test_same_path_refused(tmp_path: Path):
    f = _fixture(tmp_path)
    with pytest.raises(ValueError, match="PATH_IDENTITY_EQUAL"):
        _build(f, simulation=f["database"])


def test_phase4ak_production_identity_is_bound_to_checked_database(tmp_path: Path):
    f = _fixture(tmp_path)
    readiness = json.loads(f["readiness"].read_text())
    readiness["production_database_identity"]["size"] += 1
    readiness["artifact_hash"] = f["al"]._hash(readiness)
    f["readiness"].write_text(json.dumps(readiness))
    handoff = json.loads(f["handoff"].read_text())
    handoff["readiness_envelope_hash"] = readiness["artifact_hash"]
    handoff["manifest_hash"] = f["al"]._hash(handoff, "manifest_hash")
    f["handoff"].write_text(json.dumps(handoff))
    with pytest.raises(ValueError, match="PRODUCTION_IDENTITY_MISMATCH"):
        _build(f)


def test_symlink_and_hardlink_to_production_refused(tmp_path: Path):
    f = _fixture(tmp_path)
    link_dir = tmp_path / "links"
    link_dir.mkdir()
    symlink = link_dir / "symlink.db"
    try:
        symlink.symlink_to(f["database"])
    except OSError:
        pytest.skip("symlinks unavailable")
    with pytest.raises(ValueError, match="PATH_IDENTITY_EQUAL"):
        _build(f, simulation=symlink)
    hardlink = link_dir / "hardlink.db"
    os.link(f["database"], hardlink)
    with pytest.raises(ValueError, match="HARD_LINK"):
        _build(f, simulation=hardlink)


def test_protected_directory_refused(tmp_path: Path):
    f = _fixture(tmp_path)
    peer = f["database"].parent / "peer.db"
    shutil.copy2(f["simulation"], peer)
    with pytest.raises(ValueError, match="PROTECTED_DIRECTORY"):
        _build(f, simulation=peer)


def test_missing_and_invalid_marker_refused(tmp_path: Path):
    f = _fixture(tmp_path)
    connection = sqlite3.connect(f["simulation"])
    connection.execute("DROP TABLE phase4al_disposable_marker")
    connection.commit()
    connection.close()
    with pytest.raises(ValueError, match="MARKER_MISSING"):
        _build(f)
    f = _fixture(tmp_path / "invalid")
    connection = sqlite3.connect(f["simulation"])
    connection.execute("UPDATE phase4al_disposable_marker SET disposable=0")
    connection.commit()
    connection.close()
    with pytest.raises(ValueError, match="MARKER_INVALID"):
        _build(f)


def test_nonready_and_expired_refused_without_mutation(tmp_path: Path):
    f = _fixture(tmp_path, ready=False)
    report, _ = _build(f)
    assert report["simulation_outcome"] == "SIMULATION_REFUSED_NOT_READY"
    assert _settled_at(f["simulation"]) is None
    f = _fixture(tmp_path / "expired")
    report, _ = _build(f, now=f["now"].replace(year=2027))
    assert report["simulation_outcome"] == "SIMULATION_REFUSED_EXPIRED"
    assert _settled_at(f["simulation"]) is None


@pytest.mark.parametrize("key", ["readiness", "handoff"])
def test_tampering_fails_closed(tmp_path: Path, key: str):
    f = _fixture(tmp_path)
    payload = json.loads(f[key].read_text())
    payload["tampered"] = True
    f[key].write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="HASH_MISMATCH"):
        _build(f)


def test_publication_pair_and_hash_lineage_fail_closed(tmp_path: Path):
    f = _fixture(tmp_path)
    handoff = json.loads(f["handoff"].read_text())
    handoff["publication_pair_id"] = "wrong"
    handoff["manifest_hash"] = f["al"]._hash(handoff, "manifest_hash")
    f["handoff"].write_text(json.dumps(handoff))
    with pytest.raises(ValueError, match="PAIR_MISMATCH"):
        _build(f)


def test_precondition_drift_rolls_back(tmp_path: Path):
    f = _fixture(tmp_path)
    connection = sqlite3.connect(f["simulation"])
    connection.execute("UPDATE settlements SET updated_at='changed' WHERE ticker='KXTEST-1'")
    connection.commit()
    connection.close()
    report, _ = _build(f)
    assert report["simulation_outcome"] == "SIMULATION_ROLLED_BACK_PRECONDITION"
    assert report["rollback_verified"] is True


def test_zero_affected_rows_rolls_back(tmp_path: Path):
    f = _fixture(tmp_path)
    connection = sqlite3.connect(f["simulation"])
    connection.execute(
        "CREATE TRIGGER ignore_settlement_update BEFORE UPDATE OF settled_at "
        "ON settlements BEGIN SELECT RAISE(IGNORE); END"
    )
    connection.commit()
    connection.close()
    report, proof = _build(f)
    assert report["simulation_outcome"] == "SIMULATION_ROLLED_BACK_ROW_COUNT"
    assert report["rollback_verified"] is True
    assert proof["proof_state"] == "TRANSACTION_ROLLBACK_VERIFIED"


def test_deterministic_from_identical_starting_snapshots(tmp_path: Path):
    second = _fixture(tmp_path / "second")
    # Paths are part of the proof, so reuse one chain and reset its clone.
    pristine = second["simulation"].read_bytes()
    result_one = _build(second)
    second["simulation"].write_bytes(pristine)
    result_two = _build(second)
    assert result_one == result_two


def test_atomic_publication_and_replacement_rollback(tmp_path: Path, monkeypatch):
    f = _fixture(tmp_path)
    report, proof = _build(f)
    module = f["al"]
    a, b = tmp_path / "report.json", tmp_path / "proof.json"
    module.publish_pair(a, b, report, proof)
    with pytest.raises(FileExistsError):
        module.publish_pair(a, b, report, proof)
    old_a, old_b = a.read_bytes(), b.read_bytes()
    original, calls = module.os.replace, 0

    def fail_fourth(src, dst):
        nonlocal calls
        calls += 1
        if calls == 4:
            raise OSError("simulated publication failure")
        return original(src, dst)

    monkeypatch.setattr(module.os, "replace", fail_fourth)
    with pytest.raises(OSError, match="simulated publication failure"):
        module.publish_pair(a, b, report, proof, replace=True)
    assert a.read_bytes() == old_a and b.read_bytes() == old_b
    assert not list(tmp_path.glob(".*.tmp")) and not list(tmp_path.glob(".*.bak"))


def test_outputs_are_non_authorizing_and_contain_no_sql(tmp_path: Path):
    rendered = json.dumps(_build(_fixture(tmp_path)), sort_keys=True).lower()
    assert "update settlements" not in rendered
    assert 'production_database_mutated": true' not in rendered
    assert 'production_execution_authorized": true' not in rendered
    assert 'execution_authorized": true' not in rendered
