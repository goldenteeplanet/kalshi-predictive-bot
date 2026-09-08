# ruff: noqa: E501 - compact SQL fixtures are clearer as complete rows.

from __future__ import annotations

import importlib.util
import sqlite3
from datetime import UTC, datetime
from pathlib import Path


def _module():
    path = Path(__file__).parents[1] / "scripts/local/phase4x_settlement_lag_audit.py"
    spec = importlib.util.spec_from_file_location("phase4x_settlement_lag_audit", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_audit_separates_not_due_unresolved_and_canonical(tmp_path: Path) -> None:
    source_path = tmp_path / "source.db"
    research_path = tmp_path / "research.db"
    source = sqlite3.connect(source_path)
    source.executescript(
        """
        CREATE TABLE markets(ticker TEXT PRIMARY KEY,status TEXT,result TEXT,close_time TEXT,
          expected_expiration_time TEXT,expiration_time TEXT,settlement_ts TEXT,last_seen_at TEXT);
        CREATE TABLE settlements(ticker TEXT PRIMARY KEY,settled_at TEXT,result TEXT,
          yes_settlement_value TEXT,updated_at TEXT,raw_json TEXT);
        INSERT INTO markets VALUES('DUE','active',NULL,'2026-08-25T01:00:00+00:00',NULL,NULL,NULL,'2026-08-25T00:00:00+00:00');
        INSERT INTO markets VALUES('CANON','settled','yes','2026-08-25T01:00:00+00:00',NULL,NULL,'2026-08-25T01:05:00+00:00','2026-08-25T01:05:00+00:00');
        INSERT INTO settlements VALUES('CANON','2026-08-25T01:05:00+00:00','yes','1','2026-08-25T01:05:00+00:00','{}');
        """
    )
    source.commit()
    research = sqlite3.connect(research_path)
    research.executescript(
        """
        CREATE TABLE prospective_paired_captures(capture_id TEXT PRIMARY KEY,ticker TEXT,event_ticker TEXT,
          series_ticker TEXT,snapshot_timestamp TEXT,settlement_target TEXT,bundle_hash TEXT);
        CREATE TABLE prospective_pair_evaluations(capture_id TEXT PRIMARY KEY);
        INSERT INTO prospective_paired_captures VALUES('1','DUE','KXBTC-E1','KXBTC','2026-08-25T00:00:00+00:00','2026-08-25T01:00:00+00:00','h1');
        INSERT INTO prospective_paired_captures VALUES('2','CANON','KXBTC-E2','KXBTC','2026-08-25T00:00:00+00:00','2026-08-25T01:00:00+00:00','h2');
        INSERT INTO prospective_paired_captures VALUES('3','FUTURE','KXBTC-E3','KXBTC','2026-08-25T00:00:00+00:00','2026-08-26T01:00:00+00:00','h3');
        """
    )
    research.commit()
    result = _module().audit(
        source_path,
        research_path,
        now=datetime(2026, 8, 25, 2, tzinfo=UTC),
    )
    assert result["source_mode"] == "ro/query_only"
    assert result["reason_counts"] == {
        "DUE_API_UNRESOLVED": 1,
        "DUE_CANONICAL_PRESENT_AWAITING_RECONCILE": 1,
        "NOT_DUE": 1,
    }
