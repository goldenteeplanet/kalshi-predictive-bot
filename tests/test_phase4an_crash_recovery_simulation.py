from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest


def _module():
    path = Path(__file__).parents[1] / "scripts/local/phase4an_crash_recovery_simulation.py"
    spec = importlib.util.spec_from_file_location("phase4an_tested", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


NOW = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)


def _fixture(tmp_path: Path):
    module = _module()
    protected = tmp_path / "protected"
    protected.mkdir()
    production = protected / "production.db"
    connection = sqlite3.connect(production)
    connection.execute("CREATE TABLE production_sentinel (id INTEGER PRIMARY KEY, value TEXT)")
    connection.execute("INSERT INTO production_sentinel VALUES (1, 'unchanged')")
    connection.commit()
    connection.close()
    disposable = tmp_path / "template" / "disposable.db"
    disposable.parent.mkdir()
    connection = sqlite3.connect(disposable)
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
        ("KXCRASH-1", "YES", "2026-08-25T00:00:00+00:00", "preserve"),
    )
    connection.commit()
    connection.close()
    work = tmp_path / "work"
    return module, production, disposable, work


@pytest.mark.parametrize("stage", _module().CRASH_STAGES)
def test_every_persistence_boundary_is_classified_and_recovered(tmp_path: Path, stage: str):
    module, production, disposable, work = _fixture(tmp_path)
    production_before = production.read_bytes()
    result = module.simulate(
        production,
        disposable,
        work,
        ticker="KXCRASH-1",
        proposed_settled_at="2026-08-25T12:00:01+00:00",
        crash_stage=stage,
        now=NOW,
    )
    assert result["crash_injected"] is True
    assert result["recovery_verified"] is True
    assert result["production_metadata_unchanged"] is True
    if stage in {
        "BEFORE_TRANSACTION",
        "DURING_TRANSACTION",
        "AFTER_MUTATION_BEFORE_COMMIT",
    }:
        assert result["database_state_after_crash"] == "PRE_TRANSACTION_STATE"
        assert result["artifact_state_after_recovery"] == "ABSENT"
    else:
        assert result["database_state_after_crash"] == "COMMITTED_STATE"
        assert result["artifact_state_after_recovery"] == "VALID_PAIR"
    assert production.read_bytes() == production_before
    assert work.is_dir() and not list(work.iterdir())


def test_campaign_proves_every_required_stage(tmp_path: Path):
    module, production, disposable, work = _fixture(tmp_path)
    report, proof = module.build(
        production,
        disposable,
        work,
        ticker="KXCRASH-1",
        proposed_settled_at="2026-08-25T12:00:01+00:00",
        now=NOW,
    )
    assert report["all_recoveries_verified"] is True
    assert len(report["scenario_results"]) == len(module.CRASH_STAGES)
    assert proof["verified_stage_count"] == proof["required_stage_count"]
    assert proof["recovery_proof_state"] == "ALL_PERSISTENCE_BOUNDARIES_RECOVERABLE"
    assert report["artifact_hash"] == module._hash(report)
    assert proof["manifest_hash"] == module._hash(proof, "manifest_hash")


def test_artifact_crash_states_are_distinguished(tmp_path: Path):
    module, production, disposable, work = _fixture(tmp_path)
    absent = module.simulate(
        production,
        disposable,
        work,
        ticker="KXCRASH-1",
        proposed_settled_at="2026-08-25T12:00:01+00:00",
        crash_stage="AFTER_DATABASE_COMMIT_BEFORE_RECEIPT",
        now=NOW,
    )
    partial = module.simulate(
        production,
        disposable,
        work,
        ticker="KXCRASH-1",
        proposed_settled_at="2026-08-25T12:00:01+00:00",
        crash_stage="BETWEEN_PAIRED_REPLACEMENTS",
        now=NOW,
    )
    assert absent["artifact_state_after_crash"] == "ABSENT"
    assert partial["artifact_state_after_crash"] == "PARTIAL_OR_INVALID"


