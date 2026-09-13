import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from test_prospective_calibration import decision, official

from kalshi_predictor.overnight_paper.current_research_store import (
    append_current_record,
    read_current_records,
)

NOW = datetime(2026, 9, 13, tzinfo=UTC)


def connection():
    db = sqlite3.connect(":memory:")
    db.execute(
        "CREATE TABLE overnight_sprint_cycles(id TEXT PRIMARY KEY,captured_at TEXT,payload TEXT)"
    )
    db.execute("BEGIN IMMEDIATE")
    return db


def payload():
    return {
        "paper_eligible": False,
        "execution_authority": False,
        "assessed_at": NOW.isoformat(),
        "blocker": "UNCERTAINTY_BLOCKED",
        "ticker": "BTC-A",
        "side": "YES",
        "scan_sha256": "a" * 64,
        "scope": "CURRENT_UNCALIBRATED_RESEARCH",
    }


def test_duplicate_is_idempotent_conflict_is_rejected_and_tamper_detected():
    with connection() as db:
        kwargs = dict(kind="ASSESSMENT", identity="test", payload=payload(), recorded_at=NOW)
        first = append_current_record(db, **kwargs)
        assert append_current_record(db, **kwargs) == first
        assert len(read_current_records(db)) == 1
        with pytest.raises(ValueError, match="CONFLICT"):
            append_current_record(db, **{**kwargs, "payload": {**payload(), "blocker": "OTHER"}})
        db.execute("UPDATE overnight_sprint_cycles SET payload=replace(payload,'OTHER','X')")
        db.execute(
            "UPDATE overnight_sprint_cycles SET "
            "payload=replace(payload,'UNCERTAINTY_BLOCKED','BOOK_BLOCKED')"
        )
        with pytest.raises(ValueError, match="INTEGRITY"):
            read_current_records(db)


def test_shadow_requires_real_pre_target_persistence_and_no_execution_flag():
    with connection() as db:
        p = {**decision(), "execution_authority": False}
        append_current_record(
            db, kind="PROSPECTIVE_SHADOW", identity=p["decision_id"], payload=p, recorded_at=NOW
        )
        with pytest.raises(ValueError, match="BEFORE_TARGET"):
            append_current_record(
                db,
                kind="PROSPECTIVE_SHADOW",
                identity="b",
                payload=p,
                recorded_at=NOW + timedelta(hours=2),
            )
        with pytest.raises(ValueError, match="AUTHORIZE"):
            append_current_record(
                db,
                kind="ASSESSMENT",
                identity="c",
                payload={**payload(), "paper_eligible": True},
                recorded_at=NOW,
            )


def test_forged_shadow_probability_cannot_enter_journal():
    p = {**decision(), "execution_authority": False, "p_yes": ".99"}
    with connection() as db:
        with pytest.raises(ValueError, match="ORIGINAL_REPLAY"):
            append_current_record(
                db, kind="PROSPECTIVE_SHADOW", identity=p["decision_id"], payload=p, recorded_at=NOW
            )
        assert read_current_records(db) == []


def test_read_bound_and_timestamp_tampering_fail_closed():
    with connection() as db:
        for identity in ("a", "b"):
            append_current_record(
                db, kind="ASSESSMENT", identity=identity, payload=payload(), recorded_at=NOW
            )
        with pytest.raises(ValueError, match="READ_BOUND_EXCEEDED"):
            read_current_records(db, max_records=1)
        db.execute(
            "UPDATE overnight_sprint_cycles SET captured_at=?",
            ((NOW + timedelta(hours=1)).isoformat(),),
        )
        with pytest.raises(ValueError, match="INTEGRITY"):
            read_current_records(db)


def evaluation_payload():
    d = decision()
    e = official(d)
    return dict(
        decision=d,
        evaluation=e,
        decision_time=d["decision_time"],
        assessed_at=e["evaluated_at"],
        paper_eligible=False,
        execution_authority=False,
    )


def test_evaluation_requires_existing_exact_shadow_and_replays_official_result():
    p = evaluation_payload()
    d = p["decision"]
    at = NOW + timedelta(hours=3)
    with connection() as db:
        with pytest.raises(ValueError, match="PRIOR_SHADOW"):
            append_current_record(
                db, kind="EVALUATION", identity=d["decision_id"], payload=p, recorded_at=at
            )
        append_current_record(
            db,
            kind="PROSPECTIVE_SHADOW",
            identity=d["decision_id"],
            payload={**d, "execution_authority": False},
            recorded_at=NOW,
        )
        append_current_record(
            db, kind="EVALUATION", identity=d["decision_id"], payload=p, recorded_at=at
        )
        assert len(read_current_records(db)) == 2
        p["evaluation"]["brier"] = "0"
        with pytest.raises(ValueError, match="SCORE_MISMATCH"):
            append_current_record(
                db, kind="EVALUATION", identity=d["decision_id"], payload=p, recorded_at=at
            )


def test_final_observation_cannot_label_determined_status_as_final():
    p = evaluation_payload()
    d, e = p["decision"], p["evaluation"]
    observation = dict(
        decision_id=d["decision_id"],
        state="FINAL",
        official_original_json=e["official_original_json"],
        official_receipt_json=e["official_receipt_json"],
        observed_at=e["evaluated_at"],
        paper_eligible=False,
        execution_authority=False,
    )
    with connection() as db:
        append_current_record(
            db,
            kind="PROSPECTIVE_SHADOW",
            identity=d["decision_id"],
            payload={**d, "execution_authority": False},
            recorded_at=NOW,
        )
        append_current_record(
            db,
            kind="SHADOW_OBSERVATION",
            identity=d["decision_id"] + ":final",
            payload=observation,
            recorded_at=NOW + timedelta(hours=3),
        )
        assert len(read_current_records(db)) == 2
        import hashlib

        raw = observation["official_original_json"].replace("finalized", "determined")
        receipt = json.loads(observation["official_receipt_json"])
        receipt["source_sha256"] = hashlib.sha256(raw.encode()).hexdigest()
        observation.update(official_original_json=raw, official_receipt_json=json.dumps(receipt))
        with pytest.raises(ValueError, match="NOT_FINAL"):
            append_current_record(
                db,
                kind="SHADOW_OBSERVATION",
                identity="changed",
                payload=observation,
                recorded_at=NOW + timedelta(hours=3),
            )


def test_same_payload_later_retry_preserves_first_clock_and_never_writes_paper_table():
    with connection() as db:
        db.execute("CREATE TABLE paper_orders(id TEXT PRIMARY KEY)")
        first = append_current_record(
            db, kind="ASSESSMENT", identity="a", payload=payload(), recorded_at=NOW
        )
        assert (
            append_current_record(
                db,
                kind="ASSESSMENT",
                identity="a",
                payload=payload(),
                recorded_at=NOW + timedelta(seconds=5),
            )
            == first
        )
        assert read_current_records(db)[0]["recorded_at"] == NOW.isoformat()
        assert db.execute("SELECT count(*) FROM paper_orders").fetchone()[0] == 0
