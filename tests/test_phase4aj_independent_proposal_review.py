from __future__ import annotations

import importlib.util
import json
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _module():
    return _load(
        "phase4aj_tested",
        Path(__file__).parents[1] / "scripts/local/phase4aj_independent_proposal_review.py",
    )


def _fixture(tmp_path: Path):
    helper = _load(
        "phase4ai_fixture_for_4aj",
        Path(__file__).with_name("test_phase4ai_canonicalization_proposal.py"),
    )
    ai_fixture = helper._fixture(tmp_path)
    proposal, review = helper._build(ai_fixture)
    proposal_path = tmp_path / "phase4ai-proposal.json"
    review_path = tmp_path / "phase4ai-review.json"
    proposal_path.write_text(json.dumps(proposal), encoding="utf-8")
    review_path.write_text(json.dumps(review), encoding="utf-8")
    return {
        "module": _module(),
        "database": ai_fixture[1],
        "history": ai_fixture[2],
        "ad": ai_fixture[3],
        "ae": ai_fixture[4],
        "baseline": ai_fixture[5],
        "status": ai_fixture[6],
        "evidence": ai_fixture[7],
        "reaudit": ai_fixture[8],
        "gate": ai_fixture[9],
        "now": ai_fixture[10],
        "proposal": proposal_path,
        "review": review_path,
    }


def _build(f, **overrides):
    return f["module"].build(
        f["database"],
        f["proposal"],
        f["review"],
        f["gate"],
        f["reaudit"],
        f["baseline"],
        f["status"],
        f["evidence"],
        f["ad"],
        f["ae"],
        f["history"],
        now=overrides.get("now", f["now"]),
        approval_path=overrides.get("approval"),
        approval_clock_skew_seconds=overrides.get("skew", 300),
    )


def _rehash_proposal(f):
    m = f["module"]
    p = json.loads(f["proposal"].read_text())
    for row in p["rows"]:
        row["proposal_row_hash"] = m.canonical_hash(
            {k: v for k, v in row.items() if k != "proposal_row_hash"}
        )
    p["rows_hash"] = m.canonical_hash(p["rows"])
    p["artifact_hash"] = m._hash(p)
    f["proposal"].write_text(json.dumps(p))
    r = json.loads(f["review"].read_text())
    r["proposal_artifact_hash"] = p["artifact_hash"]
    r["proposal_rows_hash"] = p["rows_hash"]
    r["manifest_hash"] = m._hash(r, "manifest_hash")
    f["review"].write_text(json.dumps(r))
    return p, r


def _approval(f, *, approved=None, approved_at=None, expires=None, **changes):
    m = f["module"]
    p = json.loads(f["proposal"].read_text())
    r = json.loads(f["review"].read_text())
    payload = {
        "schema": m.APPROVAL_SCHEMA,
        "proposal_artifact_hash": p["artifact_hash"],
        "phase4ai_review_manifest_hash": r["manifest_hash"],
        "approved_proposal_row_hashes": approved
        if approved is not None
        else [x["proposal_row_hash"] for x in p["rows"] if x["disposition"] == "PROPOSED"],
        "reviewer_identifier": "operator:test",
        "approval_timestamp": (approved_at or f["now"]).isoformat(),
        "approval_expires_at": (expires or f["now"] + timedelta(minutes=30)).isoformat(),
        "future_separate_execution_only": True,
        "phase4aj_execution_authorized": False,
    }
    payload.update(changes)
    payload["artifact_hash"] = m._hash(payload)
    path = f["proposal"].parent / "approval.json"
    path.write_text(json.dumps(payload))
    return path


def test_valid_without_approval_awaits_human(tmp_path: Path):
    att, manifest = _build(_fixture(tmp_path))
    assert att["review_state"] == "AWAITING_HUMAN_APPROVAL"
    assert att["eligible_count"] == 1
    assert manifest["eligible_proposal_row_hashes"] == []
    assert att["execution_authorized"] is manifest["execution_authorized"] is False


def test_exactly_bound_approval_advances_without_authorizing_execution(tmp_path: Path):
    f = _fixture(tmp_path)
    att, manifest = _build(f, approval=_approval(f))
    assert att["review_state"] == "APPROVED_FOR_SEPARATELY_AUTHORIZED_EXECUTION"
    assert len(manifest["eligible_proposal_row_hashes"]) == 1
    assert manifest["execution_authorized"] is False


