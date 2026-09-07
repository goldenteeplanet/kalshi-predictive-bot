from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from kalshi_predictor.ingest.settlement_hints import (
    ALLOWED_HINT_FIELDS,
    SCHEMA,
    artifact_hash,
    validate_hint_artifact,
)


def _producer():
    path = Path(__file__).parents[1] / "scripts/local/phase4y_settlement_hint_artifact.py"
    spec = importlib.util.spec_from_file_location("phase4y_settlement_hint_artifact", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _research_db(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE prospective_paired_captures(
          capture_id TEXT PRIMARY KEY,ticker TEXT,settlement_target TEXT,bundle_hash TEXT);
        CREATE TABLE prospective_pair_evaluations(capture_id TEXT PRIMARY KEY);
        INSERT INTO prospective_paired_captures VALUES(
          'capture-1','KXBTC-DUE','2026-08-25T01:00:00+00:00','bundle-1');
        INSERT INTO prospective_paired_captures VALUES(
          'capture-2','KXBTC-FUTURE','2026-08-26T01:00:00+00:00','bundle-2');
        """
    )
    connection.commit()
    connection.close()


def test_artifact_is_outcome_blind_and_validated(tmp_path: Path) -> None:
    database = tmp_path / "research.db"
    _research_db(database)
    now = datetime(2026, 8, 25, 2, tzinfo=UTC)
    payload = _producer().build(database, now=now)
    assert payload["schema"] == SCHEMA
    assert len(payload["hints"]) == 1
    hint = payload["hints"][0]
    assert set(hint) == ALLOWED_HINT_FIELDS
    forbidden = {"probability", "edge", "outcome", "settlement", "ranking", "result"}
    assert not forbidden.intersection(json.dumps(payload).lower().split('"'))
    path = tmp_path / "hints.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert validate_hint_artifact(path, now=now) == ["KXBTC-DUE"]


def test_artifact_rejects_hash_mismatch_staleness_and_future_due(tmp_path: Path) -> None:
    now = datetime(2026, 8, 25, 2, tzinfo=UTC)
    digest = hashlib.sha256(b"id").hexdigest()
    payload = {
        "schema": SCHEMA,
        "generated_at": now.isoformat(),
        "hints": [
            {
                "ticker": "KXBTC-DUE",
                "due_at": (now - timedelta(minutes=1)).isoformat(),
                "source_capture_id_hash": digest,
                "bundle_set_hash": digest,
            }
        ],
    }
    payload["artifact_hash"] = artifact_hash(payload)
    path = tmp_path / "hints.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    tampered = dict(payload)
    tampered["generated_at"] = (now - timedelta(hours=2)).isoformat()
    path.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(ValueError, match="HASH_MISMATCH"):
        validate_hint_artifact(path, now=now)
    stale = dict(payload)
    stale["generated_at"] = (now - timedelta(hours=2)).isoformat()
    stale["artifact_hash"] = artifact_hash(stale)
    path.write_text(json.dumps(stale), encoding="utf-8")
    with pytest.raises(ValueError, match="STALE_OR_FUTURE"):
        validate_hint_artifact(path, now=now)
