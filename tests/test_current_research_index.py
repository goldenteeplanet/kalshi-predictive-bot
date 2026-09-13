import hashlib
import json
import sqlite3
from datetime import timedelta

import pytest
from test_current_research_store import NOW, evaluation_payload, payload

from kalshi_predictor.overnight_paper import current_research_index as module
from kalshi_predictor.overnight_paper.current_research_store import (
    append_current_record,
    read_current_records,
)
from kalshi_predictor.overnight_paper.store import digest, encode


def mission(tmp_path, count=3, lifecycle=False):
    path = tmp_path / "mission.db"
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute(
            "CREATE TABLE overnight_sprint_cycles "
            "(id TEXT PRIMARY KEY,captured_at TEXT,payload TEXT)"
        )
        db.execute("BEGIN")
        for i in range(count):
            append_current_record(
                db, kind="ASSESSMENT", identity=str(i), payload=payload(), recorded_at=NOW
            )
        if lifecycle:
            p = evaluation_payload()
            d = p["decision"]
            append_current_record(
                db,
                kind="PROSPECTIVE_SHADOW",
                identity=d["decision_id"],
                payload={**d, "execution_authority": False},
                recorded_at=NOW,
            )
            append_current_record(
                db,
                kind="EVALUATION",
                identity=d["decision_id"],
                payload=p,
                recorded_at=NOW + timedelta(hours=3),
            )
    return path


def test_valid_legacy_parity_compact_index_and_no_payload_list(tmp_path):
    path = mission(tmp_path, 70, lifecycle=True)
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    with sqlite3.connect(path) as db:
        expected = read_current_records(db)
    result = module.validate_research_index(path, tmp_path / "index.db")
    assert result.status == "COMPLETE_VALIDATED_SNAPSHOT"
    assert result.record_count == len(expected) == 72
    assert not hasattr(result, "records")
    with sqlite3.connect(result.index_path) as index:
        rows = index.execute("SELECT id,kind FROM records ORDER BY id").fetchall()
        assert rows == sorted((e["journal_id"], e["record_kind"]) for e in expected)
        columns = {r[1] for r in index.execute("PRAGMA table_info(records)")}
        assert not columns & {"payload", "record", "original"}
        assert index.execute("SELECT status FROM completion").fetchone()[0] == result.status
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def rewrite(db, key, edit):
    raw = db.execute("SELECT payload FROM overnight_sprint_cycles WHERE id=?", (key,)).fetchone()[0]
    value = json.loads(raw)
    edit(value)
    value["payload_sha256"] = digest(value["record"])
    new_key = (
        "current-research-v1:"
        + value["record_kind"].lower()
        + ":"
        + digest({"identity": value["identity"]})
    )
    db.execute(
        "UPDATE overnight_sprint_cycles SET id=?,payload=? WHERE id=?",
        (new_key, encode(value), key),
    )


