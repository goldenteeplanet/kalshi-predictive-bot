import json
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import func, select
from typer.testing import CliRunner

from kalshi_predictor.cli import app
from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.repositories import encode_json
from kalshi_predictor.data.schema import (
    ForecastMemory,
    PaperOrder,
    RlBehaviorDecision,
    RlPolicyDecision,
    RlRewardLedger,
    RlRun,
    TradeMemory,
)
from kalshi_predictor.reinforcement_learning.contracts import (
    ACTION_PROCEED,
    ACTION_SKIP,
    EVIDENCE_PAPER,
    MODE_OFFLINE_REPLAY,
    MODE_SHADOW,
    STATUS_COMPLETED,
    RewardDefinition,
    RLConfig,
)
from kalshi_predictor.reinforcement_learning.dataset import build_rl_dataset
from kalshi_predictor.reinforcement_learning.engine import run_rl_evaluation
from kalshi_predictor.reinforcement_learning.reward import reward_for_trade
from kalshi_predictor.reinforcement_learning.serving import recommend_policy_action
from kalshi_predictor.scheduler import scheduler_plan


def test_phase_3s_config_blocks_online_exploration_and_ungoverned_gate() -> None:
    with pytest.raises(ValueError, match="online exploration"):
        RLConfig(enabled=True, mode=MODE_OFFLINE_REPLAY, allow_online_exploration=True).validate()

    with pytest.raises(ValueError, match="explicit enablement"):
        RLConfig(enabled=True, mode="governed_gate").validate()

    with pytest.raises(ValueError, match="requires PHASE_3S_MODE"):
        RLConfig(
            enabled=True,
            mode=MODE_OFFLINE_REPLAY,
            governed_gate_enabled=True,
        ).validate()


def test_reward_uses_finalized_net_roi_and_rejects_invalid_denominator() -> None:
    settled = _dt("2026-06-23T12:00:00+00:00")
    trade = _trade_memory(
        trade_id="reward-win",
        forecast_id="forecast-reward-win",
        net_pnl="1.00",
        gross_pnl="1.10",
        total_cost="0.10",
        committed_risk="2.00",
        settled_at=settled,
    )

    reward = reward_for_trade(
        trade,
        action=ACTION_PROCEED,
        reward_definition=RewardDefinition(),
    )

    assert reward["reward"] == Decimal("0.5")
    assert reward["raw_reward"] == Decimal("0.5")
    assert reward["evidence_type"] == EVIDENCE_PAPER
    assert reward["reason_codes"] == ["RECOMMEND_PROCEED"]

    invalid = _trade_memory(
        trade_id="reward-invalid",
        forecast_id="forecast-reward-invalid",
        net_pnl="1.00",
        gross_pnl="1.00",
        total_cost=None,
        committed_risk=None,
        risk_per_contract=None,
        gross_notional=None,
        fill_price=None,
        settled_at=settled,
    )

    invalid_reward = reward_for_trade(
        invalid,
        action=ACTION_PROCEED,
        reward_definition=RewardDefinition(),
    )

    assert invalid_reward["reward_status"] == "UNAVAILABLE"
    assert invalid_reward["reason_codes"] == ["REWARD_INVALID"]