@pytest.mark.parametrize(
    ("delta", "state"),
    [(-1, "AWAITING_HUMAN_APPROVAL"), (0, "PROPOSAL_EXPIRED"), (1, "PROPOSAL_EXPIRED")],
)
def test_expiration_microsecond_boundary(tmp_path: Path, delta: int, state: str):
    f = _fixture(tmp_path)
    expires = f["now"] + timedelta(hours=1)
    att, _ = _build(f, now=expires + timedelta(microseconds=delta))
    assert att["review_state"] == state


@pytest.mark.parametrize(
    ("key", "message"),
    [("proposal", "PROPOSAL_HASH_MISMATCH"), ("review", "REVIEW_HASH_MISMATCH")],
)
def test_tampered_ai_artifacts_fail_closed(tmp_path: Path, key: str, message: str):
    f = _fixture(tmp_path)
    payload = json.loads(f[key].read_text())
    payload["tampered"] = True
    f[key].write_text(json.dumps(payload))
    with pytest.raises(ValueError, match=message):
        _build(f)


def test_mismatched_publication_pair_fails_closed(tmp_path: Path):
    f = _fixture(tmp_path)
    r = json.loads(f["review"].read_text())
    r["publication_pair_id"] = "wrong"
    r["manifest_hash"] = f["module"]._hash(r, "manifest_hash")
    f["review"].write_text(json.dumps(r))
    with pytest.raises(ValueError, match="LINEAGE_FAILURE_AI_PAIR"):
        _build(f)


def test_missing_history_link_fails_closed(tmp_path: Path):
    f = _fixture(tmp_path)
    manifest = json.loads((f["history"] / "manifest.json").read_text())
    (f["history"] / manifest["entries"][0]["filename"]).unlink()
    with pytest.raises(ValueError, match="HISTORY_INVALID"):
        _build(f)


def test_missing_history_manifest_fails_lineage(tmp_path: Path):
    f = _fixture(tmp_path)
    (f["history"] / "manifest.json").unlink()
    with pytest.raises(ValueError, match="LINEAGE_FAILURE_AC"):
        _build(f)


@pytest.mark.parametrize("key", ["gate", "reaudit", "status", "evidence", "ad", "ae", "baseline"])
def test_tampered_lineage_inputs_fail_closed(tmp_path: Path, key: str):
    f = _fixture(tmp_path)
    payload = json.loads(f[key].read_text())
    payload["tampered"] = True
    f[key].write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="HASH_MISMATCH"):
        _build(f)


def test_evidence_timestamp_drift_is_conflict(tmp_path: Path):
    f = _fixture(tmp_path)
    p = json.loads(f["proposal"].read_text())
    p["rows"][0]["proposed_settled_at"] = "2026-08-25T19:01:00+00:00"
    f["proposal"].write_text(json.dumps(p))
    _rehash_proposal(f)
    att, _ = _build(f)
    assert att["review_state"] == "EVIDENCE_CONFLICT"


@pytest.mark.parametrize(
    ("sql", "reason"),
    [
        ("UPDATE settlements SET settled_at=? WHERE ticker=?", "EXECUTION_CAPABILITY"),
    ],
)
def test_executable_capability_is_rejected(tmp_path: Path, sql: str, reason: str):
    f = _fixture(tmp_path)
    p = json.loads(f["proposal"].read_text())
    p["contains_executable_sql"] = True
    p["sql"] = sql
    f["proposal"].write_text(json.dumps(p))
    _rehash_proposal(f)
    with pytest.raises(ValueError, match=reason):
        _build(f)


@pytest.mark.parametrize(
    ("statement", "reason"),
    [
        (
            "UPDATE settlements SET settled_at='x' WHERE ticker='KXTEST-1'",
            "CANONICAL_TIMESTAMP_APPEARED",
        ),
        (
            "UPDATE settlements SET result='unknown' WHERE ticker='KXTEST-1'",
            "SETTLEMENT_RESULT_CHANGED",
        ),
        (
            "UPDATE settlements SET updated_at='changed' WHERE ticker='KXTEST-1'",
            "SETTLEMENT_LINEAGE_CHANGED",
        ),
        ("DELETE FROM settlements WHERE ticker='KXTEST-1'", "SETTLEMENT_ROW_MISSING"),
    ],
)
def test_current_database_drift(tmp_path: Path, statement: str, reason: str):
    f = _fixture(tmp_path)
    c = sqlite3.connect(f["database"])
    c.execute(statement)
    c.commit()
    c.close()
    att, _ = _build(f)
    assert att["review_state"] == "CURRENT_STATE_DRIFTED"
    assert reason in att["rows"][0]["reason_codes"]


