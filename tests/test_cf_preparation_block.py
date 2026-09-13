"""Actual persisted forecasts, canonical replay and owned SQLite; no risk pass mocks."""

import hashlib
import json
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock

import pytest
from sqlalchemy import text
from test_cf_risk_fee_boundary import NOW, SYNTHETIC_ACCOUNT, seed, unknown_record
from test_overnight_activation import baseline_template  # noqa: F401
from test_paper_release_dataset_store import factory  # noqa: F401

from kalshi_predictor.config import Settings
from kalshi_predictor.crypto.cost_record import replay_cost_record
from kalshi_predictor.overnight_paper import cf_preparation_block as writer
from kalshi_predictor.overnight_paper.boundary import LocalPaperAuthorization
from kalshi_predictor.overnight_paper.runtime_owner import acquire_runtime_owner


def inputs(factory, monkeypatch):  # noqa: F811
    with factory() as session:
        decision = seed(session, stored_cf=True)
        record = unknown_record(session, decision)
        session.commit()
    monkeypatch.setattr(writer, "utc_now", lambda: NOW)
    path = Path(factory.kw["bind"].url.database)
    objective = b"synthetic incomplete preparation storage, no orders"
    return dict(
        session_factory=factory, database_path=path, decision=decision, decision_at=NOW,
        cost_record=record, account_identity_sha256=SYNTHETIC_ACCOUNT,
        authorization=LocalPaperAuthorization(
            NOW-timedelta(seconds=1), NOW+timedelta(hours=1),
            hashlib.sha256(objective).hexdigest(), max_new_positions=1,
            isolated_database_path=str(path),
        ),
        objective_bytes=objective,
        settings=Settings(
            execution_enabled=False, execution_dry_run=True, execution_kill_switch=True,
            execution_gateway_mode="disabled", autopilot_enabled=False, autopilot_dry_run=True,
            paper_order_creation_enabled=False, paper_order_kill_switch=True,
            kalshi_api_key_id=None, kalshi_private_key_path=None, postgres_password="",
            execution_confirmation_token="",
        ),
    )


def run(args):
    with acquire_runtime_owner(args["database_path"]) as owner:
        return writer.persist_cf_cost_block(**args, runtime_owner=owner)


def test_unknown_cost_preparation_survives_restart_without_risk_or_shadow(
    factory, monkeypatch,  # noqa: F811
):
    args = inputs(factory, monkeypatch)
    identity = run(args)
    assert run(args) == identity
    del args
    with factory() as session:
        rows = session.execute(text("SELECT id,payload FROM overnight_sprint_cycles")).all()
        assert len(rows) == 1 and rows[0][0] == writer.PREFIX+identity
        saved = json.loads(rows[0][1])
        assert saved["status"] == "PREPARATION_BLOCKED_COST_UNKNOWN"
        assert saved["phase3n_status"] == saved["phase3m_status"] == "NOT_ATTESTED"
        assert saved["execution_authority"] is False
        assert "FULL_PROVENANCE_NOT_VERIFIED" in saved["blockers"]
        replayed = replay_cost_record(
            saved["cost_record"], expected_decision=saved["decision_inputs"],
        )
        assert replayed == saved["cost_assessment"]
        assert replayed["full_net_ev"] is None
        for table in ("paper_orders", "paper_fills", "overnight_shadow", "advanced_risk_decisions"):
            assert session.execute(text(f"SELECT count(*) FROM {table}")).scalar_one() == 0


@pytest.mark.parametrize("change,error", [
    ("account", "ACCOUNT_IDENTITY_MISMATCH"),
    ("verdict", "COST_RECORD_RECOMPUTATION_MISMATCH"),
    ("objective", "CURRENT_AUTHORIZATION_REQUIRED"),
    ("orders", "ORDERS_DISABLED_REQUIRED"),
])
def test_invalid_evidence_or_authorization_writes_nothing(
    factory, monkeypatch, change, error,  # noqa: F811
):
    args = inputs(factory, monkeypatch)
    if change == "account":
        args["account_identity_sha256"] = "f" * 64
    elif change == "verdict":
        args["cost_record"]["assessment"]["exchange_fee"]["value"] = "0"
    elif change == "objective":
        args["objective_bytes"] = b"different request"
    else:
        args["settings"].paper_order_creation_enabled = True
    with pytest.raises(ValueError, match=error):
        run(args)
    with factory() as session:
        assert session.execute(text(
            "SELECT count(*) FROM overnight_sprint_cycles"
        )).scalar_one() == 0


def test_clock_expiry_during_replay_rolls_back_without_a_checkpoint(
    factory, monkeypatch,  # noqa: F811
):
    args = inputs(factory, monkeypatch)
    times = iter((NOW, NOW+timedelta(seconds=61)))
    monkeypatch.setattr(writer, "utc_now", lambda: next(times))
    with pytest.raises(ValueError, match="EXPIRED_BEFORE_WRITE"):
        run(args)
    with factory() as session:
        assert session.execute(text(
            "SELECT count(*) FROM overnight_sprint_cycles"
        )).scalar_one() == 0


