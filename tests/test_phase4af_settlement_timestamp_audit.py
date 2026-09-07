from __future__ import annotations

import importlib.util
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _module():
    return _load(
        "phase4af_tested",
        Path(__file__).parents[1] / "scripts/local/phase4af_settlement_timestamp_audit.py",
    )


def _ad_helper():
    return _load(
        "phase4ad_fixture_for_4af",
        Path(__file__).with_name("test_phase4ad_reconciliation_attribution.py"),
    )


def _ae_module():
    return _load(
        "phase4ae_for_4af",
        Path(__file__).parents[1] / "scripts/local/phase4ae_reconciliation_plan.py",
    )


def _fixture(tmp_path: Path, *, settlement_updates: dict | None = None):
    ad_helper = _ad_helper()
    ad, source_db, research_db, hint_path, hint = ad_helper._fixture_files(tmp_path)
    connection = sqlite3.connect(source_db)
    connection.execute(
        "CREATE TABLE markets(ticker TEXT PRIMARY KEY,close_time TEXT,"
        "expected_expiration_time TEXT,expiration_time TEXT)"
    )
    connection.execute(
        "INSERT INTO markets VALUES(?,?,?,?)",
        (
            "KXTEST-1",
            "2026-08-25T18:30:00+00:00",
            "2026-08-25T18:45:00+00:00",
            "2026-08-25T19:00:00+00:00",
        ),
    )
    updates = {"settled_at": None, **(settlement_updates or {})}
    for key, value in updates.items():
        connection.execute(f"UPDATE settlements SET {key}=?", (value,))
    connection.commit()
    connection.close()
    now = datetime(2026, 8, 25, 19, 10, tzinfo=UTC)
    history = ad_helper._history(tmp_path, ad, hint, now - timedelta(seconds=10))
    attribution = ad.audit(
        source_db, research_db, hint_path, history, now=now, maximum_age_seconds=60
    )
    ad_path = tmp_path / "phase4ad.json"
    ad_path.write_text(json.dumps(attribution), encoding="utf-8")
    ae = _ae_module()
    plan = ae.plan(
        source_db,
        research_db,
        ad_path,
        history,
        hint_path,
        now=now,
        maximum_age_seconds=60,
    )
    ae_path = tmp_path / "phase4ae.json"
    ae_path.write_text(json.dumps(plan), encoding="utf-8")
    return _module(), source_db, history, ad_path, ae_path, now


def _audit(fixture, **overrides):
    module, source_db, history, ad_path, ae_path, now = fixture
    return module.audit(
        source_db,
        ad_path,
        ae_path,
        history,
        now=overrides.get("now", now),
        freshness_seconds=overrides.get("freshness_seconds", 3600),
        evidence_dir=overrides.get("evidence_dir"),
    )


@pytest.mark.parametrize(
    ("source_type", "timestamp", "fresh", "result", "expected", "ready"),
    [
        (
            "CANONICAL_SETTLEMENT",
            "2026-08-25T19:00:00Z",
            True,
            "yes",
            "CANONICAL_SETTLED_AT_PRESENT",
            False,
        ),
        (
            "EXCHANGE_SETTLEMENT_TIMESTAMP",
            "2026-08-25T19:00:00Z",
            True,
            "yes",
            "AUTHORITATIVE_EXCHANGE_TIMESTAMP_AVAILABLE",
            True,
        ),
        (
            "VALIDATED_SETTLEMENT_ARTIFACT",
            "2026-08-25T19:00:00Z",
            True,
            "yes",
            "VALIDATED_ARTIFACT_TIMESTAMP_AVAILABLE",
            True,
        ),
        (
            "EXCHANGE_SETTLEMENT_TIMESTAMP",
            "2026-08-25 19:00:00",
            True,
            "yes",
            "TIMEZONE_AMBIGUOUS",
            False,
        ),
        ("EXCHANGE_SETTLEMENT_TIMESTAMP", "bad", True, "yes", "TIMEZONE_AMBIGUOUS", False),
        (
            "EXCHANGE_SETTLEMENT_TIMESTAMP",
            "2026-08-25T19:00:00Z",
            False,
            "yes",
            "SOURCE_STALE",
            False,
        ),
        (
            "EXCHANGE_SETTLEMENT_TIMESTAMP",
            "2026-08-25T19:00:00Z",
            True,
            "pending",
            "NOT_ACTUALLY_SETTLED",
            False,
        ),
    ],
)
def test_pure_precedence_classifications(source_type, timestamp, fresh, result, expected, ready):
    module = _module()
    candidate = module._candidate(
        source_type, "record", timestamp, authority="authoritative", provenance={"x": 1}
    )
    disposition, flags, _ = module.classify_timestamp_evidence(
        settlement_result=result,
        candidates=[candidate],
        source_fresh=fresh,
        lineage_valid=True,
        source_present=True,
        existing_evaluation=False,
    )
    assert disposition == expected
    assert flags["ready_for_future_canonicalization"] is ready


