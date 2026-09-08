from __future__ import annotations

import importlib.util
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest


def _module():
    path = Path(__file__).parents[1] / "scripts/local/phase4av_rollback_certification_matrix.py"
    spec = importlib.util.spec_from_file_location("phase4av_tested", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


NOW = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)


def _fixture(tmp_path: Path):
    module = _module()
    tmp_path.mkdir(parents=True, exist_ok=True)
    database = tmp_path / "template.db"
    connection = sqlite3.connect(database)
    connection.execute(
        "CREATE TABLE phase4al_disposable_marker "
        "(id INTEGER PRIMARY KEY, marker_schema TEXT, disposable INTEGER)"
    )
    connection.execute(
        "INSERT INTO phase4al_disposable_marker VALUES (1, ?, 1)", (module.MARKER_SCHEMA,)
    )
    connection.execute(
        "CREATE TABLE settlements "
        "(ticker TEXT PRIMARY KEY,result TEXT,settled_at TEXT,updated_at TEXT)"
    )
    connection.executemany(
        "INSERT INTO settlements VALUES (?, ?, NULL, ?)",
        [
            ("KXROLL-1", "YES", "2026-08-25T00:00:00+00:00"),
            ("KXROLL-2", "NO", "2026-08-25T00:00:00+00:00"),
        ],
    )
    connection.execute("CREATE TABLE unrelated(id INTEGER PRIMARY KEY,value TEXT)")
    connection.execute("INSERT INTO unrelated VALUES (1,'preserve')")
    connection.commit()
    connection.close()
    return module, database


def test_complete_matrix_certifies_every_required_failure_class(tmp_path: Path):
    module, database = _fixture(tmp_path)
    baseline = database.read_bytes()
    matrix, proof = module.build(database, now=NOW)
    assert matrix["scenario_count"] == len(module.SCENARIOS) == 11
    assert {row["scenario"] for row in matrix["rows"]} == set(module.SCENARIOS)
    assert all(row["state_preserved"] for row in matrix["rows"])
    assert {row["outcome"] for row in matrix["rows"]} == {
        "PRE_MUTATION_REFUSAL",
        "TRANSACTION_ROLLED_BACK",
    }
    assert database.read_bytes() == baseline
    assert matrix["artifact_hash"] == module._hash(matrix)
    assert proof["matrix_hash"] == matrix["artifact_hash"]
    assert proof["artifact_hash"] == module._hash(proof)


@pytest.mark.parametrize("scenario", _module().SCENARIOS)
def test_each_case_preserves_exact_logical_state(tmp_path: Path, scenario: str):
    module, database = _fixture(tmp_path)
    before = module._snapshot(database)
    outcome, after = module._attempt(database, scenario)
    assert outcome in {"PRE_MUTATION_REFUSAL", "TRANSACTION_ROLLED_BACK"}
    assert before == after == module._snapshot(database)


def test_invalid_marker_missing_rows_and_existing_work_files_fail_closed(tmp_path: Path):
    module, database = _fixture(tmp_path / "marker")
    connection = sqlite3.connect(database)
    connection.execute("UPDATE phase4al_disposable_marker SET disposable=0")
    connection.commit()
    connection.close()
    with pytest.raises(ValueError, match="NOT_DISPOSABLE"):
        module.build(database, now=NOW)
    module, database = _fixture(tmp_path / "rows")
    connection = sqlite3.connect(database)
    connection.execute("DELETE FROM settlements WHERE ticker='KXROLL-2'")
    connection.commit()
    connection.close()
    with pytest.raises(ValueError, match="INSUFFICIENT_ROWS"):
        module.build(database, now=NOW)
    module, database = _fixture(tmp_path / "existing")
    work = tmp_path / "work"
    work.mkdir()
    (work / "00-input_failure.db").write_text("occupied")
    with pytest.raises(FileExistsError, match="DISPOSABLE_CASE_EXISTS"):
        module.build(database, now=NOW, work_root=work)


def test_naive_time_and_unknown_scenario_fail_closed(tmp_path: Path):
    module, database = _fixture(tmp_path)
    with pytest.raises(ValueError, match="EVALUATION_TIMEZONE_MISSING"):
        module.build(database, now=datetime(2026, 8, 25, 12, 0))
    with pytest.raises(ValueError, match="SCENARIO_UNKNOWN"):
        module._attempt(database, "UNKNOWN")


def test_static_surface_is_disposable_only():
    source = (
        Path(__file__).parents[1] / "scripts/local/phase4av_rollback_certification_matrix.py"
    ).read_text()
    assert "--production-db" not in source
    assert "--research-db" not in source
    assert "systemctl" not in source
    assert "exchange" not in source.lower().replace("exchange_requests_made", "")