def test_ended_owner_cannot_write_preparation_record(factory, monkeypatch):  # noqa: F811
    args = inputs(factory, monkeypatch)
    with acquire_runtime_owner(args["database_path"]) as owner:
        pass
    with pytest.raises(ValueError, match="RUNTIME_OWNER_NOT_ACTIVE"):
        writer.persist_cf_cost_block(**args, runtime_owner=owner)


def test_linked_database_path_is_rejected(factory, monkeypatch):  # noqa: F811
    args = inputs(factory, monkeypatch)
    path = args["database_path"]
    linked = path.with_name("linked.db")
    linked.symlink_to(path)
    with acquire_runtime_owner(path) as owner:
        args["database_path"] = linked
        with pytest.raises(ValueError, match="ISOLATED_DATABASE_REQUIRED"):
            writer.persist_cf_cost_block(**args, runtime_owner=owner)


def test_dashboard_reads_block_without_counting_a_candidate_or_trade(
    factory, monkeypatch,  # noqa: F811
):
    from kalshi_predictor.overnight_paper.dashboard import render, snapshot

    args = inputs(factory, monkeypatch)
    identity = run(args)
    path = args["database_path"]
    original = path.read_bytes()
    result = snapshot(path)
    assert path.read_bytes() == original
    assert result["cf_preparation_block_count"] == 1
    latest = result["latest_cf_preparation_block"]
    assert latest["preparation_id"] == identity
    assert latest["phase3n_status"] == "NOT_ATTESTED"
    assert latest["full_net_ev"] is None
    assert "cost_record" not in latest
    assert result["research_assessment_count"] == result["shadow_candidates"] == 0
    assert result["open_positions"] == result["settled"] == result["evaluated"] == 0
    assert result["qualification_current"] is False
    page = render(result)
    assert "Recorded incomplete CF preparation" in page
    assert "They do not establish current source" in page
    assert "payload_hex" not in page


@pytest.mark.parametrize("field,value", [
    ("execution_authority", True), ("phase3n_status", "ALLOW"),
    ("status", "PAPER_ELIGIBLE"),
])
def test_dashboard_rejects_promoted_preparation_labels(
    factory, monkeypatch, field, value,  # noqa: F811
):
    from kalshi_predictor.overnight_paper.dashboard import snapshot

    args = inputs(factory, monkeypatch)
    identity = run(args)
    with factory() as session:
        payload = json.loads(session.execute(text(
            "SELECT payload FROM overnight_sprint_cycles WHERE id=:id"
        ), {"id": writer.PREFIX+identity}).scalar_one())
        payload[field] = value
        session.execute(text(
            "UPDATE overnight_sprint_cycles SET payload=:payload WHERE id=:id"
        ), {"id": writer.PREFIX+identity, "payload": json.dumps(payload)})
        session.commit()
    result = snapshot(args["database_path"])
    assert result["paper_mode"] == "UNVERIFIED"
    assert result["cf_preparation_block_count"] is None
    assert result["first_blocker"] == "PAPER_DASHBOARD_EVIDENCE_INVALID"


def test_cf_runner_routes_incomplete_preparation_to_visible_block(
    factory, monkeypatch,  # noqa: F811
):
    from kalshi_predictor.overnight_paper import cf_research_runner as runner
    from kalshi_predictor.overnight_paper.activation import ExactReleaseEvidence
    from kalshi_predictor.overnight_paper.dashboard import snapshot

    args = inputs(factory, monkeypatch)
    monkeypatch.setattr(runner, "utc_now", lambda: NOW)
    args["paper_decision"] = args.pop("decision")
    args["preparation_cost_record"] = args.pop("cost_record")
    args["preparation_account_identity_sha256"] = args.pop("account_identity_sha256")
    at = args.pop("decision_at")
    args["provenance_args"] = {"decision": {"decision_at": at.isoformat()}, "phase3n": None}
    release = Mock(spec=ExactReleaseEvidence)
    with acquire_runtime_owner(args["database_path"]) as owner:
        result = runner.run_cf_research_cycle(**args, runtime_owner=owner, release=release)
    assert result.state == "PREPARATION_BLOCKED"
    assert result.shadow_id is result.order_id is None
    report = snapshot(args["database_path"])
    assert report["cf_preparation_block_count"] == 1
    assert report["latest_cf_preparation_block"]["preparation_id"] == result.decision_id
    assert report["research_assessment_count"] == report["shadow_candidates"] == 0
    release.verify.assert_not_called()


def test_unconfigured_dashboard_does_not_report_zero_recorded_attempts():
    from kalshi_predictor.overnight_paper.dashboard import render, snapshot

    result = snapshot(None)
    assert result["cf_preparation_block_count"] is None
    assert "Recorded attempts: unavailable" in render(result)
