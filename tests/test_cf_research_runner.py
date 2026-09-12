"""Real assembly, source gates, ownership and SQLite; no passing-gate mocks."""

import hashlib
import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock

import pytest
from sqlalchemy import text
from test_cf_candidate_assembly import preparation
from test_overnight_activation import baseline_template  # noqa: F401
from test_paper_release_dataset_store import factory  # noqa: F401

from kalshi_predictor.config import Settings
from kalshi_predictor.overnight_paper import cf_research_runner as runner
from kalshi_predictor.overnight_paper.activation import ExactReleaseEvidence
from kalshi_predictor.overnight_paper.boundary import LocalPaperAuthorization
from kalshi_predictor.overnight_paper.runtime_owner import acquire_runtime_owner


def inputs(session_factory, monkeypatch):
    paper, provenance = preparation()
    at = provenance["now"]
    monkeypatch.setattr(runner, "utc_now", lambda: at)
    path = Path(session_factory.kw["bind"].url.database)
    objective = b"synthetic CF research intake objective"
    return dict(
        session_factory=session_factory, database_path=path, paper_decision=paper,
        provenance_args=provenance,
        authorization=LocalPaperAuthorization(
            at-timedelta(seconds=1), at+timedelta(hours=2),
            hashlib.sha256(objective).hexdigest(), max_new_positions=1,
            isolated_database_path=str(path),
        ),
        objective_bytes=objective, release=Mock(spec=ExactReleaseEvidence),
        settings=Settings(
            execution_enabled=False, execution_dry_run=True, execution_kill_switch=True,
            execution_gateway_mode="disabled", autopilot_enabled=False, autopilot_dry_run=True,
            paper_order_creation_enabled=False, paper_order_kill_switch=True,
            kalshi_api_key_id=None, kalshi_private_key_path=None,
            postgres_password="", execution_confirmation_token="",
        ),
    )


def run_cf(args):
    # Real outer orchestration owns the lock; the guarded entry verifies it.
    with acquire_runtime_owner(args["database_path"]) as owner:
        return runner.run_cf_research_cycle(**args, runtime_owner=owner)


def test_current_cf_runner_persists_rejection_idempotently_without_orders(factory, monkeypatch):  # noqa: F811
    args = inputs(factory, monkeypatch)
    first = run_cf(args)
    assert first.state == "BLOCKED"
    assert run_cf(args) == first
    with factory() as session:
        for table in ("paper_orders", "paper_fills", "overnight_shadow"):
            assert session.execute(text(f"SELECT count(*) FROM {table}")).scalar_one() == 0
        saved = session.execute(text(
            "SELECT payload FROM overnight_sprint_cycles WHERE id LIKE 'release-research-v1:%'"
        )).scalars().all()
        assert len(saved) == 1
        record = json.loads(saved[0])
        assert record["full_net_ev"] is None
        assert record["cost_evidence_status"] == "ORIGINAL_EVIDENCE_REPLAYED"
        assert "COST_UNKNOWN" in record["research_blockers"]
    args["release"].verify.assert_not_called()


def test_historical_caller_clock_cannot_make_stale_candidate_current(factory, monkeypatch):  # noqa: F811
    args = inputs(factory, monkeypatch)
    actual = args["provenance_args"]["now"] + timedelta(seconds=61)
    monkeypatch.setattr(runner, "utc_now", lambda: actual)
    with pytest.raises(ValueError, match="CURRENT_DECISION_REQUIRED"):
        run_cf(args)
    with factory() as session:
        assert session.execute(text(
            "SELECT count(*) FROM overnight_sprint_cycles"
        )).scalar_one() == 0


def test_cf_research_runner_refuses_enabled_paper_orders(factory, monkeypatch):  # noqa: F811
    args = inputs(factory, monkeypatch)
    args["settings"].paper_order_creation_enabled = True
    with pytest.raises(ValueError, match="ORDERS_DISABLED_REQUIRED"):
        run_cf(args)


def test_cf_research_runner_binds_operator_objective(factory, monkeypatch):  # noqa: F811
    args = inputs(factory, monkeypatch)
    args["objective_bytes"] = b"different objective"
    with pytest.raises(ValueError, match="CURRENT_OBJECTIVE_AND_DATABASE_REQUIRED"):
        run_cf(args)


