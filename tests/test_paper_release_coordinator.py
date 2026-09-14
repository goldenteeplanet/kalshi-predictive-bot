"""Transaction/restart tests, not production qualification certification.

The reused activation fixture isolates source gates and engine revalidation.
All-gate semantic integration has a separate test; these tests use the actual
SQLite ledger, shadow storage, activation transaction and local fill simulator.
"""

import json
from dataclasses import replace
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from test_overnight_activation import baseline_template, prepared  # noqa: F401
from test_overnight_watcher import counts, observation, run

from kalshi_predictor.overnight_paper import coordinator
from kalshi_predictor.overnight_paper.boundary import authorization_fingerprint
from kalshi_predictor.overnight_paper.dashboard import snapshot
from kalshi_predictor.overnight_paper.qualification import decision_fingerprint


@pytest.fixture
def admission(prepared):  # noqa: F811
    auth = replace(prepared["authorization"], max_new_positions=1, max_open_positions=1)
    prepared["authorization"] = auth
    args = prepared["qualification_args"]
    args["decision_inputs"]["authorization_sha256"] = authorization_fingerprint(auth)
    key = decision_fingerprint(args["decision_inputs"])
    args["decision_id"] = key
    args["evidence"] = tuple(replace(e, decision_id=key) for e in args["evidence"])
    with prepared["session_factory"]() as session:
        marker_key = "authorization-baseline:" + auth.database_id
        marker = json.loads(
            session.execute(
                text("SELECT payload FROM overnight_sprint_cycles WHERE id=:id"),
                {"id": marker_key},
            ).scalar_one()
        )
        marker["authorization_sha256"] = authorization_fingerprint(auth)
        session.execute(
            text("UPDATE overnight_sprint_cycles SET payload=:p WHERE id=:id"),
            {"p": json.dumps(marker), "id": marker_key},
        )
        session.execute(text("DELETE FROM overnight_shadow"))
        session.commit()
    result = {
        k: prepared[k]
        for k in (
            "session_factory",
            "database_path",
            "authorization",
            "objective_bytes",
            "release",
            "settings",
            "now",
        )
    }
    result["candidate"] = coordinator.PreparedCandidate(
        prepared["decision"],
        args,
        prepared["shadow_payload"],
    )
    return result


def test_shadow_only_decimal_checkpoint_and_duplicate_replay(admission):
    first = coordinator.admit_prepared_candidate(**admission)
    assert first.state == "SHADOW_ONLY"
    assert coordinator.admit_prepared_candidate(**admission) == first
    with admission["session_factory"]() as session:
        assert session.execute(text("SELECT count(*) FROM paper_orders")).scalar_one() == 0
        assert session.execute(text("SELECT count(*) FROM overnight_shadow")).scalar_one() == 1
        payload = json.loads(
            session.execute(
                text(
                    "SELECT payload FROM overnight_sprint_cycles "
                    "WHERE id LIKE 'release-qualification:%'"
                )
            ).scalar_one()
        )
        assert payload["qualification"]["net_ev"] == str(
            admission["candidate"].qualification_args["ev"].net_ev
        )
    dashboard = snapshot(admission["database_path"])
    assert dashboard["shadow_candidates"] == 1
    assert len(dashboard["qualification_gates"]) == 12
    assert dashboard["last_qualification_status"] == "PAPER_ELIGIBLE"
    assert dashboard["qualification_current"] is False
    assert dashboard["paper_mode"] == "NOT_ACTIVE"
    assert "RECORDED_QUALIFICATION_REQUIRES_REVALIDATION" in dashboard["blockers"]
    assert dashboard["research_assessment_count"] == 1
    research = dashboard["latest_research_assessment"]
    assert research["status"] == "COST_UNKNOWN"
    assert research["full_net_ev"] is None
    assert research["execution_authority"] is False
    assert research["provisional_qualification_net_ev"] == payload["qualification"]["net_ev"]


def test_entry_restart_and_settlement_exactly_once(admission, prepared):  # noqa: F811
    first = coordinator.admit_prepared_candidate(**admission, entries_enabled=True)
    assert first.state == "PAPER_FILLED"
    resumed = coordinator.admit_prepared_candidate(**admission, entries_enabled=False)
    assert resumed.state == "EXISTING_EXPERIMENT"
    assert resumed.order_id == first.order_id
    prepared["shadow_id"] = first.shadow_id
    final = observation(prepared)
    run(prepared, final)
    run(prepared, final)
    assert counts(prepared) == dict(paper_orders=1, paper_fills=1, paper_pnl=1, settlements=1)


