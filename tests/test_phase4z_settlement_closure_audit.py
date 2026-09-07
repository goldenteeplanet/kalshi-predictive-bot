from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from kalshi_predictor.ingest.settlement_hints import SCHEMA, artifact_hash


def _module():
    path = Path(__file__).parents[1] / "scripts/local/phase4z_settlement_closure_audit.py"
    spec = importlib.util.spec_from_file_location("phase4z_settlement_closure_audit", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _databases(source_path: Path, research_path: Path) -> None:
    source = sqlite3.connect(source_path)
    source.executescript(
        """
        CREATE TABLE settlements(
          ticker TEXT PRIMARY KEY, settled_at TEXT, result TEXT,
          yes_settlement_value INTEGER, updated_at TEXT);
        INSERT INTO settlements VALUES
          ('CANONICAL','2026-08-25T02:00:00+00:00','yes',100,'2026-08-25T02:01:00+00:00'),
          ('EVALUATED','2026-08-25T02:00:00+00:00','no',0,'2026-08-25T02:01:00+00:00'),
          ('PARTIAL','2026-08-25T02:00:00+00:00','yes',100,'2026-08-25T02:01:00+00:00');
        """
    )
    source.commit()
    source.close()
    research = sqlite3.connect(research_path)
    research.executescript(
        """
        CREATE TABLE prospective_paired_captures(
          capture_id TEXT PRIMARY KEY,ticker TEXT,settlement_target TEXT,bundle_hash TEXT);
        CREATE TABLE prospective_pair_evaluations(
          evaluation_id TEXT PRIMARY KEY,capture_id TEXT);
        INSERT INTO prospective_paired_captures VALUES
          ('c1','UNRESOLVED','2026-08-25T01:00:00+00:00','b1'),
          ('c2','CANONICAL','2026-08-25T01:00:00+00:00','b2'),
          ('c3','EVALUATED','2026-08-25T01:00:00+00:00','b3'),
          ('c4','PARTIAL','2026-08-25T01:00:00+00:00','b4'),
          ('c5','PARTIAL','2026-08-25T01:00:00+00:00','b5');
        INSERT INTO prospective_pair_evaluations VALUES
          ('e3','c3'),('e4','c4');
        """
    )
    research.commit()
    research.close()


def _artifact(path: Path, tickers: list[str]) -> None:
    digest = hashlib.sha256(b"lineage").hexdigest()
    payload = {
        "schema": SCHEMA,
        "generated_at": "2026-08-25T02:00:00+00:00",
        "hints": [
            {
                "ticker": ticker,
                "due_at": "2026-08-25T01:00:00+00:00",
                "source_capture_id_hash": digest,
                "bundle_set_hash": digest,
            }
            for ticker in tickers
        ],
    }
    payload["artifact_hash"] = artifact_hash(payload)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_closure_audit_classifies_every_hint_without_writing(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    research = tmp_path / "research.db"
    hints = tmp_path / "hints.json"
    _databases(source, research)
    _artifact(hints, ["UNRESOLVED", "CANONICAL", "EVALUATED", "PARTIAL", "MISSING"])
    before = (source.read_bytes(), research.read_bytes())
    result = _module().audit(
        source, research, hints, now=datetime(2026, 8, 25, 3, tzinfo=UTC)
    )
    assert result["source_mode"] == "ro/query_only"
    assert result["research_mode"] == "ro/query_only"
    assert result["status_counts"] == {
        "CANONICAL_PRESENT_AWAITING_EVALUATION": 1,
        "EVALUATED": 1,
        "HINT_NO_CAPTURE": 1,
        "PARTIAL_EVALUATION": 1,
        "UNRESOLVED_AFTER_HINT": 1,
    }
    assert result["closure_complete"] is False
    assert before == (source.read_bytes(), research.read_bytes())


def test_closure_audit_rejects_tampered_or_duplicate_hints(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    research = tmp_path / "research.db"
    hints = tmp_path / "hints.json"
    _databases(source, research)
    _artifact(hints, ["EVALUATED"])
    payload = json.loads(hints.read_text(encoding="utf-8"))
    payload["generated_at"] = "tampered"
    hints.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="HASH_MISMATCH"):
        _module().audit(source, research, hints, now=datetime.now(UTC))
    _artifact(hints, ["EVALUATED", "EVALUATED"])
    with pytest.raises(ValueError, match="DUPLICATE_TICKER"):
        _module().audit(source, research, hints, now=datetime.now(UTC))