def test_linked_evaluation_precondition_change_is_drift(tmp_path: Path):
    f = _fixture(tmp_path)
    p = json.loads(f["proposal"].read_text())
    p["rows"][0]["compare_and_swap_preconditions"]["no_linked_evaluation"] = False
    f["proposal"].write_text(json.dumps(p))
    _rehash_proposal(f)
    att, _ = _build(f)
    assert att["review_state"] == "CURRENT_STATE_DRIFTED"


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"proposal_artifact_hash": "wrong"}, "PROPOSAL_MISMATCH"),
        ({"approved_proposal_row_hashes": []}, "ROWS_NOT_EXACT"),
        ({"approved_proposal_row_hashes": ["extra"]}, "ROWS_NOT_EXACT"),
        ({"phase4aj_execution_authorized": True}, "SELF_AUTHORIZES"),
        ({"future_separate_execution_only": False}, "SCOPE_INVALID"),
    ],
)
def test_invalid_approval_bindings(tmp_path: Path, changes: dict, reason: str):
    f = _fixture(tmp_path)
    att, _ = _build(f, approval=_approval(f, **changes))
    assert att["review_state"] == "HUMAN_APPROVAL_INVALID"
    assert any(reason in code for code in att["reason_codes"])


def test_expired_and_future_approval(tmp_path: Path):
    f = _fixture(tmp_path)
    expired = _approval(f, approved_at=f["now"] - timedelta(hours=2), expires=f["now"])
    assert _build(f, approval=expired)[0]["review_state"] == "HUMAN_APPROVAL_INVALID"
    future = _approval(
        f, approved_at=f["now"] + timedelta(minutes=6), expires=f["now"] + timedelta(hours=1)
    )
    assert _build(f, approval=future, skew=300)[0]["review_state"] == "HUMAN_APPROVAL_INVALID"


def test_human_rejection(tmp_path: Path):
    f = _fixture(tmp_path)
    assert _build(f, approval=_approval(f, decision="REJECTED"))[0]["review_state"] == "REJECTED"


def test_duplicate_proposal_rows_rejected(tmp_path: Path):
    f = _fixture(tmp_path)
    p = json.loads(f["proposal"].read_text())
    p["rows"].append(dict(p["rows"][0]))
    f["proposal"].write_text(json.dumps(p))
    _rehash_proposal(f)
    with pytest.raises(ValueError, match="DUPLICATE_PROPOSAL_ROW"):
        _build(f)


@pytest.mark.parametrize("field", ["generated_at", "proposal_expires_at"])
def test_naive_and_malformed_proposal_times_rejected(tmp_path: Path, field: str):
    f = _fixture(tmp_path)
    p = json.loads(f["proposal"].read_text())
    p[field] = "not-a-time" if field == "generated_at" else "2026-08-25T20:10:00"
    f["proposal"].write_text(json.dumps(p))
    _rehash_proposal(f)
    with pytest.raises(ValueError, match="INVALID|TIMEZONE_MISSING"):
        _build(f)


def test_deterministic_output_and_database_unchanged(tmp_path: Path):
    f = _fixture(tmp_path)
    before = f["database"].read_bytes()
    before_stat = f["database"].stat()
    first = _build(f)
    second = _build(f)
    assert first == second
    assert f["database"].read_bytes() == before
    assert f["database"].stat().st_mtime_ns == before_stat.st_mtime_ns


def test_atomic_publication_refusal_replace_and_cleanup(tmp_path: Path, monkeypatch):
    f = _fixture(tmp_path)
    att, manifest = _build(f)
    m = f["module"]
    a, b = tmp_path / "att.json", tmp_path / "manifest.json"
    m.publish_pair(a, b, att, manifest)
    with pytest.raises(FileExistsError):
        m.publish_pair(a, b, att, manifest)
    m.publish_pair(a, b, att, manifest, replace=True)
    a.unlink()
    b.unlink()
    original = m.os.replace
    calls = 0

    def fail_second(src, dst):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated")
        return original(src, dst)

    monkeypatch.setattr(m.os, "replace", fail_second)
    with pytest.raises(OSError, match="simulated"):
        m.publish_pair(a, b, att, manifest)
    assert not a.exists() and not b.exists() and not list(tmp_path.glob(".*.tmp"))


def test_outputs_contain_no_sql_or_authorization(tmp_path: Path):
    rendered = json.dumps(_build(_fixture(tmp_path)), sort_keys=True).lower()
    assert "update settlements" not in rendered
    assert 'execution_authorized": true' not in rendered
    assert 'database_mutation_performed": true' not in rendered