@pytest.mark.parametrize(
    "fault", ["hash", "malformed", "missing_parent", "duplicate", "late_parent"]
)
def test_fault_after_many_rows_never_publishes_complete_and_removes_owned_index(tmp_path, fault):
    path = mission(tmp_path, 70, lifecycle=True)
    with sqlite3.connect(path) as db:
        shadow = db.execute(
            "SELECT id FROM overnight_sprint_cycles " "WHERE id LIKE '%:prospective_shadow:%'"
        ).fetchone()[0]
        evaluation = db.execute(
            "SELECT id FROM overnight_sprint_cycles " "WHERE id LIKE '%:evaluation:%'"
        ).fetchone()[0]
        if fault == "hash":
            db.execute(
                "UPDATE overnight_sprint_cycles SET payload=replace(payload,'UNCERTAINTY',"
                "'WRONG') WHERE id LIKE '%:assessment:%'"
            )
        elif fault == "malformed":
            db.execute("UPDATE overnight_sprint_cycles SET payload='{' WHERE id=?", (evaluation,))
        elif fault == "missing_parent":
            db.execute("DELETE FROM overnight_sprint_cycles WHERE id=?", (shadow,))
        elif fault == "duplicate":
            # A second semantic shadow with a valid envelope key but wrong identity.
            raw = db.execute(
                "SELECT payload FROM overnight_sprint_cycles WHERE id=?", (shadow,)
            ).fetchone()[0]
            value = json.loads(raw)
            value["identity"] = "alias"
            key = "current-research-v1:prospective_shadow:" + digest({"identity": "alias"})
            db.execute(
                "INSERT INTO overnight_sprint_cycles VALUES(?,?,?)",
                (key, NOW.isoformat(), encode(value)),
            )
        else:
            # Equal parent/child journal clocks pass store linking but not the explicit new rule.
            raw = db.execute(
                "SELECT payload FROM overnight_sprint_cycles WHERE id=?", (evaluation,)
            ).fetchone()[0]
            value = json.loads(raw)
            at = json.loads(
                db.execute(
                    "SELECT payload FROM overnight_sprint_cycles WHERE id=?", (shadow,)
                ).fetchone()[0]
            )["recorded_at"]
            # Keep valid payload evaluation clock by moving both to a pretarget decision example
            # is impossible here; instead child time before evaluated time must also fail closed.
            value["recorded_at"] = at
            db.execute(
                "UPDATE overnight_sprint_cycles SET captured_at=?,payload=? WHERE id=?",
                (at, encode(value), evaluation),
            )
    target = tmp_path / "index.db"
    with pytest.raises((ValueError, sqlite3.IntegrityError)):
        module.validate_research_index(path, target)
    assert not target.exists() and not list(tmp_path.glob("index.db-*"))


def test_exact_aggregate_limits_and_one_over(tmp_path):
    path = mission(tmp_path, 2)
    with sqlite3.connect(path) as db:
        size = db.execute(
            "SELECT sum(length(CAST(payload AS BLOB))) FROM overnight_sprint_cycles"
        ).fetchone()[0]
    assert (
        module.validate_research_index(
            path, tmp_path / "ok", max_records=2, max_bytes=size
        ).payload_bytes
        == size
    )
    for options in ({"max_records": 1}, {"max_bytes": size - 1}):
        with pytest.raises(ValueError, match="AGGREGATE"):
            module.validate_research_index(path, tmp_path / "bad", **options)
        assert not (tmp_path / "bad").exists()


def test_existing_destination_preserved_and_bad_bounds_refuse(tmp_path):
    path = mission(tmp_path)
    target = tmp_path / "index"
    target.write_bytes(b"preserve")
    with pytest.raises(ValueError, match="EXCLUSIVE"):
        module.validate_research_index(path, target)
    assert target.read_bytes() == b"preserve"
    for options in ({"max_records": 10001}, {"max_bytes": 32000001}, {"timeout_seconds": 61}):
        with pytest.raises(ValueError, match="BOUNDS"):
            module.validate_research_index(path, target, **options)


def test_same_snapshot_ignores_concurrent_append_until_next_pass(tmp_path, monkeypatch):
    path = mission(tmp_path, 2)
    original = module._read_envelope
    injected = False

    def replay(*args):
        nonlocal injected
        if not injected:
            injected = True
            with sqlite3.connect(path) as db:
                db.execute("BEGIN")
                append_current_record(
                    db, kind="ASSESSMENT", identity="later", payload=payload(), recorded_at=NOW
                )
        return original(*args)

    monkeypatch.setattr(module, "_read_envelope", replay)
    assert module.validate_research_index(path, tmp_path / "first").record_count == 2
    assert module.validate_research_index(path, tmp_path / "second").record_count == 3


def test_failure_mid_replay_no_index_and_mission_unchanged(tmp_path, monkeypatch):
    path = mission(tmp_path, 70)
    before = path.read_bytes()
    original = module._read_envelope
    calls = 0

    def replay(*args):
        nonlocal calls
        calls += 1
        if calls == 66:
            raise RuntimeError("injected interruption")
        return original(*args)

    monkeypatch.setattr(module, "_read_envelope", replay)
    with pytest.raises(RuntimeError, match="interruption"):
        module.validate_research_index(path, tmp_path / "index")
    assert not (tmp_path / "index").exists() and path.read_bytes() == before