def test_crash_after_fill_before_checkpoint_does_not_duplicate(admission, monkeypatch):
    original = coordinator._checkpoint

    def interrupted(session, key, at, payload):
        if key.startswith("release-entry:"):
            raise RuntimeError("synthetic crash after committed fill")
        return original(session, key, at, payload)

    monkeypatch.setattr(coordinator, "_checkpoint", interrupted)
    with pytest.raises(RuntimeError, match="synthetic crash"):
        coordinator.admit_prepared_candidate(**admission, entries_enabled=True)
    monkeypatch.setattr(coordinator, "_checkpoint", original)
    result = coordinator.admit_prepared_candidate(**admission, entries_enabled=True)
    assert result.state == "EXISTING_EXPERIMENT"
    with admission["session_factory"]() as session:
        assert session.execute(text("SELECT count(*) FROM paper_orders")).scalar_one() == 1
        assert session.execute(text("SELECT count(*) FROM paper_fills")).scalar_one() == 1
        assert (
            session.execute(
                text("SELECT count(*) FROM overnight_sprint_cycles WHERE id LIKE 'release-entry:%'")
            ).scalar_one()
            == 1
        )


def test_crash_before_shadow_commit_rolls_back_then_retries(admission, monkeypatch):
    original = coordinator._checkpoint

    def interrupted(session, key, at, payload):
        if key.startswith("release-shadow:"):
            raise RuntimeError("synthetic precommit crash")
        return original(session, key, at, payload)

    monkeypatch.setattr(coordinator, "_checkpoint", interrupted)
    with pytest.raises(RuntimeError, match="precommit"):
        coordinator.admit_prepared_candidate(**admission)
    with admission["session_factory"]() as session:
        assert session.execute(text("SELECT count(*) FROM overnight_shadow")).scalar_one() == 0
        assert session.execute(text(
            "SELECT count(*) FROM overnight_sprint_cycles WHERE id LIKE 'release-research-v1:%'"
        )).scalar_one() == 0
    monkeypatch.setattr(coordinator, "_checkpoint", original)
    assert coordinator.admit_prepared_candidate(**admission).state == "SHADOW_ONLY"


def test_callable_factory_refused_without_invocation(admission):
    factory = Mock()
    with pytest.raises(ValueError, match="CONCRETE_SQLITE_SESSION_FACTORY_REQUIRED"):
        coordinator.admit_prepared_candidate(**{**admission, "session_factory": factory})
    factory.assert_not_called()


def test_caller_engine_connection_creator_is_never_invoked(admission):
    creator = Mock(side_effect=AssertionError("caller connection callback executed"))
    engine = create_engine(f"sqlite:///{admission['database_path']}", creator=creator)
    try:
        result = coordinator.admit_prepared_candidate(
            **{**admission, "session_factory": sessionmaker(engine)}
        )
        assert result.state == "SHADOW_ONLY"
        creator.assert_not_called()
    finally:
        engine.dispose()


def test_missing_evidence_is_durable_rejection(admission):
    admission["candidate"].qualification_args["evidence"] = ()
    result = coordinator.admit_prepared_candidate(**admission, entries_enabled=True)
    assert result.state == "BLOCKED"
    assert result.blockers
    with admission["session_factory"]() as session:
        assert session.execute(text("SELECT count(*) FROM paper_orders")).scalar_one() == 0
        assert session.execute(text("SELECT count(*) FROM overnight_shadow")).scalar_one() == 0
    dashboard = snapshot(admission["database_path"])
    assert dashboard["first_blocker"] == result.blockers[0]
    assert dashboard["last_qualification_status"] == "PAPER_NOT_READY"
    assert dashboard["research_assessment_count"] == 1
    assert dashboard["research_status_counts"] == {"RULE_UNCERTIFIED": 1}
    assert dashboard["latest_research_assessment"]["research_blockers"] == [
        "RULE_UNCERTIFIED", "BOOK_INVALID", "COST_UNKNOWN",
    ]
    assert dashboard["shadow_candidates"] == 0


def test_invalid_book_record_is_durable_and_idempotent(admission):
    args = admission["candidate"].qualification_args
    args["evidence"] = tuple(e for e in args["evidence"] if e.gate != 5)
    first = coordinator.admit_prepared_candidate(**admission)
    assert first.state == "BLOCKED"
    assert coordinator.admit_prepared_candidate(**admission) == first
    dashboard = snapshot(admission["database_path"])
    assert dashboard["research_assessment_count"] == 1
    assert dashboard["research_status_counts"] == {"BOOK_INVALID": 1}
    assert dashboard["shadow_candidates"] == 0
    assert dashboard["open_positions"] == 0


