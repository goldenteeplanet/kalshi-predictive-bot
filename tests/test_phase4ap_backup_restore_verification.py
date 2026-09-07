from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest


def _module():
    path = Path(__file__).parents[1] / "scripts/local/phase4ap_backup_restore_verification.py"
    spec = importlib.util.spec_from_file_location("phase4ap_tested", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


NOW = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)


def _fixture(tmp_path: Path):
    module = _module()
    protected = tmp_path / "protected"
    protected.mkdir(parents=True)
    production = protected / "production.db"
    research = protected / "research.db"
    for path, value in ((production, "production"), (research, "research")):
        con = sqlite3.connect(path)
        con.execute("CREATE TABLE sentinel (id INTEGER PRIMARY KEY, value TEXT)")
        con.execute("INSERT INTO sentinel VALUES (1, ?)", (value,))
        con.commit()
        con.close()
    source = tmp_path / "source" / "disposable.db"
    source.parent.mkdir()
    con = sqlite3.connect(source)
    con.execute(
        "CREATE TABLE phase4al_disposable_marker "
        "(id INTEGER PRIMARY KEY, marker_schema TEXT, disposable INTEGER)"
    )
    con.execute("INSERT INTO phase4al_disposable_marker VALUES (1, ?, 1)", (module.MARKER_SCHEMA,))
    con.execute("CREATE TABLE data (id INTEGER PRIMARY KEY, value TEXT UNIQUE)")
    con.executemany("INSERT INTO data VALUES (?, ?)", [(1, "alpha"), (2, "beta")])
    con.execute("CREATE INDEX data_value_index ON data(value)")
    con.commit()
    con.close()
    snapshot = tmp_path / "backup" / "snapshot.db"
    restore = tmp_path / "restore" / "restored.db"
    return module, source, snapshot, restore, production, research


def _build(fixture):
    module, source, snapshot, restore, production, research = fixture
    return module.build(
        source,
        snapshot,
        restore,
        production,
        research_db=research,
        now=NOW,
    )


def test_backup_and_distinct_restore_match_schema_rows_and_hashes(tmp_path: Path):
    fixture = _fixture(tmp_path)
    before = fixture[4].read_bytes(), fixture[5].read_bytes()
    report, proof = _build(fixture)
    assert report["snapshot_identity_verified"] is True
    assert proof["all_equalities_verified"] is True
    assert all(proof["equality_proof"].values())
    assert fixture[2].is_file() and fixture[3].is_file()
    assert (fixture[4].read_bytes(), fixture[5].read_bytes()) == before


def test_restored_database_is_independent_from_source(tmp_path: Path):
    fixture = _fixture(tmp_path)
    _build(fixture)
    con = sqlite3.connect(fixture[3])
    con.execute("UPDATE data SET value='changed' WHERE id=1")
    con.commit()
    con.close()
    source = sqlite3.connect(f"file:{fixture[1].as_posix()}?mode=ro", uri=True)
    assert source.execute("SELECT value FROM data WHERE id=1").fetchone()[0] == "alpha"
    source.close()


def test_snapshot_corruption_is_detected(tmp_path: Path):
    fixture = _fixture(tmp_path)
    report, _ = _build(fixture)
    with fixture[2].open("ab") as handle:
        handle.write(b"tamper")
    with pytest.raises(ValueError, match="FILE_HASH_MISMATCH"):
        fixture[0].verify_snapshot(fixture[2], report["snapshot_file_hash"])


def test_snapshot_wrong_expected_hash_fails_closed(tmp_path: Path):
    fixture = _fixture(tmp_path)
    _build(fixture)
    with pytest.raises(ValueError, match="FILE_HASH_MISMATCH"):
        fixture[0].verify_snapshot(fixture[2], "0" * 64)


@pytest.mark.parametrize("target_index", [2, 3])
def test_restore_or_snapshot_over_production_and_research_refused(
    tmp_path: Path, target_index: int
):
    fixture = list(_fixture(tmp_path))
    module, source, _, _, production, research = fixture
    for protected in (production, research):
        args = [source, fixture[2], fixture[3], production]
        args[target_index - 1] = protected
        with pytest.raises((FileExistsError, ValueError), match="OUTPUT_DATABASE_EXISTS|COLLISION"):
            module.build(*args, research_db=research, now=NOW)


def test_protected_parent_refused(tmp_path: Path):
    module, source, _, restore, production, research = _fixture(tmp_path)
    snapshot = production.parent / "snapshot.db"
    with pytest.raises(ValueError, match="PROTECTED"):
        module.build(source, snapshot, restore, production, research_db=research, now=NOW)


def test_hardlink_identity_refused(tmp_path: Path):
    module, source, snapshot, restore, production, research = _fixture(tmp_path)
    hardlink = tmp_path / "source-hardlink.db"
    try:
        os.link(production, hardlink)
    except OSError:
        pytest.skip("hard links unavailable")
    with pytest.raises(ValueError, match="HARD_LINK"):
        module.build(hardlink, snapshot, restore, production, research_db=research, now=NOW)


def test_missing_and_invalid_disposable_marker_refused(tmp_path: Path):
    fixture = _fixture(tmp_path)
    con = sqlite3.connect(fixture[1])
    con.execute("DROP TABLE phase4al_disposable_marker")
    con.commit()
    con.close()
    with pytest.raises(ValueError, match="MARKER_MISSING"):
        _build(fixture)
    fixture = _fixture(tmp_path / "invalid")
    con = sqlite3.connect(fixture[1])
    con.execute("UPDATE phase4al_disposable_marker SET marker_schema='wrong'")
    con.commit()
    con.close()
    with pytest.raises(ValueError, match="MARKER_INVALID"):
        _build(fixture)


def test_existing_snapshot_or_restore_refused(tmp_path: Path):
    fixture = _fixture(tmp_path)
    fixture[2].parent.mkdir(parents=True)
    fixture[2].write_bytes(b"existing")
    with pytest.raises(FileExistsError, match="OUTPUT_DATABASE_EXISTS"):
        _build(fixture)


def test_naive_time_refused(tmp_path: Path):
    module, source, snapshot, restore, production, research = _fixture(tmp_path)
    with pytest.raises(ValueError, match="TIMEZONE_MISSING"):
        module.build(
            source,
            snapshot,
            restore,
            production,
            research_db=research,
            now=datetime(2026, 8, 25),
        )


def test_artifacts_are_hash_valid_deterministic_and_non_authorizing(tmp_path: Path):
    first_fixture = _fixture(tmp_path / "first")
    first = _build(first_fixture)
    first_fixture[2].unlink()
    first_fixture[3].unlink()
    second = _build(first_fixture)
    assert first == second
    report, proof = first
    assert report["artifact_hash"] == first_fixture[0]._hash(report)
    assert proof["manifest_hash"] == first_fixture[0]._hash(proof, "manifest_hash")
    rendered = json.dumps(first, sort_keys=True).lower()
    assert 'execution_authorized": true' not in rendered
    assert 'production_database_mutated": true' not in rendered
    assert 'research_database_mutated": true' not in rendered