def test_pair_validator_detects_tampering_and_missing_half(tmp_path: Path):
    module = _module()
    receipt, proof = module._receipt("operation", "KX", {}, {"settled_at": "x"})
    module._write_durable(tmp_path / "receipt.json", receipt)
    module._write_durable(tmp_path / "receipt-proof.json", proof)
    assert module._pair_valid(tmp_path) is True
    payload = json.loads((tmp_path / "receipt.json").read_text())
    payload["ticker"] = "TAMPERED"
    (tmp_path / "receipt.json").write_text(json.dumps(payload))
    assert module._pair_valid(tmp_path) is False
    (tmp_path / "receipt.json").unlink()
    assert module._pair_valid(tmp_path) is False


def test_same_path_symlink_and_hardlink_are_refused(tmp_path: Path):
    module, production, _, work = _fixture(tmp_path)
    with pytest.raises(ValueError, match="PATH_OVERLAP"):
        module._validate_template(production, production, work)
    if hasattr(os, "symlink"):
        symlink = tmp_path / "production-symlink.db"
        try:
            symlink.symlink_to(production)
        except OSError:
            pass
        else:
            with pytest.raises(ValueError, match="PATH_OVERLAP"):
                module._validate_template(production, symlink, work)
    hardlink = tmp_path / "production-hardlink.db"
    try:
        os.link(production, hardlink)
    except OSError:
        return
    with pytest.raises(ValueError, match="HARD_LINK"):
        module._validate_template(production, hardlink, work)


def test_protected_work_root_and_template_parent_are_refused(tmp_path: Path):
    module, production, disposable, _ = _fixture(tmp_path)
    protected_root = production.parent / "nested-work"
    protected_root.mkdir()
    with pytest.raises(ValueError, match="PATH_OVERLAP"):
        module._validate_template(production, disposable, protected_root)
    same_parent_template = production.parent / "copy.db"
    same_parent_template.write_bytes(disposable.read_bytes())
    with pytest.raises(ValueError, match="PATH_OVERLAP"):
        module._validate_template(production, same_parent_template, tmp_path / "safe-work")


def test_missing_and_invalid_disposable_marker_fail_closed(tmp_path: Path):
    module, production, disposable, work = _fixture(tmp_path)
    connection = sqlite3.connect(disposable)
    connection.execute("DROP TABLE phase4al_disposable_marker")
    connection.commit()
    connection.close()
    with pytest.raises(ValueError, match="MARKER_MISSING"):
        module._validate_template(production, disposable, work)
    connection = sqlite3.connect(disposable)
    connection.execute(
        "CREATE TABLE phase4al_disposable_marker "
        "(id INTEGER PRIMARY KEY, marker_schema TEXT, disposable INTEGER)"
    )
    connection.execute("INSERT INTO phase4al_disposable_marker VALUES (1, 'wrong', 1)")
    connection.commit()
    connection.close()
    with pytest.raises(ValueError, match="MARKER_INVALID"):
        module._validate_template(production, disposable, work)


def test_invalid_stage_and_naive_time_fail_closed(tmp_path: Path):
    module, production, disposable, work = _fixture(tmp_path)
    kwargs = {
        "ticker": "KXCRASH-1",
        "proposed_settled_at": "2026-08-25T12:00:01+00:00",
        "now": NOW,
    }
    with pytest.raises(ValueError, match="STAGE_INVALID"):
        module.simulate(production, disposable, work, crash_stage="UNKNOWN", **kwargs)
    kwargs["now"] = datetime(2026, 8, 25)
    with pytest.raises(ValueError, match="TIMEZONE_MISSING"):
        module.simulate(production, disposable, work, crash_stage="BEFORE_TRANSACTION", **kwargs)


def test_output_is_deterministic_and_non_authorizing(tmp_path: Path):
    module, production, disposable, work = _fixture(tmp_path)
    first = module.build(
        production,
        disposable,
        work,
        ticker="KXCRASH-1",
        proposed_settled_at="2026-08-25T12:00:01+00:00",
        now=NOW,
    )
    second = module.build(
        production,
        disposable,
        work,
        ticker="KXCRASH-1",
        proposed_settled_at="2026-08-25T12:00:01+00:00",
        now=NOW,
    )
    assert first == second
    rendered = json.dumps(first, sort_keys=True).lower()
    assert "production recovery command" not in rendered
    assert 'execution_authorized": true' not in rendered
    assert 'production_database_mutated": true' not in rendered
    assert "service control" not in rendered
