from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import func, select
from typer.testing import CliRunner

from kalshi_predictor.cli import app
from kalshi_predictor.config import Settings
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.repositories import encode_json
from kalshi_predictor.data.schema import (
    FeatureCandidate,
    FeatureDiscoveryRun,
    FeatureEvaluation,
    FeatureRecommendation,
    ForecastMemory,
    TradeMemory,
)
from kalshi_predictor.feature_discovery.contracts import FeatureDiscoveryConfig
from kalshi_predictor.feature_discovery.dataset import build_phase3o_discovery_dataset
from kalshi_predictor.feature_discovery.engine import run_feature_discovery
from kalshi_predictor.feature_discovery.experiment import export_feature_experiment_spec
from kalshi_predictor.feature_discovery.grammar import (
    CandidateValidationError,
    candidate_from_expression,
)
from kalshi_predictor.scheduler import scheduler_plan


def test_dataset_excludes_future_features_and_future_labels(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    config = FeatureDiscoveryConfig(min_samples=1)
    with session_factory() as session:
        _seed_forecast_memory(
            session,
            forecast_id="feature-leak",
            sequence=1,
            feature_observed_through=_dt("2026-06-22T11:00:00-05:00"),
            label_available_at=_dt("2026-06-22T18:00:00-05:00"),
        )
        _seed_forecast_memory(
            session,
            forecast_id="future-label",
            sequence=1,
            feature_observed_through=_dt("2026-06-22T10:00:00-05:00"),
            label_available_at=_dt("2026-06-24T18:00:00-05:00"),
        )
        _seed_forecast_memory(
            session,
            forecast_id="valid",
            sequence=1,
            feature_observed_through=_dt("2026-06-22T10:00:00-05:00"),
            label_available_at=_dt("2026-06-22T18:00:00-05:00"),
        )

        rows, manifest = build_phase3o_discovery_dataset(
            session,
            training_as_of=_dt("2026-06-23T02:00:00-05:00"),
            config=config,
        )

    assert [row.source_memory_id for row in rows] == ["valid-1"]
    assert manifest.excluded_counts["feature_observed_after_decision"] == 1
    assert manifest.excluded_counts["label_after_training_cutoff"] == 1


def test_net_profitability_uses_net_after_costs(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    config = FeatureDiscoveryConfig(min_samples=1)
    with session_factory() as session:
        _seed_trade_memory(
            session,
            trade_id="gross-positive-net-negative",
            day_offset=0,
            opportunity_score="90",
            gross_pnl="1.00",
            net_pnl="-0.25",
            total_cost="1.25",
        )

        rows, _ = build_phase3o_discovery_dataset(
            session,
            training_as_of=_dt("2026-06-23T02:00:00-05:00"),
            config=config,
        )

    assert len(rows) == 1
    assert rows[0].outcome_name == "net_profitable_after_costs"
    assert rows[0].outcome_value == Decimal("0")
    assert rows[0].total_cost == Decimal("1.25")


def test_candidate_grammar_canonicalizes_and_rejects_leakage() -> None:
    config = FeatureDiscoveryConfig()
    left = candidate_from_expression(
        {"operator": "raw", "sources": ["opportunity_score"]},
        config=config,
    )
    right = candidate_from_expression(
        {"sources": ["opportunity_score"], "operator": "raw"},
        config=config,
    )

    assert left.candidate_id == right.candidate_id

    with pytest.raises(CandidateValidationError) as forbidden:
        candidate_from_expression({"operator": "raw", "sources": ["net_pnl"]}, config=config)
    assert forbidden.value.reason_code == "forbidden_leakage_source"

    with pytest.raises(CandidateValidationError) as centered:
        candidate_from_expression(
            {
                "operator": "trailing_mean",
                "sources": ["opportunity_score"],
                "window_seconds": 60,
                "window_position": "centered",
            },
            config=config,
        )
    assert centered.value.reason_code == "centered_window_rejected"

    with pytest.raises(CandidateValidationError):
        candidate_from_expression(
            {"operator": "raw", "sources": ["opportunity_score"]},
            config=config,
            lineage={"renamed_from": "settlement_price"},
        )


def test_feature_discovery_run_persists_scorecards_and_report(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    output = tmp_path / "feature_discovery.md"
    json_output = tmp_path / "feature_discovery.json"
    with session_factory() as session:
        _seed_predictive_trade_set(session)
        result = run_feature_discovery(
            session,
            training_as_of=_dt("2026-06-30T02:00:00-05:00"),
            output_path=output,
            json_output_path=json_output,
            settings=_settings(),
        )
        session.commit()
        run_count = session.scalar(select(func.count()).select_from(FeatureDiscoveryRun))
        candidate_count = session.scalar(select(func.count()).select_from(FeatureCandidate))
        evaluation_count = session.scalar(select(func.count()).select_from(FeatureEvaluation))
        recommendation_count = session.scalar(
            select(func.count()).select_from(FeatureRecommendation)
        )

    assert result.status == "COMPLETED"
    assert result.manifest.rows_included == 6
    assert result.candidate_counts["generated"] > 0
    assert run_count == 1
    assert candidate_count and candidate_count > 0
    assert evaluation_count and evaluation_count > 0
    assert recommendation_count and recommendation_count > 0
    assert "Phase 3Q Feature Discovery Report" in output.read_text(encoding="utf-8")
    assert json_output.exists()


def test_feature_discovery_idempotent_retry_publishes_once(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed_predictive_trade_set(session)
        first = run_feature_discovery(
            session,
            training_as_of=_dt("2026-06-30T02:00:00-05:00"),
            output_path=tmp_path / "first.md",
            json_output_path=tmp_path / "first.json",
            settings=_settings(),
        )
        session.commit()
        second = run_feature_discovery(
            session,
            training_as_of=_dt("2026-06-30T02:00:00-05:00"),
            output_path=tmp_path / "second.md",
            json_output_path=tmp_path / "second.json",
            settings=_settings(),
        )
        run_count = session.scalar(select(func.count()).select_from(FeatureDiscoveryRun))

    assert first.idempotent is False
    assert second.idempotent is True
    assert run_count == 1


def test_experiment_export_requires_human_approval_reference(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed_predictive_trade_set(session)
        run_feature_discovery(
            session,
            training_as_of=_dt("2026-06-30T02:00:00-05:00"),
            output_path=tmp_path / "report.md",
            json_output_path=tmp_path / "report.json",
            settings=_settings(),
        )
        evaluation = session.scalar(
            select(FeatureEvaluation)
            .where(FeatureEvaluation.status != "REJECTED")
            .limit(1)
        )
        assert evaluation is not None
        with pytest.raises(ValueError):
            export_feature_experiment_spec(
                session,
                evaluation_id=evaluation.evaluation_id,
                human_approval_reference="",
                output_path=tmp_path / "experiment.json",
            )
        output = export_feature_experiment_spec(
            session,
            evaluation_id=evaluation.evaluation_id,
            human_approval_reference="human-ticket-123",
            output_path=tmp_path / "experiment.json",
        )

    assert output.exists()
    assert "human-ticket-123" in output.read_text(encoding="utf-8")


def test_phase_3q_cli_and_scheduler_smoke() -> None:
    runner = CliRunner()
    for command in (
        "feature-discovery-status",
        "feature-discovery-run",
        "feature-discovery-report",
        "feature-experiment-export",
    ):
        result = runner.invoke(app, [command, "--help"])
        assert result.exit_code == 0
        assert "Usage" in result.output
    assert scheduler_plan("feature-discovery-nightly")[0].command.startswith(
        "kalshi-bot feature-discovery-run"
    )


def test_phase_3q_config_blocks_production_mutation() -> None:
    with pytest.raises(ValueError, match="production mutation"):
        FeatureDiscoveryConfig(allow_production_mutation=True).validate()


def _session_factory(tmp_path):
    engine = init_db(f"sqlite:///{Path(tmp_path) / 'phase3q.db'}")
    return get_session_factory(engine)


def _settings() -> Settings:
    return Settings(
        phase_3q_min_samples=2,
        phase_3q_min_practical_effect=Decimal("0.01"),
        phase_3q_q_value_threshold=Decimal("1.0"),
        phase_3q_purge_seconds=0,
        phase_3q_embargo_seconds=0,
    )


def _seed_predictive_trade_set(session) -> None:
    for index, score in enumerate(("10", "20", "30")):
        _seed_trade_memory(
            session,
            trade_id=f"low-{index}",
            day_offset=index,
            opportunity_score=score,
            net_pnl="-1.00",
            total_cost="0.10",
            gross_pnl="-0.90",
        )
    for index, score in enumerate(("80", "90", "95"), start=3):
        _seed_trade_memory(
            session,
            trade_id=f"high-{index}",
            day_offset=index,
            opportunity_score=score,
            net_pnl="1.00",
            total_cost="0.10",
            gross_pnl="1.10",
        )


def _seed_forecast_memory(
    session,
    *,
    forecast_id: str,
    sequence: int,
    feature_observed_through: datetime,
    label_available_at: datetime,
) -> None:
    session.add(
        ForecastMemory(
            forecast_memory_event_id=f"{forecast_id}-{sequence}",
            forecast_id=forecast_id,
            event_type="FORECAST_OUTCOME_FINALIZED",
            event_sequence=sequence,
            event_time=_dt("2026-06-22T10:00:00-05:00"),
            observed_at=_dt("2026-06-22T10:00:00-05:00"),
            recorded_at=_dt("2026-06-22T10:00:01-05:00"),
            source_component="test",
            idempotency_key=f"{forecast_id}-{sequence}",
            payload_hash=f"sha256:{forecast_id}-{sequence}",
            metadata_json="{}",
            instrument_id="P3Q-FORECAST",
            strategy_id="ensemble_v2",
            timeframe="intraday",
            direction="YES",
            forecast_generated_at=_dt("2026-06-22T10:00:00-05:00"),
            forecast_target_at=_dt("2026-06-22T18:00:00-05:00"),
            forecast_type="BINARY_PROBABILITY",
            predicted_probability="0.70",
            confidence_score="0.70",
            opportunity_score="80",
            decision_status="ELIGIBLE",
            reason_codes_json=encode_json([]),
            phase_3m_proposed_contracts=1,
            phase_3n_approved_contracts=1,
            phase_3n_reason_codes_json=encode_json([]),
            primary_model_id="ensemble_v2",
            primary_model_version="v1",
            primary_model_artifact_hash="sha256:model-v1",
            feature_schema_version="features-v1",
            feature_vector_hash=f"sha256:{forecast_id}",
            feature_observed_through=feature_observed_through,
            model_lineage_json="{}",
            feature_lineage_json="{}",
            forecast_outcome_status="FINAL",
            label_available_at=label_available_at,
            outcome_finalized_at=label_available_at,
            direction_correct=1,
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
    day_offset: int,
    opportunity_score: str,
    net_pnl: str,
    total_cost: str,
    gross_pnl: str = "0",
    execution_mode: str = "PAPER",
) -> None:
    decision = _dt("2026-06-22T10:00:00-05:00") + timedelta(days=day_offset)
    settled = decision + timedelta(hours=2)
    session.add(
        TradeMemory(
            trade_memory_event_id=f"{trade_id}-1",
            trade_id=trade_id,
            event_type="TRADE_OUTCOME_FINALIZED",
            event_sequence=1,
            event_time=decision,
            observed_at=decision,
            recorded_at=decision + timedelta(seconds=1),
            source_component="test",
            idempotency_key=f"{trade_id}-1",
            payload_hash=f"sha256:{trade_id}-1",
            metadata_json="{}",
            forecast_id=f"forecast-{trade_id}",
            execution_mode=execution_mode,
            instrument_id="P3Q-TRADE",
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
            confidence_score="0.70",
            opportunity_score=opportunity_score,
            risk_adjusted_expected_value="0.10",
            gross_pnl=gross_pnl,
            net_pnl=net_pnl,
            total_cost=total_cost,
            paper_fill_policy_json="{}",
            settlement_status="FINAL",
            settled_at=settled,
            outcome_finalized_at=settled,
            outcome_class="WIN" if Decimal(net_pnl) > 0 else "LOSS",
            outcome_reason_codes_json="[]",
            ingestion_mode="LIVE",
            data_quality_flags_json="[]",
            event_payload_json="{}",
        )
    )
    session.flush()


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value)