def test_oversize_length_rejected_before_replay(tmp_path, monkeypatch):
    path = mission(tmp_path, 1)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE overnight_sprint_cycles SET payload=?", ("x" * 8000001,))

    def forbidden(*args):
        raise AssertionError("must not decode oversized record")

    monkeypatch.setattr(module, "_read_envelope", forbidden)
    with pytest.raises(ValueError, match="RECORD_BOUND"):
        module.validate_research_index(path, tmp_path / "index")


def test_duplicate_observation_across_history_rejected_even_with_alias_identity(tmp_path):
    path = mission(tmp_path, 70, lifecycle=True)
    p = evaluation_payload()
    d, e = p["decision"], p["evaluation"]
    record = dict(
        decision_id=d["decision_id"],
        state="FINAL",
        official_original_json=e["official_original_json"],
        official_receipt_json=e["official_receipt_json"],
        observed_at=e["evaluated_at"],
        paper_eligible=False,
        execution_authority=False,
    )
    with sqlite3.connect(path) as db:
        db.execute("BEGIN")
        for identity in ("first", "alias"):
            append_current_record(
                db,
                kind="SHADOW_OBSERVATION",
                identity=identity,
                payload=record,
                recorded_at=NOW + timedelta(hours=3),
            )
        assert len(read_current_records(db)) == 74
    with pytest.raises(sqlite3.IntegrityError):
        module.validate_research_index(path, tmp_path / "index")
    assert not (tmp_path / "index").exists()


def test_timeout_during_replay_cleans_incomplete_result(tmp_path, monkeypatch):
    path = mission(tmp_path, 70)
    clock = [0.0]
    original = module._read_envelope
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])

    def replay(*args):
        clock[0] += 2
        return original(*args)

    monkeypatch.setattr(module, "_read_envelope", replay)
    with pytest.raises((ValueError, sqlite3.OperationalError)):
        module.validate_research_index(path, tmp_path / "index", timeout_seconds=1)
    assert not (tmp_path / "index").exists()


def test_later_sorted_oversized_parent_refused_before_linked_fetch(tmp_path, monkeypatch):
    path = mission(tmp_path, 0, lifecycle=True)
    with sqlite3.connect(path) as db:
        db.execute(
            "UPDATE overnight_sprint_cycles SET captured_at=?,payload=? "
            "WHERE id LIKE '%:prospective_shadow:%'",
            ((NOW + timedelta(hours=4)).isoformat(), "x" * 8000001),
        )
    replay = module._read_envelope
    kinds = []

    def checked_replay(*args):
        result = replay(*args)
        kinds.append(result["record_kind"])
        return result

    def forbidden_linked_fetch(*args):
        raise AssertionError("existing linked helper fetches parent payload; must not be called")

    monkeypatch.setattr(module, "_read_envelope", checked_replay)
    monkeypatch.setattr(module, "_linked_shadow", forbidden_linked_fetch)
    with pytest.raises(ValueError, match="INDEX_LINKED_RECORD_BOUND_EXCEEDED"):
        module.validate_research_index(path, tmp_path / "index")
    assert kinds == ["EVALUATION"]
    assert not (tmp_path / "index").exists()


@pytest.mark.parametrize("offset", [-1, 0, 1])
def test_index_clock_invariant_directly_uses_normalized_parent_time(offset):
    # Valid per-record observation/evaluation clocks already imply target ordering.
    # Exercise the additional compact-index defense directly, without pretending
    # an earlier malformed payload reached this exact check.
    with sqlite3.connect(":memory:") as index:
        index.execute(
            "CREATE TABLE records(id TEXT,kind TEXT,semantic_id TEXT,parent TEXT,at TEXT)"
        )
        index.execute(
            "INSERT INTO records VALUES(?,?,?,?,?)",
            ("parent", "PROSPECTIVE_SHADOW", "d", None, NOW.isoformat()),
        )
        index.execute(
            "INSERT INTO records VALUES(?,?,?,?,?)",
            ("child", "EVALUATION", "d", "d", (NOW + timedelta(seconds=offset)).isoformat()),
        )
        if offset <= 0:
            with pytest.raises(ValueError, match="INDEX_STRICT_PRIOR_SHADOW_REQUIRED"):
                module._validate_index_links(index)
        else:
            module._validate_index_links(index)
