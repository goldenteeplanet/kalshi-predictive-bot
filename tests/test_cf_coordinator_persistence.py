"""Actual source gates and SQLite transactions; no passing-gate mocks."""

import json
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from test_cf_candidate_assembly import preparation
from test_overnight_activation import baseline_template  # noqa: F401
from test_paper_release_dataset_store import factory  # noqa: F401

from kalshi_predictor.config import Settings
from kalshi_predictor.overnight_paper.activation import ExactReleaseEvidence
from kalshi_predictor.overnight_paper.boundary import LocalPaperAuthorization
from kalshi_predictor.overnight_paper.cf_candidate_assembly import assemble_cf_research_candidate
from kalshi_predictor.overnight_paper.coordinator import admit_prepared_candidate


def test_cf_rejection_commits_once_and_survives_engine_restart_without_orders(factory):  # noqa: F811
    paper, provenance = preparation()
    candidate = assemble_cf_research_candidate(paper_decision=paper, provenance_args=provenance)
    path = Path(factory.kw["bind"].url.database)
    now = provenance["now"]
    release = Mock(spec=ExactReleaseEvidence)
    args = dict(
        session_factory=factory, database_path=path, candidate=candidate,
        authorization=LocalPaperAuthorization(
            now, now+timedelta(hours=1), "0"*64, max_new_positions=1,
        ),
        objective_bytes=b"synthetic CF persistence test", release=release,
        settings=Settings(
            execution_enabled=False, execution_dry_run=True, execution_kill_switch=True,
            execution_gateway_mode="disabled", autopilot_enabled=False, autopilot_dry_run=True,
            kalshi_api_key_id=None, kalshi_private_key_path=None,
            postgres_password="", execution_confirmation_token="",
        ),
        now=now, entries_enabled=True,
    )
    first = admit_prepared_candidate(**args)
    assert first.state == "BLOCKED"
    assert "POSITIVE_NET_EV" in first.blockers
    factory.kw["bind"].dispose()
    restarted = create_engine(f"sqlite:///{path}")
    try:
        second = admit_prepared_candidate(**(args | {"session_factory": sessionmaker(restarted)}))
        assert second == first
        with sessionmaker(restarted)() as session:
            for table in ("paper_orders", "paper_fills", "overnight_shadow"):
                assert session.execute(text(f"SELECT count(*) FROM {table}")).scalar_one() == 0
            saved = session.execute(text(
                "SELECT payload FROM overnight_sprint_cycles "
                "WHERE id LIKE 'release-research-v1:%'"
            )).scalars().all()
            assert len(saved) == 1
            record = json.loads(saved[0])
            assert record["full_net_ev"] is None
            assert record["execution_authority"] is False
            assert "COST_UNKNOWN" in record["research_blockers"]
            assert record["cost_evidence_status"] == "ORIGINAL_EVIDENCE_REPLAYED"
            assert "FEE_APPLICABILITY_UNKNOWN" in record["research_blockers"]
            assert "EXPECTED_SLIPPAGE_UNKNOWN" in record["research_blockers"]
            assert record["cost_assessment"]["full_net_ev"] is None
            checkpoint = json.loads(session.execute(text(
                "SELECT payload FROM overnight_sprint_cycles "
                "WHERE id LIKE 'release-qualification:%'"
            )).scalar_one())
            assert checkpoint["shadow_payload"]["cf_context"]["target"]["symbol"] == "SOL"
            from kalshi_predictor.crypto.cost_record import (
                cost_decision_from_qualification,
                replay_cost_record,
            )
            replayed = replay_cost_record(
                checkpoint["shadow_payload"]["cost_record"],
                expected_decision=cost_decision_from_qualification(checkpoint["decision_inputs"]),
            )
            assert replayed == record["cost_assessment"]
    finally:
        restarted.dispose()
    release.verify.assert_not_called()