def test_context_timestamps_are_not_promoted_and_database_is_unchanged(tmp_path: Path):
    fixture = _fixture(tmp_path)
    source_db = fixture[1]
    before = source_db.read_bytes()
    result = _audit(fixture)
    row = result["rows"][0]
    assert result["classification_counts"] == {"RESULT_PRESENT_TIMESTAMP_MISSING": 1}
    assert {item["authority"] for item in row["evidence_inventory"]} == {"non-authoritative"}
    assert {item["source_type"] for item in row["evidence_inventory"]} >= {
        "MARKET_CLOSE_TIME",
        "MARKET_EXPECTED_EXPIRATION_TIME",
        "MARKET_EXPIRATION_TIME",
        "SETTLEMENT_UPDATED_AT",
    }
    assert source_db.read_bytes() == before
    assert result["safe_for_canonicalization"] is False


def test_exchange_timestamp_available_and_timezone_normalized(tmp_path: Path):
    fixture = _fixture(
        tmp_path,
        settlement_updates={"raw_json": json.dumps({"settlement_ts": "2026-08-25T14:00:00-05:00"})},
    )
    result = _audit(fixture)
    assert result["classification_counts"] == {"AUTHORITATIVE_EXCHANGE_TIMESTAMP_AVAILABLE": 1}
    assert result["ready_count"] == 1
    assert result["rows"][0]["normalized_timestamp_candidate"] == "2026-08-25T19:00:00+00:00"


def test_authoritative_conflict(tmp_path: Path):
    module = _module()
    candidates = [
        module._candidate(
            "EXCHANGE_SETTLEMENT_TIMESTAMP",
            "a",
            "2026-08-25T19:00:00Z",
            authority="authoritative",
            provenance="a",
        ),
        module._candidate(
            "VALIDATED_SETTLEMENT_ARTIFACT",
            "b",
            "2026-08-25T19:01:00Z",
            authority="authoritative",
            provenance="b",
        ),
    ]
    disposition, flags, _ = module.classify_timestamp_evidence(
        settlement_result="yes",
        candidates=candidates,
        source_fresh=True,
        lineage_valid=True,
        source_present=True,
        existing_evaluation=False,
    )
    assert disposition == "TIMESTAMP_CONFLICT"
    assert flags["timestamp_conflict_present"] is True


def test_equivalent_authoritative_timestamps_do_not_conflict():
    module = _module()
    candidates = [
        module._candidate(
            "EXCHANGE_SETTLEMENT_TIMESTAMP",
            "a",
            "2026-08-25T19:00:00Z",
            authority="authoritative",
            provenance="a",
        ),
        module._candidate(
            "VALIDATED_SETTLEMENT_ARTIFACT",
            "b",
            "2026-08-25T14:00:00-05:00",
            authority="authoritative",
            provenance="b",
        ),
    ]
    disposition, flags, chosen = module.classify_timestamp_evidence(
        settlement_result="yes",
        candidates=candidates,
        source_fresh=True,
        lineage_valid=True,
        source_present=True,
        existing_evaluation=False,
    )
    assert disposition == "AUTHORITATIVE_EXCHANGE_TIMESTAMP_AVAILABLE"
    assert flags["timestamp_conflict_present"] is False
    assert chosen["normalized_utc_timestamp"] == "2026-08-25T19:00:00+00:00"


