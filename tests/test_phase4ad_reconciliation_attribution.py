from __future__ import annotations

import importlib.util
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from kalshi_predictor.ingest.settlement_hints import SCHEMA as HINT_SCHEMA
from kalshi_predictor.ingest.settlement_hints import artifact_hash as hint_hash
from kalshi_predictor.phase4cd.reconciliation_audit import settlement_lineage_hash


def _module():
    path = Path(__file__).parents[1] / "scripts/local/phase4ad_reconciliation_attribution.py"
    spec = importlib.util.spec_from_file_location("phase4ad_reconciliation_attribution", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _capture(**overrides):
    row = {
        "capture_id": "capture-1",
        "ticker": "KXTEST-1",
        "event_ticker": "KXTEST",
        "snapshot_id": 1,
        "snapshot_timestamp": "2026-08-25T18:00:00+00:00",
        "snapshot_hash": "a" * 64,
        "bundle_hash": "b" * 64,
        "feature_ids_json": '["f1"]',
        "feature_hashes_json": '{"f1":"c"}',
        "source_observations_json": '{"source":"test"}',
        "model_versions_json": '{"crypto":"v1"}',
        "market_probability": "0.40",
        "crypto_probability": "0.60",
        "best_yes_bid": "0.39",
        "best_yes_ask": "0.41",
    }
    row.update(overrides)
    return row


def _settlement(**overrides):
    row = {
        "ticker": "KXTEST-1",
        "settled_at": "2026-08-25T19:00:00+00:00",
        "result": "yes",
        "yes_settlement_value": 100,
        "raw_json": "{}",
        "updated_at": "2026-08-25T19:01:00+00:00",
    }
    row.update(overrides)
    return row


@pytest.mark.parametrize(
    ("capture_overrides", "settlement_overrides", "expected"),
    [
        ({}, {}, "READY_FOR_RECONCILIATION"),
        ({"snapshot_hash": "bad"}, {}, "CAPTURE_LINEAGE_INCOMPLETE"),
        ({}, {"result": "unknown"}, "SETTLEMENT_RESULT_UNUSABLE"),
        ({}, {"settled_at": None}, "SETTLEMENT_RESULT_UNUSABLE"),
        ({"market_probability": "1.2"}, {}, "PROBABILITY_INPUT_INVALID"),
        ({"crypto_probability": "nan"}, {}, "PROBABILITY_INPUT_INVALID"),
        ({"best_yes_ask": None}, {}, "EXECUTABLE_EVIDENCE_INCOMPLETE"),
    ],
)
def test_terminal_input_classifications(capture_overrides, settlement_overrides, expected):
    module = _module()
    result, _, _ = module._classify(
        _capture(**capture_overrides),
        _settlement(**settlement_overrides),
        None,
        stale=False,
    )
    assert result == expected


def test_already_evaluated_race_and_settlement_conflict() -> None:
    module = _module()
    settlement = _settlement()
    matching = {"settlement_hash": settlement_lineage_hash(settlement)}
    result, _, _ = module._classify(_capture(), settlement, matching, stale=False)
    assert result == "ALREADY_EVALUATED"
    conflict = {"settlement_hash": "f" * 64}
    result, _, _ = module._classify(_capture(), settlement, conflict, stale=False)
    assert result == "SETTLEMENT_LINEAGE_CONFLICT"


def test_stale_threshold_is_inclusive_and_timezone_safe() -> None:
    module = _module()
    result, reasons, _ = module._classify(_capture(), _settlement(), None, stale=True)
    assert result == "SOURCE_ARTIFACT_STALE"
    assert reasons == ["MAXIMUM_AGE_THRESHOLD_REACHED"]
    assert module._utc("2026-08-25T13:00:00-05:00") == datetime(2026, 8, 25, 18, tzinfo=UTC)


def _fixture_files(tmp_path: Path):
    module = _module()
    source_db, research_db = tmp_path / "source.db", tmp_path / "research.db"
    source = sqlite3.connect(source_db)
    source.execute(
        """
        CREATE TABLE settlements(
          ticker TEXT PRIMARY KEY,settled_at TEXT,result TEXT,yes_settlement_value INTEGER,
          raw_json TEXT,updated_at TEXT)
        """
    )
    source.execute("INSERT INTO settlements VALUES(?,?,?,?,?,?)", tuple(_settlement().values()))
    source.commit()
    source.close()
    research = sqlite3.connect(research_db)
    research.executescript(
        """
        CREATE TABLE prospective_paired_captures(
          capture_id TEXT PRIMARY KEY,run_id TEXT,ticker TEXT,event_ticker TEXT,
          series_ticker TEXT,snapshot_id INTEGER,snapshot_timestamp TEXT,snapshot_hash TEXT,
          feature_ids_json TEXT,feature_hashes_json TEXT,source_observations_json TEXT,
          market_probability TEXT,crypto_probability TEXT,model_versions_json TEXT,
          best_yes_bid TEXT,best_yes_ask TEXT,spread TEXT,liquidity TEXT,
          settlement_target TEXT,bundle_hash TEXT,latency_json TEXT,persisted_at TEXT,
          comparator_lineage_json TEXT,range_comparator_verdict_json TEXT);
        CREATE TABLE prospective_pair_evaluations(
          evaluation_id TEXT PRIMARY KEY,capture_id TEXT,settlement_hash TEXT);
        """
    )
    capture = _capture()
    research.execute(
        """
        INSERT INTO prospective_paired_captures(
          capture_id,run_id,ticker,event_ticker,series_ticker,snapshot_id,snapshot_timestamp,
          snapshot_hash,feature_ids_json,feature_hashes_json,source_observations_json,
          market_probability,crypto_probability,model_versions_json,best_yes_bid,best_yes_ask,
          spread,liquidity,settlement_target,bundle_hash,latency_json,persisted_at,
          comparator_lineage_json,range_comparator_verdict_json)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            capture["capture_id"],
            "run-1",
            capture["ticker"],
            capture["event_ticker"],
            "KXTEST",
            capture["snapshot_id"],
            capture["snapshot_timestamp"],
            capture["snapshot_hash"],
            capture["feature_ids_json"],
            capture["feature_hashes_json"],
            capture["source_observations_json"],
            capture["market_probability"],
            capture["crypto_probability"],
            capture["model_versions_json"],
            capture["best_yes_bid"],
            capture["best_yes_ask"],
            "0.02",
            "100",
            "2026-08-25T19:00:00+00:00",
            capture["bundle_hash"],
            "{}",
            "2026-08-25T18:01:00+00:00",
            None,
            None,
        ),
    )
    research.commit()
    research.close()
    hint_path = tmp_path / "hints.json"
    hint = {
        "schema": HINT_SCHEMA,
        "generated_at": "2026-08-25T19:00:00+00:00",
        "hints": [
            {
                "ticker": "KXTEST-1",
                "due_at": "2026-08-25T19:00:00+00:00",
                "source_capture_id_hash": "c" * 64,
                "bundle_set_hash": "d" * 64,
            }
        ],
    }
    hint["artifact_hash"] = hint_hash(hint)
    hint_path.write_text(json.dumps(hint), encoding="utf-8")
    return module, source_db, research_db, hint_path, hint


def _history(tmp_path: Path, module, hint, generated_at: datetime) -> Path:
    path = Path(__file__).parents[1] / "scripts/local/phase4ac_artifact_history.py"
    spec = importlib.util.spec_from_file_location("phase4ac_test_helper", path)
    assert spec and spec.loader
    history_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(history_module)
    gate = {
        "schema": "phase4aa.settlement-closure-gate.v1",
        "generated_at": generated_at.isoformat(),
        "state": "WAITING_RECONCILIATION",
        "safe_to_advance": False,
        "production_database_written": False,
        "trading_mode_changed": False,
        "source_rows_hash": "e" * 64,
        "source_hint_artifact_hash": hint["artifact_hash"],
        "hint_count": 1,
        "canonical_count": 1,
        "fully_evaluated_count": 0,
        "blocking_counts": {},
    }
    gate["artifact_hash"] = history_module._artifact_hash(gate)
    gate_path = tmp_path / "gate.json"
    gate_path.write_text(json.dumps(gate), encoding="utf-8")
    history_dir = tmp_path / "history"
    history_module.append(history_dir, gate_path, kind="PHASE4AA_GATE", retention=10)
    return history_dir


def test_end_to_end_ready_hashes_atomic_and_database_bytes_unchanged(tmp_path: Path) -> None:
    module, source_db, research_db, hint_path, hint = _fixture_files(tmp_path)
    now = datetime(2026, 8, 25, 19, 10, tzinfo=UTC)
    history = _history(tmp_path, module, hint, now - timedelta(seconds=10))
    before = source_db.read_bytes(), research_db.read_bytes()
    first = module.audit(
        source_db, research_db, hint_path, history, now=now, maximum_age_seconds=60
    )
    second = module.audit(
        source_db, research_db, hint_path, history, now=now, maximum_age_seconds=60
    )
    assert first["classification_counts"] == {"READY_FOR_RECONCILIATION": 1}
    assert first["safe_to_reconcile"] is True
    assert first["rows_hash"] == second["rows_hash"]
    assert first["artifact_hash"] == second["artifact_hash"]
    output = tmp_path / "status.json"
    module.write_atomic(output, first)
    assert json.loads(output.read_text(encoding="utf-8")) == first
    assert before == (source_db.read_bytes(), research_db.read_bytes())


def test_stale_gate_boundary_and_tampered_manifest(tmp_path: Path) -> None:
    module, source_db, research_db, hint_path, hint = _fixture_files(tmp_path)
    now = datetime(2026, 8, 25, 19, 10, tzinfo=UTC)
    history = _history(tmp_path, module, hint, now - timedelta(seconds=60))
    result = module.audit(
        source_db, research_db, hint_path, history, now=now, maximum_age_seconds=60
    )
    assert result["classification_counts"] == {"SOURCE_ARTIFACT_STALE": 1}
    manifest_path = history / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["total_entries"] = 99
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="MANIFEST_HASH_MISMATCH"):
        module.audit(
            source_db,
            research_db,
            hint_path,
            history,
            now=now,
            maximum_age_seconds=60,
        )


def test_unknown_row_error_fails_closed(tmp_path: Path, monkeypatch) -> None:
    module, source_db, research_db, hint_path, hint = _fixture_files(tmp_path)
    now = datetime(2026, 8, 25, 19, 10, tzinfo=UTC)
    history = _history(tmp_path, module, hint, now)
    monkeypatch.setattr(module, "_classify", lambda *args, **kwargs: 1 / 0)
    result = module.audit(
        source_db, research_db, hint_path, history, now=now, maximum_age_seconds=60
    )
    assert result["classification_counts"] == {"UNKNOWN_BLOCKER": 1}
    assert result["safe_to_reconcile"] is False
