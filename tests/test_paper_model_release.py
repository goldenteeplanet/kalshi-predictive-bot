"""Real same-ledger evaluation; synthetic originals, no verifier substitutes."""

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from test_overnight_provenance import artifact
from test_paper_release_dataset import observation, outcome, policy, stamp

from kalshi_predictor.overnight_paper.dataset_store import persist_dataset_record
from kalshi_predictor.overnight_paper.model_release import verify_model_release
from kalshi_predictor.overnight_paper.provenance import canonical_hash


@pytest.fixture
def ledger():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE overnight_sprint_cycles "
                "(id TEXT PRIMARY KEY,captured_at TEXT,payload TEXT)"
            )
        )
    with Session(engine) as session, session.begin():
        yield session
    engine.dispose()


def append(session, record, at):
    return persist_dataset_record(session, dataset="paper-release", record=record, recorded_at=at)


def prepared(session, *, pending=False, policy_hash=True):
    train = observation(8)
    model_hash = train.decode()["originals"]["model"]["sha256"]
    append(session, train, stamp(8))
    append(session, outcome(train), stamp(8, 2))
    append(
        session, policy(**({"model_artifact_sha256": model_hash} if policy_hash else {})), stamp(9)
    )
    for day in (10, 11):
        obs = observation(day)
        head = append(session, obs, stamp(day))
        if not pending:
            head = append(session, outcome(obs), stamp(day, 2))
    decision = observation(14).decode()["decision"]
    decision["model_evaluation_head_sha256"] = head
    return decision


def test_actual_chronological_evaluation_releases_exact_model_without_writes(ledger):
    decision = prepared(ledger)
    before = ledger.execute(text("SELECT payload FROM overnight_sprint_cycles ORDER BY id")).all()
    result = verify_model_release(ledger, decision, stamp(14))
    assert result.passed, result.blockers
    assert result.model_calibration_verified
    assert decision["model_artifact_sha256"] in result.verified_hashes
    assert decision["model_evaluation_head_sha256"] in result.verified_hashes
    assert (
        ledger.execute(text("SELECT payload FROM overnight_sprint_cycles ORDER BY id")).all()
        == before
    )


@pytest.mark.parametrize(
    "key,value,blocker",
    [
        ("model_artifact_sha256", None, "MODEL_RELEASE_MODEL_HASH_REQUIRED"),
        ("model_artifact_sha256", "a" * 64, "MODEL_RELEASE_POLICY_MODEL_HASH_MISMATCH"),
        ("model_name", "different", "MODEL_RELEASE_MODEL_IDENTITY_MISMATCH"),
        ("model_version", "different", "MODEL_RELEASE_MODEL_IDENTITY_MISMATCH"),
        ("model_evaluation_head_sha256", None, "MODEL_EVALUATION_HEAD_REQUIRED"),
        ("model_evaluation_head_sha256", "b" * 64, "MODEL_EVALUATION_HEAD_MISMATCH"),
    ],
)
def test_model_and_head_bindings(ledger, key, value, blocker):
    decision = prepared(ledger)
    decision[key] = value
    result = verify_model_release(ledger, decision, stamp(14))
    assert not result.passed and blocker in result.blockers


def test_legacy_optional_policy_hash_is_not_release_eligible(ledger):
    result = verify_model_release(ledger, prepared(ledger, policy_hash=False), stamp(14))
    assert result.blockers == ("MODEL_RELEASE_POLICY_MODEL_HASH_REQUIRED",)


def test_incomplete_holdout_is_not_release_eligible(ledger):
    result = verify_model_release(ledger, prepared(ledger, pending=True), stamp(14))
    assert not result.passed and "HOLDOUT_OUTCOMES_INCOMPLETE" in result.blockers


def test_only_exact_current_decision_observation_tail_is_allowed(ledger):
    decision = prepared(ledger)
    row = observation(14).decode()
    row.update(decision=decision, decision_id=canonical_hash(decision))
    current = artifact(row)
    tail_head = append(ledger, current, stamp(14))
    result = verify_model_release(ledger, decision, stamp(14))
    assert result.passed, result.blockers
    assert tail_head in result.verified_hashes
    append(ledger, outcome(current), stamp(14, 2))
    result = verify_model_release(ledger, decision, stamp(14, 3))
    assert result.blockers == ("MODEL_EVALUATION_HEAD_MISMATCH",)


def test_unrelated_observation_tail_cannot_use_old_head(ledger):
    decision = prepared(ledger)
    append(ledger, observation(14), stamp(14))
    assert verify_model_release(ledger, decision, stamp(14)).blockers == (
        "MODEL_EVALUATION_HEAD_MISMATCH",
    )


def test_bound_evidence_cannot_arrive_after_decision(ledger):
    decision = prepared(ledger)
    decision["decision_at"] = stamp(11).isoformat()
    assert verify_model_release(ledger, decision, stamp(14)).blockers == (
        "MODEL_EVALUATION_NOT_AVAILABLE_AT_DECISION",
    )


def test_policy_cannot_relabel_old_holdout_as_different_exact_model(ledger):
    train = observation(8)
    append(ledger, train, stamp(8))
    append(ledger, outcome(train), stamp(8, 2))
    append(ledger, policy(model_artifact_sha256="a" * 64), stamp(9))
    for day in (10, 11):
        obs = observation(day)
        append(ledger, obs, stamp(day))
        head = append(ledger, outcome(obs), stamp(day, 2))
    decision = observation(14).decode()["decision"]
    decision.update(model_evaluation_head_sha256=head, model_artifact_sha256="a" * 64)
    assert verify_model_release(ledger, decision, stamp(14)).blockers == (
        "MODEL_RELEASE_HOLDOUT_MODEL_HASH_MISMATCH",
    )


def test_complete_results_still_wait_for_precommitted_holdout_end(ledger):
    decision = prepared(ledger)
    decision["decision_at"] = stamp(12).isoformat()
    assert verify_model_release(ledger, decision, stamp(14)).blockers == (
        "HOLDOUT_WINDOW_NOT_COMPLETE",
    )


def test_missing_same_ledger_dataset_fails(ledger):
    decision = observation(14).decode()["decision"]
    decision["model_evaluation_head_sha256"] = "a" * 64
    assert verify_model_release(ledger, decision, stamp(14)).blockers == (
        "MODEL_EVALUATION_DATASET_MISSING",
    )