@pytest.mark.parametrize("tamper", ["positive", "missing_qualification", "changed_qualification"])
def test_research_reader_refuses_forged_positive_or_broken_lineage(admission, tamper):
    coordinator.admit_prepared_candidate(**admission)
    with admission["session_factory"]() as session:
        if tamper == "positive":
            query = ("SELECT id,payload FROM overnight_sprint_cycles "
                     "WHERE id LIKE 'release-research-v1:%'")
            key, raw = session.execute(text(query)).one()
            payload = json.loads(raw)
            payload.update(status="POSITIVE_NET_EV", full_net_ev="0.50")
            session.execute(text("UPDATE overnight_sprint_cycles SET payload=:p WHERE id=:id"),
                            {"p": json.dumps(payload), "id": key})
        elif tamper == "missing_qualification":
            session.execute(text(
                "DELETE FROM overnight_sprint_cycles WHERE id LIKE 'release-qualification:%'"
            ))
        else:
            query = ("SELECT id,payload FROM overnight_sprint_cycles "
                     "WHERE id LIKE 'release-qualification:%'")
            key, raw = session.execute(text(query)).one()
            payload = json.loads(raw)
            payload["qualification"]["net_ev"] = "0.99"
            session.execute(text("UPDATE overnight_sprint_cycles SET payload=:p WHERE id=:id"),
                            {"p": json.dumps(payload), "id": key})
        session.commit()
    dashboard = snapshot(admission["database_path"])
    assert dashboard["paper_mode"] == "UNVERIFIED"
    assert dashboard["research_assessment_count"] is None
    assert dashboard["latest_research_assessment"] is None
    assert dashboard["research_status_counts"] is None


def test_different_candidate_cannot_use_second_slot(admission):
    coordinator.admit_prepared_candidate(**admission, entries_enabled=True)
    admission["candidate"].shadow_payload["model_version"] = "different-candidate"
    result = coordinator.admit_prepared_candidate(**admission, entries_enabled=True)
    assert result.state == "EXPERIMENT_CAPACITY_EXHAUSTED"


def test_restart_refuses_changed_fill(admission):
    coordinator.admit_prepared_candidate(**admission, entries_enabled=True)
    with admission["session_factory"]() as session:
        session.execute(text("UPDATE paper_fills SET price='0.01'"))
        session.commit()
    with pytest.raises(ValueError, match="EXISTING_FILL_RECONCILIATION_FAILED"):
        coordinator.admit_prepared_candidate(**admission, entries_enabled=True)


def test_fee_expiry_does_not_recharge_existing_experiment(admission):
    from datetime import timedelta

    first = coordinator.admit_prepared_candidate(**admission, entries_enabled=True)
    admission["now"] += timedelta(days=2)
    replay = coordinator.admit_prepared_candidate(**admission, entries_enabled=True)
    assert replay.state == "EXISTING_EXPERIMENT"
    assert replay.order_id == first.order_id
    with admission["session_factory"]() as session:
        assert session.execute(text("SELECT count(*) FROM paper_fills")).scalar_one() == 1
        assert session.execute(text("SELECT fee FROM paper_fills")).scalar_one() == "0.02"


def test_replay_rejects_guarded_fill_provenance_label_tamper(admission):
    coordinator.admit_prepared_candidate(**admission, entries_enabled=True)
    with admission["session_factory"]() as session:
        raw = json.loads(
            session.execute(text("SELECT raw_fill_json FROM paper_fills")).scalar_one()
        )
        raw["fee_provenance"] = "LEGACY_CONFIGURED_NONCERTIFIED"
        session.execute(text("UPDATE paper_fills SET raw_fill_json=:raw"), {"raw": json.dumps(raw)})
        session.commit()
        before = session.execute(text("SELECT count(*) FROM overnight_sprint_cycles")).scalar_one()
    with pytest.raises(ValueError, match="EXISTING_FILL_FEE_LINEAGE_MISMATCH"):
        coordinator.admit_prepared_candidate(**admission, entries_enabled=True)
    with admission["session_factory"]() as session:
        assert session.execute(text("SELECT count(*) FROM paper_fills")).scalar_one() == 1
        assert (
            session.execute(text("SELECT count(*) FROM overnight_sprint_cycles")).scalar_one()
            == before
        )
