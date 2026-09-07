import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from kalshi_predictor.config import Settings
from kalshi_predictor.roadmap.category_contract import (
    CategoryPipelineEvidence,
    certify_category_pipeline,
)
from kalshi_predictor.roadmap.database_authority import (
    DatabaseParityEvidence,
    certify_postgres_authority,
)
from kalshi_predictor.roadmap.execution_gateway import (
    ApprovedOrderIntent,
    DisabledExecutionGateway,
    LaunchEnvelope,
    authorize_intent,
)
from kalshi_predictor.roadmap.paper_scale import (
    PaperScaleEvidence,
    build_zero_trade_diagnosis,
    evaluate_paper_scale_gate,
)
from kalshi_predictor.roadmap.status import build_roadmap_status


def test_category_contract_requires_complete_current_external_evidence() -> None:
    evidence = CategoryPipelineEvidence(
        category="crypto",
        generated_at=datetime.now(UTC).isoformat(),
        source_state="READY",
        source_name="coinbase",
        source_available_at=datetime.now(UTC).isoformat(),
        active_markets=2,
        verified_links=2,
        fresh_snapshots=2,
        fresh_features=2,
        forecasts=2,
        rankings=2,
        opportunity_rows=1,
        risk_evidence_rows=1,
        complete_paper_traces=1,
        live_v1_allowed=True,
    )
    result = certify_category_pipeline(evidence)
    assert result["paper_pipeline_certified"] is True
    assert result["live_v1_scope_certified"] is True

    manual = certify_category_pipeline(
        CategoryPipelineEvidence(
            **{
                **evidence.as_payload(),
                "category": "economic",
                "synthetic_or_manual_only": True,
            }
        )
    )
    assert manual["paper_pipeline_certified"] is False
    assert manual["live_v1_scope_certified"] is False


def test_paper_gate_preserves_100_total_and_30_per_live_category() -> None:
    result = evaluate_paper_scale_gate(
        PaperScaleEvidence(
            settled_total=105,
            settled_by_category={"crypto": 50, "weather": 55},
            net_pnl_after_costs="12.50",
        )
    )
    assert result["passed"] is True
    assert result["paper_order_creation_enabled"] is False
    assert result["live_execution_enabled"] is False

    contaminated = evaluate_paper_scale_gate(
        PaperScaleEvidence(
            settled_total=105,
            settled_by_category={"crypto": 50, "weather": 55},
            net_pnl_after_costs="12.50",
            synthetic_trades=10,
        )
    )
    assert contaminated["passed"] is False
    assert contaminated["eligible_settled_trades"] == 95


def test_zero_trade_diagnosis_is_deterministic_and_never_lowers_thresholds() -> None:
    result = build_zero_trade_diagnosis({"risk": 3, "no_snapshot": 1})
    assert result["primary_reason"] == "no_snapshot"
    assert result["thresholds_lowered"] is False


def test_postgres_authority_requires_parity_restore_rollback_and_concurrency() -> None:
    evidence = DatabaseParityEvidence(
        sqlite_counts={"markets": 10, "paper_orders": 4},
        postgres_counts={"markets": 10, "paper_orders": 4},
        sqlite_schema_revision="0012",
        postgres_schema_revision="0012",
        backup_restore_passed=True,
        rollback_rehearsal_passed=True,
        concurrency_rehearsal_passed=True,
    )
    assert certify_postgres_authority(evidence)["passed"] is True

    mismatch = DatabaseParityEvidence(
        **{**evidence.__dict__, "postgres_counts": {"markets": 9, "paper_orders": 4}}
    )
    result = certify_postgres_authority(mismatch)
    assert result["passed"] is False
    assert result["count_mismatches"]["markets"] == {"sqlite": 10, "postgres": 9}


def test_gateway_is_disabled_by_default_and_authorization_fails_closed() -> None:
    assert Settings().execution_gateway_mode == "disabled"
    gateway = DisabledExecutionGateway()
    intent = ApprovedOrderIntent(
        intent_id="intent-1",
        ticker="KXBTC-TEST",
        category="crypto",
        side="yes",
        action="buy",
        quantity=1,
        limit_price_cents=45,
        phase_3n_approved=True,
        operator_confirmed=True,
        idempotency_key="intent-1",
    )
    with pytest.raises(PermissionError):
        gateway.submit_order(intent)

    envelope = LaunchEnvelope(
        environment="prod",
        account_id="acct-redacted",
        deployed_sha="a" * 40,
        config_hash="b" * 64,
        model_hashes={"crypto_v2": "c" * 64},
    )
    blocked = authorize_intent(
        intent,
        envelope,
        current_environment="prod",
        current_sha="a" * 40,
    )
    assert blocked["authorized"] is False
    assert "phase_3v_approved" in blocked["blocking_reasons"]
    assert blocked["network_call_performed"] is False


def test_roadmap_status_stops_at_first_missing_evidence(tmp_path: Path) -> None:
    reports = tmp_path / "reports"
    gh2 = reports / "phase_gh2"
    gh2.mkdir(parents=True)
    (gh2 / "gh2_active_candidate_refresh.json").write_text(
        json.dumps({"soak": {"soak_complete": False}}), encoding="utf-8"
    )
    result = build_roadmap_status(reports)
    assert result["phases"][0]["status"] == "PASSED"
    assert result["phases"][1]["status"] == "BLOCKED"
    assert result["phases"][9]["status"] == "BLOCKED"
    assert result["live_execution_enabled"] is False
