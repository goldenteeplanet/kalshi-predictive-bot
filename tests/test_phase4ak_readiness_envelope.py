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
        "phase4ak_tested",
        Path(__file__).parents[1] / "scripts/local/phase4ak_readiness_envelope.py",
    )


def _fixture(tmp_path: Path, *, approved: bool = True, approval_expires=None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    helper = _load(
        "phase4aj_fixture_for_4ak",
        Path(__file__).with_name("test_phase4aj_independent_proposal_review.py"),
    )
    f = helper._fixture(tmp_path)
    approval = (
        helper._approval(
            f,
            expires=approval_expires or f["now"] + timedelta(hours=2),
        )
        if approved
        else None
    )
    attestation, manifest = helper._build(f, approval=approval)
    aj_path, aj_manifest_path = tmp_path / "phase4aj.json", tmp_path / "phase4aj-manifest.json"
    aj_path.write_text(json.dumps(attestation))
    aj_manifest_path.write_text(json.dumps(manifest))
    f.update(
        {
            "ak": _module(),
            "approval": approval,
            "aj": aj_path,
            "aj_manifest": aj_manifest_path,
        }
    )
    return f


def _build(f, *, now=None, approval_marker=True):
    return f["ak"].build(
        f["database"],
        f["aj"],
        f["aj_manifest"],
        f["proposal"],
        f["review"],
        f["approval"] if approval_marker else None,
        f["gate"],
        f["reaudit"],
        f["baseline"],
        f["status"],
        f["evidence"],
        f["ad"],
        f["ae"],
        f["history"],
        now=now or f["now"],
    )


def _rehash_aj(f):
    m = f["ak"]
    a = json.loads(f["aj"].read_text())
    a["rows_hash"] = m.canonical_hash(a["rows"])
    a["artifact_hash"] = m._hash(a)
    f["aj"].write_text(json.dumps(a))
    manifest = json.loads(f["aj_manifest"].read_text())
    manifest["review_attestation_hash"] = a["artifact_hash"]
    manifest["manifest_hash"] = m._hash(manifest, "manifest_hash")
    f["aj_manifest"].write_text(json.dumps(manifest))
    return a, manifest


def _rehash_proposal_chain(f):
    m = f["ak"]
    p = json.loads(f["proposal"].read_text())
    for row in p["rows"]:
        row["proposal_row_hash"] = m.canonical_hash(
            {k: v for k, v in row.items() if k != "proposal_row_hash"}
        )
    p["rows_hash"] = m.canonical_hash(p["rows"])
    p["artifact_hash"] = m._hash(p)
    f["proposal"].write_text(json.dumps(p))
    review = json.loads(f["review"].read_text())
    review["proposal_artifact_hash"] = p["artifact_hash"]
    review["proposal_rows_hash"] = p["rows_hash"]
    review["manifest_hash"] = m._hash(review, "manifest_hash")
    f["review"].write_text(json.dumps(review))
    return p


def test_approved_review_produces_non_executable_readiness_envelope(tmp_path: Path):
    envelope, handoff = _build(_fixture(tmp_path))
    assert envelope["readiness_state"] == "READY_FOR_SEPARATE_EXECUTOR_DESIGN"
    assert envelope["eligible_count"] == 1
    assert handoff["executor_implemented"] is False
    assert envelope["execution_authorized"] is handoff["execution_authorized"] is False
    assert len(handoff["required_future_safeguards"]) == 11


def test_missing_approval_fails_lineage_for_approved_review(tmp_path: Path):
    f = _fixture(tmp_path)
    with pytest.raises(ValueError, match="LINEAGE_FAILURE_AJ_APPROVAL"):
        _build(f, approval_marker=False)


def test_awaiting_approval_is_not_ready(tmp_path: Path):
    envelope, _ = _build(_fixture(tmp_path, approved=False))
    assert envelope["readiness_state"] == "REVIEW_NOT_APPROVED"


@pytest.mark.parametrize("state", ["CURRENT_STATE_DRIFTED", "PROPOSAL_EXPIRED"])
def test_nonapproved_phase4aj_states_are_rejected(tmp_path: Path, state: str):
    f = _fixture(tmp_path)
    a = json.loads(f["aj"].read_text())
    a["review_state"] = state
    f["aj"].write_text(json.dumps(a))
    _rehash_aj(f)
    envelope, _ = _build(f)
    assert envelope["readiness_state"] == "REVIEW_NOT_APPROVED"


@pytest.mark.parametrize(
    ("key", "message"),
    [
        ("aj", "ATTESTATION_HASH_MISMATCH"),
        ("aj_manifest", "MANIFEST_HASH_MISMATCH"),
        ("proposal", "PROPOSAL_HASH_MISMATCH"),
    ],
)
def test_tampered_primary_inputs_fail_closed(tmp_path: Path, key: str, message: str):
    f = _fixture(tmp_path)
    payload = json.loads(f[key].read_text())
    payload["tampered"] = True
    f[key].write_text(json.dumps(payload))
    with pytest.raises(ValueError, match=message):
        _build(f)


def test_mismatched_aj_pair_and_attestation_reference_fail_closed(tmp_path: Path):
    f = _fixture(tmp_path)
    manifest = json.loads(f["aj_manifest"].read_text())
    manifest["publication_pair_id"] = "wrong"
    manifest["manifest_hash"] = f["ak"]._hash(manifest, "manifest_hash")
    f["aj_manifest"].write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="LINEAGE_FAILURE_AJ_PAIR"):
        _build(f)
    f = _fixture(tmp_path / "second")
    manifest = json.loads(f["aj_manifest"].read_text())
    manifest["review_attestation_hash"] = "wrong"
    manifest["manifest_hash"] = f["ak"]._hash(manifest, "manifest_hash")
    f["aj_manifest"].write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="LINEAGE_FAILURE_AJ_ATTESTATION"):
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
    with pytest.raises(ValueError, match="LINEAGE_FAILURE_AJ_AC"):
        _build(f)


