from datetime import datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import func, select
from typer.testing import CliRunner

from kalshi_predictor.cli import app
from kalshi_predictor.config import Settings
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.repositories import encode_json
from kalshi_predictor.data.schema import (
    ForecastMemory,
    MarketMemory,
    SelfEvaluationJournal,
    SelfEvaluationMetric,
    TradeMemory,
)
from kalshi_predictor.scheduler import scheduler_plan
from kalshi_predictor.self_evaluation.reports import generate_self_evaluation_report


def test_self_evaluation_generates_final_journal_from_phase3o_memory(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    output = tmp_path / "journal.md"
    json_output = tmp_path / "journal.json"
    with session_factory() as session:
        _seed_market_memory(session, ticker="P3P-FINAL")
        _seed_forecast_memory(
            session,
            forecast_id="forecast-final",
            ticker="P3P-FINAL",
            sequence=1,
            event_type="FORECAST_CREATED",
            status="PENDING",
            model_version="v1",
        )
        _seed_forecast_memory(
            session,
            forecast_id="forecast-final",
            ticker="P3P-FINAL",
            sequence=2,
            event_type="FORECAST_OUTCOME_FINALIZED",
            status="FINAL",
            model_version="v1",
            label_available_at=_dt("2026-06-22T18:00:00-05:00"),
            direction_correct=1,
            brier_component="0.09",
            actual_value="1",
        )
        _seed_trade_memory(
            session,
            trade_id="paper-final",
            ticker="P3P-FINAL",
            sequence=1,
            event_type="TRADE_OUTCOME_FINALIZED",
            execution_mode="PAPER",
            net_pnl="3.00",
            gross_pnl="3.20",
            total_cost="0.20",
            settled_at=_dt("2026-06-22T18:30:00-05:00"),
        )

        result = generate_self_evaluation_report(
            session,
            output_path=output,
            json_output_path=json_output,
            session_date="2026-06-22",
            evaluation_as_of="2026-06-23T02:00:00-05:00",
            settings=_settings(),
        )
        session.commit()
        journals = session.scalars(select(SelfEvaluationJournal)).all()

    assert result.journal_status == "FINAL"
    assert result.payload["coverage_summary"]["finalized_forecasts"] == 1
    assert result.payload["coverage_summary"]["finalized_trades"] == 1
    assert "## What worked" in output.read_text(encoding="utf-8")
    assert json_output.exists()
    assert len(journals) == 1


def test_evaluation_cutoff_excludes_future_labels(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed_forecast_memory(
            session,
            forecast_id="forecast-cutoff",
            ticker="P3P-CUTOFF",
            sequence=1,
            event_type="FORECAST_CREATED",
            status="PENDING",
            recorded_at=_dt("2026-06-22T10:05:00-05:00"),
        )
        _seed_forecast_memory(
            session,
            forecast_id="forecast-cutoff",
            ticker="P3P-CUTOFF",
            sequence=2,
            event_type="FORECAST_OUTCOME_FINALIZED",
            status="FINAL",
            recorded_at=_dt("2026-06-23T03:00:00-05:00"),
            label_available_at=_dt("2026-06-23T03:00:00-05:00"),
            direction_correct=1,
            brier_component="0.04",
        )

        result = generate_self_evaluation_report(
            session,
            output_path=tmp_path / "cutoff.md",
            json_output_path=tmp_path / "cutoff.json",
            session_date="2026-06-22",
            evaluation_as_of="2026-06-23T02:00:00-05:00",
            settings=_settings(),
        )

    coverage = result.payload["coverage_summary"]
    excluded = result.payload["evidence_appendix"]["excluded_rows_by_reason"]
    assert result.journal_status == "PROVISIONAL"
    assert coverage["finalized_forecasts"] == 0
    assert coverage["pending_forecasts"] == 1
    assert excluded["forecast_memory_after_cutoff"] == 1


def test_duplicate_self_evaluation_run_is_idempotent(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed_forecast_memory(
            session,
            forecast_id="forecast-idempotent",
            ticker="P3P-IDEMPOTENT",
            sequence=1,
            event_type="FORECAST_CREATED",
            status="PENDING",
        )
        first = generate_self_evaluation_report(
            session,
            output_path=tmp_path / "first.md",
            json_output_path=tmp_path / "first.json",
            session_date="2026-06-22",
            evaluation_as_of="2026-06-23T02:00:00-05:00",
            settings=_settings(),
        )
        session.commit()
        second = generate_self_evaluation_report(
            session,
            output_path=tmp_path / "second.md",
            json_output_path=tmp_path / "second.json",
            session_date="2026-06-22",
            evaluation_as_of="2026-06-23T02:00:00-05:00",
            settings=_settings(),
        )
        journals = session.scalars(select(SelfEvaluationJournal)).all()

    assert first.idempotent is False
    assert second.idempotent is True
    assert second.journal_id == first.journal_id
    assert len(journals) == 1


def test_late_finalization_creates_append_only_revision(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed_forecast_memory(
            session,
            forecast_id="forecast-revision",
            ticker="P3P-REVISION",
            sequence=1,
            event_type="FORECAST_CREATED",
            status="PENDING",
        )
        first = generate_self_evaluation_report(
            session,
            output_path=tmp_path / "revision_1.md",
            json_output_path=tmp_path / "revision_1.json",
            session_date="2026-06-22",
            evaluation_as_of="2026-06-23T02:00:00-05:00",
            settings=_settings(),
        )
        session.commit()
        _seed_forecast_memory(
            session,
            forecast_id="forecast-revision",
            ticker="P3P-REVISION",
            sequence=2,
            event_type="FORECAST_OUTCOME_FINALIZED",
            status="FINAL",
            recorded_at=_dt("2026-06-23T04:00:00-05:00"),
            label_available_at=_dt("2026-06-23T04:00:00-05:00"),
            direction_correct=1,
            brier_component="0.01",
        )
        second = generate_self_evaluation_report(
            session,
            output_path=tmp_path / "revision_2.md",
            json_output_path=tmp_path / "revision_2.json",
            session_date="2026-06-22",
            evaluation_as_of="2026-06-23T05:00:00-05:00",
            settings=_settings(),
        )
        journals = session.scalars(
            select(SelfEvaluationJournal).order_by(SelfEvaluationJournal.journal_revision)
        ).all()

    assert first.journal_revision == 1
    assert second.journal_revision == 2
    assert len(journals) == 2
    assert journals[1].supersedes_journal_id == journals[0].journal_id


def test_no_activity_session_renders_required_empty_sections(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        result = generate_self_evaluation_report(
            session,
            output_path=tmp_path / "no_activity.md",
            json_output_path=tmp_path / "no_activity.json",
            session_date="2026-06-22",
            evaluation_as_of="2026-06-23T02:00:00-05:00",
            settings=_settings(),
        )

    assert result.journal_status == "NO_ACTIVITY"
    assert "## What worked" in result.markdown
    assert "## What failed" in result.markdown
    assert "## What changed" in result.markdown


def test_trade_metrics_keep_paper_and_live_separate(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed_trade_memory(
            session,
            trade_id="paper-separate",
            ticker="P3P-MODES",
            sequence=1,
            event_type="TRADE_OUTCOME_FINALIZED",
            execution_mode="PAPER",
            net_pnl="2.00",
            settled_at=_dt("2026-06-22T18:00:00-05:00"),
        )
        _seed_trade_memory(
            session,
            trade_id="live-separate",
            ticker="P3P-MODES",
            sequence=1,
            event_type="TRADE_OUTCOME_FINALIZED",
            execution_mode="LIVE",
            net_pnl="-1.00",
            settled_at=_dt("2026-06-22T18:00:00-05:00"),
        )
        result = generate_self_evaluation_report(
            session,
            output_path=tmp_path / "modes.md",
            json_output_path=tmp_path / "modes.json",
            session_date="2026-06-22",
            evaluation_as_of="2026-06-23T02:00:00-05:00",
            settings=_settings(),
        )

    pnl_metrics = [
        metric
        for metric in result.payload["key_metrics"]
        if metric["metric_name"] == "trade.net_pnl.total"
    ]
    cohorts = {metric["cohort"]["execution_mode"]: metric["value"] for metric in pnl_metrics}
    assert cohorts["PAPER"] == 2
    assert cohorts["LIVE"] == -1


def test_phase3n_conflict_becomes_data_quality_finding(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed_forecast_memory(
            session,
            forecast_id="forecast-conflict",
            ticker="P3P-CONFLICT",
            sequence=1,
            event_type="FORECAST_CREATED",
            status="PENDING",
            phase_3m_proposed_contracts=1,
            phase_3n_approved_contracts=3,
            phase_3n_action="ALLOW",
        )
        result = generate_self_evaluation_report(
            session,
            output_path=tmp_path / "conflict.md",
            json_output_path=tmp_path / "conflict.json",
            session_date="2026-06-22",
            evaluation_as_of="2026-06-23T02:00:00-05:00",
            settings=_settings(),
        )

    assert any(
        finding["finding_subtype"] == "PHASE3N_APPROVED_GT_PHASE3M_PROPOSED"
        for finding in result.payload["data_quality_items"]
    )


def test_self_evaluate_cli_and_scheduler_smoke() -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["self-evaluate", "--help"])

    assert result.exit_code == 0
    assert "Usage" in result.output
    assert scheduler_plan("self-evaluation-nightly")[0].command.startswith(
        "kalshi-bot self-evaluate"
    )


def test_self_evaluation_metrics_are_persisted(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed_forecast_memory(
            session,
            forecast_id="forecast-metrics",
            ticker="P3P-METRICS",
            sequence=1,
            event_type="FORECAST_CREATED",
            status="PENDING",
        )
        generate_self_evaluation_report(
            session,
            output_path=tmp_path / "metrics.md",
            json_output_path=tmp_path / "metrics.json",
            session_date="2026-06-22",
            evaluation_as_of="2026-06-23T02:00:00-05:00",
            settings=_settings(),
        )
        metric_count = session.scalar(select(func.count()).select_from(SelfEvaluationMetric))

    assert metric_count is not None
    assert metric_count > 0


def _session_factory(tmp_path):
    engine = init_db(f"sqlite:///{Path(tmp_path) / 'phase3p.db'}")
    return get_session_factory(engine)


def _settings() -> Settings:
    return Settings(
        phase_3p_minimum_current_sample=1,
        phase_3p_minimum_baseline_sample=1,
        phase_3p_minimum_practical_effect_size=0.01,
    )


def _seed_market_memory(session, *, ticker: str = "P3P") -> None:
    session.add(
        MarketMemory(
            market_memory_id=f"market-{ticker}",
            event_type="MARKET_SNAPSHOT",
            event_time=_dt("2026-06-22T10:00:00-05:00"),
            observed_at=_dt("2026-06-22T10:00:00-05:00"),
            recorded_at=_dt("2026-06-22T10:00:01-05:00"),
            source_component="test",
            idempotency_key=f"market-{ticker}",
            payload_hash=f"sha256:market-{ticker}",
            metadata_json="{}",
            instrument_id=ticker,
            timeframe="intraday",
            snapshot_type="DECISION",
            market_event_time=_dt("2026-06-22T10:00:00-05:00"),
            source_name="test",
            feature_values_json="{}",
            data_mode="AS_OBSERVED",
            ingestion_mode="LIVE",
            data_quality_flags_json="[]",
            event_payload_json="{}",
        )
    )
    session.flush()


def _seed_forecast_memory(
    session,
    *,
    forecast_id: str,
    ticker: str,
    sequence: int,
    event_type: str,
    status: str,
    model_version: str = "v1",
    recorded_at: datetime | None = None,
    label_available_at: datetime | None = None,
    direction_correct: int | None = None,
    brier_component: str | None = None,
    actual_value: str | None = None,
    phase_3m_proposed_contracts: int | None = 1,
    phase_3n_approved_contracts: int | None = 1,
    phase_3n_action: str | None = "ALLOW",
) -> None:
    session.add(
        ForecastMemory(
            forecast_memory_event_id=f"{forecast_id}-{sequence}",
            forecast_id=forecast_id,
            event_type=event_type,
            event_sequence=sequence,
            event_time=_dt("2026-06-22T10:00:00-05:00"),
            recorded_at=recorded_at or _dt("2026-06-22T10:00:01-05:00"),
            source_component="test",
            idempotency_key=f"{forecast_id}-{sequence}",
            payload_hash=f"sha256:{forecast_id}-{sequence}",
            metadata_json="{}",
            market_memory_id=f"market-{ticker}",
            instrument_id=ticker,
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
            reason_codes_json=encode_json({"reason_codes": []}),
            phase_3m_tier="MEDIUM",
            phase_3m_proposed_contracts=phase_3m_proposed_contracts,
            phase_3m_config_version="phase3m-test",
            phase_3n_action=phase_3n_action,
            phase_3n_approved_contracts=phase_3n_approved_contracts,
            phase_3n_reason_codes_json=encode_json({"reason_codes": ["test"]}),
            phase_3n_config_version="phase3n-test",
            primary_model_id="ensemble_v2",
            primary_model_version=model_version,
            primary_model_artifact_hash=f"sha256:model-{model_version}",
            feature_schema_version="features-v1",
            feature_vector_hash=f"sha256:features-{forecast_id}",
            model_lineage_json="{}",
            feature_lineage_json="{}",
            forecast_outcome_status=status,
            label_available_at=label_available_at,
            outcome_finalized_at=label_available_at,
            actual_value=actual_value,
            direction_correct=direction_correct,
            brier_component=brier_component,
            outcome_class="YES" if actual_value == "1" else None,
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
    ticker: str,
    sequence: int,
    event_type: str,
    execution_mode: str,
    net_pnl: str | None = None,
    gross_pnl: str | None = None,
    total_cost: str | None = None,
    settled_at: datetime | None = None,
) -> None:
    session.add(
        TradeMemory(
            trade_memory_event_id=f"{trade_id}-{sequence}",
            trade_id=trade_id,
            event_type=event_type,
            event_sequence=sequence,
            event_time=_dt("2026-06-22T10:30:00-05:00"),
            recorded_at=_dt("2026-06-22T10:30:01-05:00"),
            source_component="test",
            idempotency_key=f"{trade_id}-{sequence}",
            payload_hash=f"sha256:{trade_id}-{sequence}",
            metadata_json="{}",
            forecast_id=f"forecast-{trade_id}",
            execution_mode=execution_mode,
            instrument_id=ticker,
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
            gross_pnl=gross_pnl,
            net_pnl=net_pnl,
            total_cost=total_cost,
            paper_fill_policy_json="{}",
            settlement_status="FINAL" if settled_at else "OPEN",
            settled_at=settled_at,
            outcome_finalized_at=settled_at,
            outcome_class="WIN" if net_pnl and Decimal(net_pnl) > 0 else "LOSS",
            outcome_reason_codes_json="[]",
            ingestion_mode="LIVE",
            data_quality_flags_json="[]",
            event_payload_json="{}",
        )
    )
    session.flush()


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value)
