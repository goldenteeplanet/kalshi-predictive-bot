from __future__ import annotations

import importlib.util
import json
import shutil
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
        "phase4au_tested",
        Path(__file__).parents[1] / "scripts/local/phase4au_field_level_mutation_diff.py",
    )


NOW = datetime(2026, 8, 25, 12, 1, tzinfo=UTC)


def _fixture(tmp_path: Path):
    helper = _load(
        "phase4at_fixture_for_au", Path(__file__).with_name("test_phase4at_sandbox_executor.py")
    )
    f = helper._fixture(tmp_path)
    connection = sqlite3.connect(f["sandbox"])
    connection.execute("CREATE TABLE unrelated(id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
    connection.execute("CREATE INDEX unrelated_value_idx ON unrelated(value)")
    connection.execute("INSERT INTO unrelated VALUES (1, 'preserved')")
    connection.execute(
        "INSERT INTO settlements VALUES ('KXAT-2','NO',NULL,'2026-08-24T00:00:00+00:00','other')"
    )
    connection.execute("PRAGMA application_id=4120")
    connection.commit()
    connection.close()
    before = tmp_path / "before" / "snapshot.db"
    before.parent.mkdir()
    shutil.copy2(f["sandbox"], before)
    receipt, _ = helper._build(f)
    receipt_path = tmp_path / "phase4at-receipt.json"
    receipt_path.write_text(json.dumps(receipt))
    f.update({"au": _module(), "before": before, "after": f["sandbox"], "receipt": receipt_path})
    return f


def _build(f, **overrides):
    return f["au"].build(
        overrides.get("before", f["before"]),
        overrides.get("after", f["after"]),
        f["receipt"],
        now=overrides.get("now", NOW),
    )


def test_proves_exact_intended_change_and_all_unrelated_state(tmp_path: Path):
    f = _fixture(tmp_path)
    proof, preservation = _build(f)
    assert proof["change"]["field"] == "settled_at"
    assert proof["change"]["before"] is None
    assert proof["change"]["after"] == "2026-08-25T11:30:00+00:00"
    assert proof["artifact_hash"] == f["au"]._hash(proof)
    assert preservation["unrelated_rows_preserved"] is True
    assert preservation["columns_indexes_constraints_preserved"] is True
    assert preservation["application_metadata_preserved"] is True
    assert preservation["database_mutation_performed"] is False
    assert preservation["artifact_hash"] == f["au"]._hash(preservation)


def test_tampered_receipt_fails_closed(tmp_path: Path):
    f = _fixture(tmp_path)
    payload = json.loads(f["receipt"].read_text())
    payload["ticker"] = "tampered"
    f["receipt"].write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="RECEIPT_SCHEMA_OR_HASH_INVALID"):
        _build(f)


@pytest.mark.parametrize(
    ("sql", "reason"),
    [
        ("UPDATE unrelated SET value='changed'", "UNINTENDED_STATE_CHANGE"),
        (
            "UPDATE settlements SET payload='changed' WHERE ticker='KXAT-1'",
            "UNINTENDED_STATE_CHANGE",
        ),
        ("UPDATE settlements SET settled_at='x' WHERE ticker='KXAT-2'", "UNINTENDED_STATE_CHANGE"),
        ("DELETE FROM unrelated", "ROW_COUNT_CHANGED"),
        ("CREATE TABLE extra(value TEXT)", "SCHEMA_OBJECTS_CHANGED"),
        ("CREATE INDEX extra_idx ON settlements(result)", "SCHEMA_OBJECTS_CHANGED"),
        ("PRAGMA application_id=99", "APPLICATION_METADATA_CHANGED"),
    ],
)
def test_unrelated_schema_row_index_and_metadata_changes_fail_closed(
    tmp_path: Path, sql: str, reason: str
):
    f = _fixture(tmp_path)
    connection = sqlite3.connect(f["after"])
    connection.execute(sql)
    connection.commit()
    connection.close()
    with pytest.raises(ValueError, match=reason):
        _build(f)


def test_no_change_and_multiple_field_change_are_refused(tmp_path: Path):
    f = _fixture(tmp_path / "none")
    unchanged = tmp_path / "none" / "unchanged.db"
    shutil.copy2(f["before"], unchanged)
    with pytest.raises(ValueError, match="EXACTLY_ONE_INTENDED_CHANGE_REQUIRED"):
        _build(f, after=unchanged)
    f = _fixture(tmp_path / "multiple")
    connection = sqlite3.connect(f["after"])
    connection.execute("UPDATE settlements SET result='NO' WHERE ticker='KXAT-1'")
    connection.commit()
    connection.close()
    with pytest.raises(ValueError, match="UNINTENDED_STATE_CHANGE"):
        _build(f)


def test_same_file_and_hard_link_snapshots_are_refused(tmp_path: Path):
    f = _fixture(tmp_path / "same")
    with pytest.raises(ValueError, match="SNAPSHOTS_NOT_DISTINCT"):
        _build(f, before=f["after"])
    f = _fixture(tmp_path / "hardlink")
    alias = tmp_path / "hardlink" / "alias.db"
    alias.hardlink_to(f["after"])
    with pytest.raises(ValueError, match="SNAPSHOTS_NOT_DISTINCT"):
        _build(f, before=alias)


def test_missing_marker_and_receipt_row_hash_mismatch_are_refused(tmp_path: Path):
    f = _fixture(tmp_path / "marker")
    connection = sqlite3.connect(f["before"])
    connection.execute("UPDATE phase4al_disposable_marker SET disposable=0")
    connection.commit()
    connection.close()
    with pytest.raises(ValueError, match="DISPOSABLE_MARKER_INVALID"):
        _build(f)
    f = _fixture(tmp_path / "receipt")
    payload = json.loads(f["receipt"].read_text())
    payload["after_row_hash"] = "0" * 64
    payload["artifact_hash"] = f["au"]._hash(payload)
    f["receipt"].write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="AFTER_RECEIPT_MISMATCH"):
        _build(f)


def test_naive_evaluation_time_and_static_read_only_surface(tmp_path: Path):
    f = _fixture(tmp_path)
    with pytest.raises(ValueError, match="EVALUATION_TIMEZONE_MISSING"):
        _build(f, now=datetime(2026, 8, 25, 12, 1))
    source = (
        Path(__file__).parents[1] / "scripts/local/phase4au_field_level_mutation_diff.py"
    ).read_text()
    assert "mode=ro" in source
    assert "UPDATE settlements" not in source
    assert "--production-db" not in source