def test_cf_research_runner_uses_borrowed_owner_without_reacquiring(factory, monkeypatch):  # noqa: F811
    args = inputs(factory, monkeypatch)
    with acquire_runtime_owner(args["database_path"]) as owner:
        assert runner.run_cf_research_cycle(**args, runtime_owner=owner).state == "BLOCKED"
    with pytest.raises(ValueError, match="RUNTIME_OWNER_NOT_ACTIVE"):
        runner.run_cf_research_cycle(**args, runtime_owner=owner)


@pytest.mark.parametrize("expiry", [False, True])
def test_clock_advance_during_assembly_cannot_write_stale_attempt(factory, monkeypatch, expiry):  # noqa: F811
    args = inputs(factory, monkeypatch)
    at = args["provenance_args"]["now"]
    # Both real assembly passes see fresh data. The final writer clock is later.
    final = at + timedelta(seconds=61)
    if expiry:
        final = at + timedelta(seconds=30)
        args["authorization"] = replace(args["authorization"], expires_at=final)
    clock = iter((at, at, at, final))
    monkeypatch.setattr(runner, "utc_now", lambda: next(clock))
    expected = "AUTHORIZATION_EXPIRED" if expiry else "CURRENT_DECISION_REQUIRED"
    with pytest.raises(ValueError, match=expected):
        run_cf(args)
    with factory() as session:
        assert session.execute(text(
            "SELECT count(*) FROM overnight_sprint_cycles"
        )).scalar_one() == 0
        assert session.execute(text("SELECT count(*) FROM paper_orders")).scalar_one() == 0


def test_current_unknown_cost_observation_commits_and_replays_after_restart(factory, monkeypatch):  # noqa: F811
    from test_cf_dataset_replay import observation

    from kalshi_predictor.overnight_paper.dataset_store import load_dataset
    from kalshi_predictor.overnight_paper.evaluation_dataset import _validate_stored_observation

    args = inputs(factory, monkeypatch)
    item, provenance = observation(cost_evidence=True, return_inputs=True)
    args.update(
        provenance_args=provenance, evaluation_observation=item,
        cost_record=item.decode()["cost_record"],
    )
    first = run_cf(args)
    assert first.state == "BLOCKED"
    assert run_cf(args) == first
    del item, args
    with factory() as session:
        records = load_dataset(session, dataset="paper-release")
        assert len(records) == 1
        row = records[0].decode()["record"]
        _validate_stored_observation(row)
        assert row["estimated_fee"] is row["slippage"] is row["uncertainty"] is None
        for table in ("paper_orders", "paper_fills", "overnight_shadow"):
            assert session.execute(text(f"SELECT count(*) FROM {table}")).scalar_one() == 0


def test_candidate_observation_must_share_exact_cost_record(factory, monkeypatch):  # noqa: F811
    from test_cf_dataset_replay import observation

    from kalshi_predictor.overnight_paper.provenance import Artifact, canonical_hash

    args = inputs(factory, monkeypatch)
    item, provenance = observation(cost_evidence=True, return_inputs=True)
    row = item.decode()
    record = row.pop("cost_record")
    args.update(
        provenance_args=provenance, cost_record=record,
        evaluation_observation=Artifact(
            canonical_hash(row), json.dumps(row, sort_keys=True, separators=(",", ":")).encode(),
        ),
    )
    with pytest.raises(ValueError, match="CF_ASSEMBLY_EVALUATION_BINDING_MISMATCH"):
        run_cf(args)
    with factory() as session:
        assert session.execute(text(
            "SELECT count(*) FROM overnight_sprint_cycles"
        )).scalar_one() == 0


def test_cf_runner_cannot_enter_without_concrete_active_owner(factory, monkeypatch):  # noqa: F811
    args = inputs(factory, monkeypatch)
    with pytest.raises(ValueError, match="CONCRETE_RUNTIME_OWNER_REQUIRED"):
        runner.run_cf_research_cycle(**args, runtime_owner=None)
    with factory() as session:
        assert session.execute(text(
            "SELECT count(*) FROM overnight_sprint_cycles"
        )).scalar_one() == 0


def test_actual_cf_guarded_entry_has_no_dynamic_execution_capability():
    from kalshi_predictor.overnight_paper.boundary_gate import audit_local_call_path

    checked = audit_local_call_path(
        Path(__file__).resolve().parents[1],
        entrypoints=(("kalshi_predictor.overnight_paper.cf_research_runner",
                      "run_cf_research_cycle"),),
    )
    assert checked.passed, checked.blockers