@pytest.mark.parametrize(
    ("delta", "state"),
    [(-1, "READY_FOR_SEPARATE_EXECUTOR_DESIGN"), (0, "PROPOSAL_EXPIRED"), (1, "PROPOSAL_EXPIRED")],
)
def test_proposal_expiration_boundary(tmp_path: Path, delta: int, state: str):
    f = _fixture(tmp_path)
    expires = f["now"] + timedelta(hours=1)
    assert _build(f, now=expires + timedelta(microseconds=delta))[0]["readiness_state"] == state


@pytest.mark.parametrize(
    ("delta", "state"),
    [
        (-1, "READY_FOR_SEPARATE_EXECUTOR_DESIGN"),
        (0, "APPROVAL_EXPIRED_OR_INVALID"),
        (1, "APPROVAL_EXPIRED_OR_INVALID"),
    ],
)
def test_approval_expiration_boundary(tmp_path: Path, delta: int, state: str):
    expires = _fixture(tmp_path)["now"] + timedelta(minutes=30)
    f = _fixture(tmp_path / "case", approval_expires=expires)
    assert _build(f, now=expires + timedelta(microseconds=delta))[0]["readiness_state"] == state


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
    envelope, _ = _build(f)
    assert envelope["readiness_state"] == "CURRENT_STATE_DRIFTED"
    assert reason in envelope["rows"][0]["reason_codes"]


def test_authoritative_evidence_change_and_timestamp_conflict(tmp_path: Path):
    f = _fixture(tmp_path)
    p = json.loads(f["proposal"].read_text())
    p["rows"][0]["proposed_settled_at"] = "2026-08-25T19:01:00+00:00"
    f["proposal"].write_text(json.dumps(p))
    _rehash_proposal_chain(f)
    with pytest.raises(ValueError, match="LINEAGE_FAILURE_AJ_AI_PROPOSAL"):
        _build(f)
    f = _fixture(tmp_path / "conflict")
    reaudit = json.loads(f["reaudit"].read_text())
    reaudit["rows"][0]["classification"] = "TIMESTAMP_CONFLICT"
    reaudit["rows_hash"] = f["ak"].canonical_hash(reaudit["rows"])
    reaudit["artifact_hash"] = f["ak"]._hash(reaudit)
    f["reaudit"].write_text(json.dumps(reaudit))
    with pytest.raises(ValueError, match="LINEAGE_FAILURE_AJ_AF"):
        _build(f)


