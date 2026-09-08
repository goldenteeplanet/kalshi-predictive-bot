from __future__ import annotations

import importlib.util
import json
import sqlite3
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
        "phase4ai_tested",
        Path(__file__).parents[1] / "scripts/local/phase4ai_canonicalization_proposal.py",
    )


def _ah_helper():
    return _load(
        "phase4ah_fixture_for_4ai",
        Path(__file__).with_name("test_phase4ah_timestamp_readiness_gate.py"),
    )


def _fixture(tmp_path: Path, *, with_evidence: bool = True):
    helper = _ah_helper()
    ah_fixture = helper._fixture(tmp_path, with_evidence=with_evidence)
    reaudit, gate = helper._build(ah_fixture)
    reaudit_path = tmp_path / "phase4af-reaudit.json"
    gate_path = tmp_path / "phase4ah-gate.json"
    reaudit_path.write_text(json.dumps(reaudit), encoding="utf-8")
    gate_path.write_text(json.dumps(gate), encoding="utf-8")
    return (
        _module(),
        ah_fixture[1],
        ah_fixture[2],
        ah_fixture[3],
        ah_fixture[4],
        ah_fixture[5],
        ah_fixture[6],
        ah_fixture[7],
        reaudit_path,
        gate_path,
        ah_fixture[8],
    )


def _build(fixture, **overrides):
    module, database, history, ad, ae, baseline, status, evidence, reaudit, gate, now = fixture
    return module.build(
        database,
        gate,
        reaudit,
        baseline,
        status,
        evidence,
        ad,
        ae,
        history,
        now=overrides.get("now", now),
        valid_for_seconds=overrides.get("valid_for_seconds", 3600),
    )


def test_valid_proposal_is_unreviewed_non_executable_and_database_unchanged(tmp_path: Path):
    fixture = _fixture(tmp_path)
    database = fixture[1]
    before = database.read_bytes()
    proposal, review = _build(fixture)
    assert proposal["proposal_state"] == "READY_FOR_REVIEW"
    assert proposal["disposition_counts"] == {"PROPOSED": 1}
    assert proposal["proposed_count"] == 1
    assert proposal["contains_executable_sql"] is False
    assert proposal["execution_authorized"] is False
    assert review["approval_status"] == "UNREVIEWED"
    assert review["execution_authorized"] is False
    row = proposal["rows"][0]
    assert row["semantic_mutation"]["executable"] is False
    assert row["compare_and_swap_preconditions"]["settled_at"] is None
    assert database.read_bytes() == before


def test_gate_not_ready_produces_no_proposal(tmp_path: Path):
    proposal, review = _build(_fixture(tmp_path, with_evidence=False))
    assert proposal["proposal_state"] == "NO_ELIGIBLE_ROWS"
    assert proposal["disposition_counts"] == {"GATE_NOT_READY": 1}
    assert proposal["proposed_count"] == 0
    assert review["proposed_ticker_count"] == 0


def test_canonical_timestamp_appearing_is_attention_drift(tmp_path: Path):
    fixture = _fixture(tmp_path)
    connection = sqlite3.connect(fixture[1])
    connection.execute(
        "UPDATE settlements SET settled_at='2026-08-25T19:00:00+00:00' WHERE ticker='KXTEST-1'"
    )
    connection.commit()
    connection.close()
    proposal, _ = _build(fixture)
    assert proposal["proposal_state"] == "ATTENTION_DRIFT"
    assert proposal["disposition_counts"] == {"CANONICAL_TIMESTAMP_ALREADY_PRESENT": 1}


def test_settlement_lineage_drift_is_detected(tmp_path: Path):
    fixture = _fixture(tmp_path)
    connection = sqlite3.connect(fixture[1])
    connection.execute("UPDATE settlements SET updated_at='2026-08-25T19:02:00+00:00'")
    connection.commit()
    connection.close()
    proposal, _ = _build(fixture)
    assert proposal["disposition_counts"] == {"SETTLEMENT_LINEAGE_DRIFTED": 1}


@pytest.mark.parametrize(
    ("index", "field", "message"),
    [
        (9, "ready_count", "PHASE4AH_GATE_HASH_MISMATCH"),
        (8, "ready_count", "PHASE4AF_REAUDIT_HASH_MISMATCH"),
        (6, "request_count", "PHASE4AG_STATUS_HASH_MISMATCH"),
        (3, "ready_count", "PHASE4AD_HASH_MISMATCH"),
        (4, "planned_row_count", "PHASE4AE_HASH_MISMATCH"),
    ],
)
def test_tampered_lineage_artifacts_rejected(tmp_path: Path, index: int, field: str, message: str):
    fixture = _fixture(tmp_path)
    path = fixture[index]
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload[field] = 999
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        _build(fixture)


def test_deterministic_proposal_and_expiration(tmp_path: Path):
    fixture = _fixture(tmp_path)
    first = _build(fixture, valid_for_seconds=60)
    second = _build(fixture, valid_for_seconds=60)
    assert first == second
    assert first[0]["proposal_expires_at"] == "2026-08-25T19:11:00+00:00"


def test_invalid_validity_and_naive_time_rejected(tmp_path: Path):
    fixture = _fixture(tmp_path)
    with pytest.raises(ValueError, match="VALIDITY_INVALID"):
        _build(fixture, valid_for_seconds=0)
    with pytest.raises(ValueError, match="TIMEZONE_MISSING"):
        _build(fixture, now=fixture[10].replace(tzinfo=None))


def test_review_manifest_hash_and_required_assertions(tmp_path: Path):
    module = _module()
    proposal, review = _build(_fixture(tmp_path))
    expected = module.canonical_hash(
        {key: value for key, value in review.items() if key != "manifest_hash"}
    )
    assert review["manifest_hash"] == expected
    assert review["proposal_artifact_hash"] == proposal["artifact_hash"]
    assert "DATABASE_MUTATION_HAS_NOT_OCCURRED" in review["required_reviewer_assertions"]


def test_atomic_pair_refusal_replace_and_cleanup(tmp_path: Path):
    fixture = _fixture(tmp_path)
    module = fixture[0]
    proposal, review = _build(fixture)
    proposal_path, review_path = tmp_path / "proposal.json", tmp_path / "review.json"
    module.publish_pair(proposal_path, review_path, proposal, review)
    with pytest.raises(FileExistsError):
        module.publish_pair(proposal_path, review_path, proposal, review)
    module.publish_pair(proposal_path, review_path, proposal, review, replace=True)
    assert not list(tmp_path.glob(".*.tmp"))
    assert json.loads(review_path.read_text(encoding="utf-8"))["approval_status"] == "UNREVIEWED"


def test_proposal_contains_no_sql_or_authorization(tmp_path: Path):
    proposal, review = _build(_fixture(tmp_path))
    rendered = json.dumps([proposal, review], sort_keys=True).lower()
    assert "update settlements" not in rendered
    assert 'execution_authorized": true' not in rendered
    assert 'write_authorized": true' not in rendered