def test_dataset_excludes_future_features_and_synthetic_markets(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    cutoff = _dt("2026-06-23T12:00:00+00:00")
    with session_factory() as session:
        _seed_forecast_memory(
            session,
            forecast_id="valid-skip",
            ticker="P3S-VALID",
            decision_status="NO_TRADE",
            opportunity_score="10",
            confidence_score="0.10",
        )
        _seed_forecast_memory(
            session,
            forecast_id="future-features",
            ticker="P3S-FUTURE",
            decision_status="NO_TRADE",
            feature_observed_through=_dt("2026-06-23T13:00:00+00:00"),
        )
        _seed_forecast_memory(
            session,
            forecast_id="synthetic-market",
            ticker="synthetic:p3s",
            decision_status="NO_TRADE",
        )
        dataset = build_rl_dataset(
            session,
            training_as_of=cutoff,
            config=_rl_config(min_rows=1),
            reward_definition=RewardDefinition(),
        )

    assert len(dataset.rows) == 1
    assert dataset.rows_total == 3
    assert dataset.rows[0].chosen_action == ACTION_SKIP
    assert dataset.exclusion_counts == {
        "feature_observed_after_decision": 1,
        "synthetic_market_no_realized_roi": 1,
    }


def test_rl_evaluation_persists_audit_rows_and_no_order_state(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    cutoff = _dt("2026-06-23T12:00:00+00:00")
    output = tmp_path / "rl.md"
    json_output = tmp_path / "rl.json"
    with session_factory() as session:
        _seed_supported_rl_memory(session)
        result = run_rl_evaluation(
            session,
            training_as_of=cutoff,
            output_path=output,
            json_output_path=json_output,
            settings=_settings(min_rows=4),
        )
        session.commit()
        run_count = session.scalar(select(func.count()).select_from(RlRun))
        decision_count = session.scalar(select(func.count()).select_from(RlBehaviorDecision))
        reward_count = session.scalar(select(func.count()).select_from(RlRewardLedger))
        paper_order_count = session.scalar(select(func.count()).select_from(PaperOrder))

    assert result.status == STATUS_COMPLETED
    assert result.mode == MODE_OFFLINE_REPLAY
    assert len(result.dataset.rows) == 4
    assert run_count == 1
    assert decision_count == 4
    assert reward_count == 4
    assert paper_order_count == 0
    assert "Phase 3M remains sizing authority" in output.read_text(encoding="utf-8")
    assert json.loads(json_output.read_text(encoding="utf-8"))["action_space"] == [
        "SKIP",
        "PROCEED",
    ]


def test_rl_evaluation_is_idempotent_for_same_dataset_and_cutoff(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    cutoff = _dt("2026-06-23T12:00:00+00:00")
    with session_factory() as session:
        _seed_supported_rl_memory(session)
        first = run_rl_evaluation(
            session,
            training_as_of=cutoff,
            output_path=tmp_path / "first.md",
            json_output_path=tmp_path / "first.json",
            settings=_settings(min_rows=4),
        )
        second = run_rl_evaluation(
            session,
            training_as_of=cutoff,
            output_path=tmp_path / "second.md",
            json_output_path=tmp_path / "second.json",
            settings=_settings(min_rows=4),
        )
        run_count = session.scalar(select(func.count()).select_from(RlRun))

    assert first.idempotent is False
    assert second.idempotent is True
    assert run_count == 1


def test_shadow_recommendation_logs_decision_without_quantity_or_orders(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        recommendation = recommend_policy_action(
            opportunity={
                "opportunity_id": "shadow-opportunity",
                "opportunity_score": "90",
                "confidence_score": "0.90",
            },
            config=_rl_config(mode=MODE_SHADOW),
            session=session,
        )
        session.commit()
        policy_decision_count = session.scalar(select(func.count()).select_from(RlPolicyDecision))
        paper_order_count = session.scalar(select(func.count()).select_from(PaperOrder))

    assert recommendation.recommended_action == ACTION_PROCEED
    assert recommendation.baseline_action == ACTION_PROCEED
    assert "SHADOW_ONLY" in recommendation.reason_codes
    assert not hasattr(recommendation, "quantity")
    assert policy_decision_count == 1
    assert paper_order_count == 0


def test_phase_3s_cli_and_scheduler_smoke(tmp_path) -> None:
    assert scheduler_plan("rl-policy-nightly")[0].command.startswith("kalshi-bot rl-evaluate")

    runner = CliRunner()
    for command in (
        "rl-status",
        "rl-dataset",
        "rl-train",
        "rl-evaluate",
        "rl-shadow-report",
        "rl-drift-report",
    ):
        result = runner.invoke(app, [command, "--help"])
        assert result.exit_code == 0
        assert "Usage" in result.output

    get_settings.cache_clear()
    db_url = f"sqlite:///{tmp_path / 'cli.db'}"
    output = tmp_path / "cli.md"
    json_output = tmp_path / "cli.json"
    result = runner.invoke(
        app,
        [
            "rl-evaluate",
            "--enable-research",
            "--output",
            str(output),
            "--json-output",
            str(json_output),
        ],
        env={"DATABASE_URL": db_url},
    )
    get_settings.cache_clear()

    assert result.exit_code == 0
    assert "Safety: no order creation" in result.output
    assert output.exists()
    assert json.loads(json_output.read_text(encoding="utf-8"))["formulation"] == (
        "CONTEXTUAL_BANDIT"
    )


def _session_factory(tmp_path):
    engine = init_db(f"sqlite:///{Path(tmp_path) / 'phase3s.db'}")
    return get_session_factory(engine)


def _rl_config(
    *,
    mode: str = MODE_OFFLINE_REPLAY,
    min_rows: int = 1,
    min_support: int = 1,
) -> RLConfig:
    return RLConfig(
        enabled=True,
        mode=mode,
        min_training_rows=min_rows,
        min_action_support=min_support,
        baseline_opportunity_score=Decimal("45"),
        candidate_opportunity_score=Decimal("55"),
        min_lcb_improvement=Decimal("0.001"),
    )


def _settings(*, min_rows: int = 1) -> Settings:
    return Settings(
        phase_3s_reinforcement_learning_enabled=True,
        phase_3s_mode=MODE_OFFLINE_REPLAY,
        phase_3s_min_training_rows=min_rows,
        phase_3s_min_action_support=1,
        phase_3s_baseline_opportunity_score=Decimal("45"),
        phase_3s_candidate_opportunity_score=Decimal("55"),
        phase_3s_min_lcb_improvement=Decimal("0.001"),
    )


def _seed_supported_rl_memory(session) -> None:
    for index, (forecast_id, score, pnl, status) in enumerate(
        (
            ("proceed-win", "80", "1.00", "TRADE_SELECTED"),
            ("proceed-loss", "85", "-0.20", "TRADE_SELECTED"),
            ("skip-low-1", "10", "0", "NO_TRADE"),
            ("skip-low-2", "20", "0", "NO_TRADE"),
        )
    ):
        _seed_forecast_memory(
            session,
            forecast_id=forecast_id,
            ticker=f"P3S-{forecast_id.upper()}",
            decision_status=status,
            opportunity_score=score,
            confidence_score="0.80",
            event_time=_dt("2026-06-23T10:00:00+00:00") + timedelta(minutes=index),
            opportunity_id=f"opp-{forecast_id}" if status == "TRADE_SELECTED" else None,
        )
        if status == "TRADE_SELECTED":
            _seed_trade_memory(
                session,
                trade_id=f"trade-{forecast_id}",
                forecast_id=forecast_id,
                ticker=f"P3S-{forecast_id.upper()}",
                net_pnl=pnl,
                gross_pnl=pnl,
                total_cost="0.10",
                committed_risk="1.00",
                settled_at=_dt("2026-06-23T11:00:00+00:00") + timedelta(minutes=index),
            )


def _seed_forecast_memory(
    session,
    *,
    forecast_id: str,
    ticker: str,
    decision_status: str,
    opportunity_score: str = "80",
    confidence_score: str = "0.70",
    event_time: datetime | None = None,
    opportunity_id: str | None = None,
    feature_observed_through: datetime | None = None,
) -> None:
    event_time = event_time or _dt("2026-06-23T10:00:00+00:00")
    session.add(
        ForecastMemory(
            forecast_memory_event_id=f"{forecast_id}-1",
            forecast_id=forecast_id,
            event_type=(
                "TRADE_SELECTED"
                if decision_status == "TRADE_SELECTED"
                else "FORECAST_CREATED"
            ),
            event_sequence=1,
            event_time=event_time,
            observed_at=event_time,
            recorded_at=event_time + timedelta(seconds=1),
            source_component="test",
            idempotency_key=f"forecast:{forecast_id}:1",
            payload_hash=f"sha256:{forecast_id}:1",
            metadata_json="{}",
            opportunity_id=opportunity_id,
            market_memory_id=f"market-{ticker}" if opportunity_id else None,
            instrument_id=ticker,
            category_id="TEST",
            strategy_id="ensemble_v2",
            timeframe="intraday",
            direction="YES",
            forecast_generated_at=event_time,
            forecast_target_at=event_time + timedelta(hours=1),
            forecast_type="BINARY_PROBABILITY",
            predicted_probability="0.70",
            confidence_score=confidence_score,
            opportunity_score=opportunity_score,
            decision_status=decision_status,
            reason_codes_json=encode_json([]),
            phase_3m_proposed_contracts=1 if opportunity_id else None,
            phase_3n_action="ALLOW",
            phase_3n_approved_contracts=1 if opportunity_id else None,
            phase_3n_reason_codes_json=encode_json([]),
            primary_model_id="ensemble_v2",
            primary_model_version="v1",
            primary_model_artifact_hash="sha256:model-v1",
            feature_schema_version="features-v1",
            feature_vector_hash=f"sha256:feature:{forecast_id}",
            feature_observed_through=feature_observed_through or event_time - timedelta(seconds=1),
            model_lineage_json="{}",
            feature_lineage_json="{}",
            forecast_outcome_status="FINAL",
            label_available_at=event_time + timedelta(hours=1),
            outcome_finalized_at=event_time + timedelta(hours=1),
            ingestion_mode="LIVE",
            data_quality_flags_json="[]",
            event_payload_json="{}",
        )
    )
    session.flush()


def _seed_trade_memory(
    session,
    *,
    trade_id: str,
    forecast_id: str,
    ticker: str,
    net_pnl: str,
    gross_pnl: str,
    total_cost: str | None,
    committed_risk: str | None,
    settled_at: datetime,
) -> None:
    session.add(
        _trade_memory(
            trade_id=trade_id,
            forecast_id=forecast_id,
            ticker=ticker,
            net_pnl=net_pnl,
            gross_pnl=gross_pnl,
            total_cost=total_cost,
            committed_risk=committed_risk,
            settled_at=settled_at,
        )
    )
    session.flush()


def _trade_memory(
    *,
    trade_id: str,
    forecast_id: str,
    net_pnl: str,
    gross_pnl: str,
    total_cost: str | None,
    committed_risk: str | None,
    settled_at: datetime,
    ticker: str = "P3S-TRADE",
    risk_per_contract: str | None = "1.00",
    gross_notional: str | None = "1.00",
    fill_price: str | None = "1.00",
) -> TradeMemory:
    return TradeMemory(
        trade_memory_event_id=f"{trade_id}-1",
        trade_id=trade_id,
        event_type="TRADE_OUTCOME_FINALIZED",
        event_sequence=1,
        event_time=settled_at,
        observed_at=settled_at,
        recorded_at=settled_at + timedelta(seconds=1),
        source_component="test",
        idempotency_key=f"trade:{trade_id}:1",
        payload_hash=f"sha256:{trade_id}:1",
        metadata_json="{}",
        forecast_id=forecast_id,
        execution_mode="PAPER",
        instrument_id=ticker,
        category_id="TEST",
        strategy_id="ensemble_v2",
        model_id="ensemble_v2",
        model_version="v1",
        model_lineage_json="{}",
        direction="BUY_YES",
        phase_3m_proposed_contracts=1,
        phase_3n_approved_contracts=1,
        requested_quantity=1,
        accepted_quantity=1,
        filled_quantity=1,
        fill_price=fill_price,
        confidence_score="0.80",
        opportunity_score="80",
        gross_notional=gross_notional,
        risk_per_contract=risk_per_contract,
        committed_risk=committed_risk,
        risk_adjusted_expected_value="0.10",
        gross_pnl=gross_pnl,
        net_pnl=net_pnl,
        total_cost=total_cost,
        paper_fill_policy_json="{}",
        settlement_status="FINAL",
        settled_at=settled_at,
        outcome_finalized_at=settled_at,
        outcome_class="WIN" if Decimal(net_pnl) > 0 else "LOSS",
        outcome_reason_codes_json="[]",
        ingestion_mode="LIVE",
        data_quality_flags_json="[]",
        event_payload_json="{}",
    )


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value)