def test_linked_evaluation_and_incomplete_compare_and_swap_fail_closed(tmp_path: Path):
    f = _fixture(tmp_path)
    p = json.loads(f["proposal"].read_text())
    p["rows"][0]["compare_and_swap_preconditions"]["no_linked_evaluation"] = False
    f["proposal"].write_text(json.dumps(p))
    _rehash_proposal_chain(f)
    with pytest.raises(ValueError, match="LINEAGE_FAILURE_AJ_AI_PROPOSAL"):
        _build(f)
    f = _fixture(tmp_path / "missing")
    p = json.loads(f["proposal"].read_text())
    del p["rows"][0]["compare_and_swap_preconditions"]["updated_at"]
    f["proposal"].write_text(json.dumps(p))
    _rehash_proposal_chain(f)
    with pytest.raises(ValueError, match="LINEAGE_FAILURE_AJ_AI_PROPOSAL"):
        _build(f)


def test_duplicate_and_partial_approved_sets_fail_closed(tmp_path: Path):
    f = _fixture(tmp_path)
    manifest = json.loads(f["aj_manifest"].read_text())
    manifest["eligible_proposal_row_hashes"].append(manifest["eligible_proposal_row_hashes"][0])
    manifest["manifest_hash"] = f["ak"]._hash(manifest, "manifest_hash")
    f["aj_manifest"].write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="ADVANCEMENT_ROWS_INVALID"):
        _build(f)
    f = _fixture(tmp_path / "partial")
    approval = json.loads(f["approval"].read_text())
    approval["approved_proposal_row_hashes"] = []
    approval["artifact_hash"] = f["ak"]._hash(approval)
    f["approval"].write_text(json.dumps(approval))
    with pytest.raises(ValueError, match="LINEAGE_FAILURE_AJ_APPROVAL"):
        _build(f)


def test_deterministic_and_database_unchanged(tmp_path: Path):
    f = _fixture(tmp_path)
    before = f["database"].read_bytes()
    stat = f["database"].stat()
    first = _build(f)
    second = _build(f)
    assert first == second and f["database"].read_bytes() == before
    assert f["database"].stat().st_mtime_ns == stat.st_mtime_ns


def test_atomic_publication_refusal_replace_and_failure_rollback(tmp_path: Path, monkeypatch):
    f = _fixture(tmp_path)
    envelope, handoff = _build(f)
    m = f["ak"]
    a, b = tmp_path / "envelope.json", tmp_path / "handoff.json"
    m.publish_pair(a, b, envelope, handoff)
    with pytest.raises(FileExistsError):
        m.publish_pair(a, b, envelope, handoff)
    m.publish_pair(a, b, envelope, handoff, replace=True)
    old_a, old_b = a.read_bytes(), b.read_bytes()
    original = m.os.replace
    calls = 0

    def fail(src, dst):
        nonlocal calls
        calls += 1
        if calls == 4:
            raise OSError("simulated")
        return original(src, dst)

    monkeypatch.setattr(m.os, "replace", fail)
    with pytest.raises(OSError, match="simulated"):
        m.publish_pair(a, b, envelope, handoff, replace=True)
    assert a.read_bytes() == old_a and b.read_bytes() == old_b
    assert not list(tmp_path.glob(".*.tmp")) and not list(tmp_path.glob(".*.bak"))


def test_outputs_contain_no_sql_callbacks_or_authorization(tmp_path: Path):
    rendered = json.dumps(_build(_fixture(tmp_path)), sort_keys=True).lower()
    assert "update settlements" not in rendered
    assert 'execution_authorized": true' not in rendered
    assert 'executor_implemented": true' not in rendered
    assert 'contains_sql": true' not in rendered