@pytest.mark.parametrize(
    ("offset_microseconds", "expected"),
    [(-1, True), (0, False), (1, False)],
)
def test_freshness_boundary(offset_microseconds: int, expected: bool):
    module = _module()
    reference = datetime(2026, 8, 25, 19, 0, tzinfo=UTC)
    now = reference + timedelta(seconds=60, microseconds=offset_microseconds)
    age, fresh = module.source_freshness(now, reference, 60)
    assert age == 60 + offset_microseconds / 1_000_000
    assert fresh is expected


def test_validated_artifact_timestamp(tmp_path: Path):
    fixture = _fixture(tmp_path)
    module = fixture[0]
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    rows = [
        {
            "ticker": "KXTEST-1",
            "settlement_timestamp": "2026-08-25T19:00:00Z",
            "source_record_identity": "exchange-report:1",
        }
    ]
    payload = {"schema": module.EVIDENCE_SCHEMA, "rows_hash": canonical_hash(rows), "rows": rows}
    payload["artifact_hash"] = module.artifact_hash(payload)
    (evidence_dir / "evidence.json").write_text(json.dumps(payload), encoding="utf-8")
    result = _audit(fixture, evidence_dir=evidence_dir)
    assert result["classification_counts"] == {"VALIDATED_ARTIFACT_TIMESTAMP_AVAILABLE": 1}


def test_tampering_unknown_duplicate_and_missing_sources_fail_closed(tmp_path: Path):
    fixture = _fixture(tmp_path)
    ad_path = fixture[3]
    original = json.loads(ad_path.read_text(encoding="utf-8"))
    tampered = dict(original)
    tampered["ready_count"] = 99
    ad_path.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(ValueError, match="PHASE4AD_HASH_MISMATCH"):
        _audit(fixture)
    duplicate = dict(original)
    duplicate["rows"] = original["rows"] * 2
    duplicate["rows_hash"] = canonical_hash(duplicate["rows"])
    duplicate["artifact_hash"] = fixture[0].artifact_hash(duplicate)
    ad_path.write_text(json.dumps(duplicate), encoding="utf-8")
    with pytest.raises(ValueError, match="DUPLICATE|LINEAGE_MISMATCH"):
        _audit(fixture)


def test_atomic_output_refusal_replacement_and_cleanup(tmp_path: Path):
    fixture = _fixture(tmp_path)
    module, result = fixture[0], _audit(fixture)
    output = tmp_path / "audit.json"
    module.write_atomic(output, result)
    first = output.read_bytes()
    with pytest.raises(FileExistsError):
        module.write_atomic(output, result)
    assert output.read_bytes() == first
    module.write_atomic(output, result, replace=True)
    assert not list(tmp_path.glob(".audit.json.*.tmp"))
    assert json.loads(output.read_text(encoding="utf-8")) == result


def test_history_and_plan_tampering_rejected(tmp_path: Path):
    fixture = _fixture(tmp_path)
    ae_path = fixture[4]
    payload = json.loads(ae_path.read_text(encoding="utf-8"))
    payload["planned_row_count"] = 42
    ae_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="PHASE4AE_HASH_MISMATCH"):
        _audit(fixture)


def test_empty_input_is_valid_artifact_but_fail_closed(tmp_path: Path):
    fixture = _fixture(tmp_path)
    module, ad_path, ae_path = fixture[0], fixture[3], fixture[4]
    ad = json.loads(ad_path.read_text(encoding="utf-8"))
    ad["rows"] = []
    ad["rows_hash"] = canonical_hash([])
    ad["artifact_hash"] = module.artifact_hash(ad)
    ad_path.write_text(json.dumps(ad), encoding="utf-8")
    ae = json.loads(ae_path.read_text(encoding="utf-8"))
    ae["rows"] = []
    ae["rows_hash"] = canonical_hash([])
    ae["source_phase4ad_artifact_hash"] = ad["artifact_hash"]
    ae["artifact_hash"] = module.artifact_hash(ae)
    ae_path.write_text(json.dumps(ae), encoding="utf-8")
    result = _audit(fixture)
    assert result["input_row_count"] == 0
    assert result["safe_for_canonicalization"] is False
