import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest

from kalshi_predictor.overnight_paper import runner


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "sprint.db"
    with closing(sqlite3.connect(path)) as db:
        db.execute(
            "CREATE TABLE overnight_sprint_cycles("
            "id TEXT PRIMARY KEY,captured_at TEXT NOT NULL,payload TEXT NOT NULL)"
        )
        for table in (
            "overnight_shadow",
            "overnight_history",
            "weather_settlement_rules",
            "paper_orders",
            "paper_fills",
            "paper_positions",
        ):
            db.execute(f"CREATE TABLE {table}(id INTEGER PRIMARY KEY)")
        db.commit()
    return path


def capture(root, **kwargs):
    assert kwargs["resume_from"] is None
    root.mkdir()
    return {
        "rows": [
            {"source_time": "2026-09-08T00:00:00Z", "first_blocker": "SETTLEMENT_RULE_UNCERTIFIED"}
        ],
        "eligible_candidates": [],
        "orders_created": 0,
    }


def test_resume_is_idempotent_and_preserves_source_clocks(database, tmp_path, monkeypatch):
    calls = []

    def discover(root, **kwargs):
        calls.append(root)
        return capture(root, **kwargs)

    monkeypatch.setattr(runner, "run_discovery", discover)
    for _ in range(2):
        result = runner.run_observation_cycles(
            database, tmp_path / "archives", run_id="one", cycles=1
        )
        assert result["cycles_completed"] == 1
        assert result["orders_created"] == result["shadows_created"] == 0
    assert len(calls) == 1
    with closing(sqlite3.connect(database)) as db:
        payload = json.loads(
            db.execute("SELECT payload FROM overnight_sprint_cycles").fetchone()[0]
        )
        assert payload["result"]["rows"][0]["source_time"] == "2026-09-08T00:00:00Z"
        assert payload["result"]["rows"][0]["first_blocker"] == "SETTLEMENT_RULE_UNCERTIFIED"
        assert db.execute("SELECT count(*) FROM paper_orders").fetchone()[0] == 0


def test_holds_single_writer_lock_through_public_capture(database, tmp_path, monkeypatch):
    def discover(root, **kwargs):
        with closing(sqlite3.connect(database, timeout=0)) as other:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                other.execute("BEGIN IMMEDIATE")
        return capture(root, **kwargs)

    monkeypatch.setattr(runner, "run_discovery", discover)
    runner.run_observation_cycles(database, tmp_path / "archives", run_id="lock", cycles=1)


def test_existing_writer_refuses_before_network(database, tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "run_discovery", lambda *a, **k: pytest.fail("network reached"))
    with closing(sqlite3.connect(database)) as existing:
        existing.execute("BEGIN IMMEDIATE")
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            runner.run_observation_cycles(database, tmp_path / "archives", run_id="lock", cycles=1)


def test_error_checkpoint_stops_and_requires_review(database, tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("source unavailable")

    monkeypatch.setattr(runner, "run_discovery", fail)
    result = runner.run_observation_cycles(database, tmp_path / "archives", run_id="fail", cycles=2)
    assert result["status"] == "ERROR"
    assert result["cycles_completed"] == 1
    with pytest.raises(ValueError, match="PREVIOUS_CYCLE_ERROR_REQUIRES_REVIEW"):
        runner.run_observation_cycles(database, tmp_path / "archives", run_id="fail", cycles=2)


def test_checkpoint_corruption_fails_closed(database, tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "run_discovery", capture)
    runner.run_observation_cycles(database, tmp_path / "archives", run_id="corrupt", cycles=1)
    with closing(sqlite3.connect(database)) as db:
        db.execute("UPDATE overnight_sprint_cycles SET payload='{}'")
        db.commit()
    with pytest.raises(ValueError, match="CHECKPOINT_HASH_MISMATCH"):
        runner.run_observation_cycles(database, tmp_path / "archives", run_id="corrupt", cycles=1)


def test_resume_config_drift_rejected(database, tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "run_discovery", capture)
    runner.run_observation_cycles(database, tmp_path / "archives", run_id="config", cycles=1)
    with pytest.raises(ValueError, match="CHECKPOINT_CONFIG_MISMATCH"):
        runner.run_observation_cycles(
            database, tmp_path / "archives", run_id="config", cycles=1, max_pages=1
        )


def test_each_cycle_uses_fresh_window_and_unique_archive(database, tmp_path, monkeypatch):
    class Clock(datetime):
        value = datetime.now(UTC)

        @classmethod
        def now(cls, tz=None):
            return cls.value

    roots = []

    def discover(root, **kwargs):
        roots.append(root)
        Clock.value += timedelta(seconds=60)
        return capture(root, **kwargs)

    monkeypatch.setattr(runner, "datetime", Clock)
    monkeypatch.setattr(runner, "run_discovery", discover)
    result = runner.run_observation_cycles(
        database, tmp_path / "archives", run_id="fresh", cycles=2
    )
    assert result["cycles_completed"] == 2
    assert roots[0] != roots[1]


def test_authorizer_denies_other_tables_and_ddl(database):
    with closing(sqlite3.connect(database)) as db:
        db.set_authorizer(runner._authorize)
        for statement in (
            "INSERT INTO paper_orders VALUES(1)",
            "DELETE FROM overnight_sprint_cycles",
            "DROP TABLE paper_orders",
            "ATTACH DATABASE ':memory:' AS other",
        ):
            with pytest.raises(sqlite3.DatabaseError, match="authorized"):
                db.execute(statement)


def test_unowned_or_missing_database_never_initialized(tmp_path):
    missing = tmp_path / "missing.db"
    with pytest.raises(ValueError, match="EXISTING_SPRINT_DATABASE_REQUIRED"):
        runner.run_observation_cycles(missing, tmp_path / "archives", run_id="missing", cycles=1)
    assert not missing.exists()
    missing.write_text("corrupt")
    with pytest.raises(sqlite3.DatabaseError):
        runner.run_observation_cycles(missing, tmp_path / "archives", run_id="bad", cycles=1)


def test_synchronized_or_nested_database_paths_refused(database, tmp_path):
    with pytest.raises(ValueError, match="UNSYNCED_PATH_REQUIRED"):
        runner.run_observation_cycles(database, tmp_path / "OneDrive" / "archive", run_id="sync")
    with pytest.raises(ValueError, match="DATABASE_MUST_BE_SEPARATE_FROM_ARCHIVE"):
        runner.run_observation_cycles(database, tmp_path, run_id="nested")
