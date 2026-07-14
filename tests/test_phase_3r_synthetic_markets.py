import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import func, select
from typer.testing import CliRunner

from kalshi_predictor.cli import app
from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.repositories import upsert_market
from kalshi_predictor.data.schema import (
    ForecastMemory,
    MarketMemory,
    MarketRanking,
    PaperOrder,
    SyntheticMarketRun,
    SyntheticProbabilityEstimate,
    TradeMemory,
)
from kalshi_predictor.scheduler import scheduler_plan
from kalshi_predictor.synthetic_markets.contracts import (
    DISCLAIMER,
    SyntheticMarketsConfig,
)
from kalshi_predictor.synthetic_markets.engine import run_synthetic_markets
from kalshi_predictor.synthetic_markets.policy import build_candidate_from_payload
from kalshi_predictor.synthetic_markets.repository import synthetic_markets_status


def test_phase_3r_config_blocks_execution_and_opportunity_creation() -> None:
    with pytest.raises(ValueError, match="order actions"):
        SyntheticMarketsConfig(allow_order_actions=True).validate()
    with pytest.raises(ValueError, match="exchange writes"):
        SyntheticMarketsConfig(allow_exchange_write_endpoints=True).validate()
    with pytest.raises(ValueError, match="trading opportunities"):
        SyntheticMarketsConfig(allow_opportunity_creation=True).validate()


def test_candidate_identity_is_deterministic() -> None:
    config = SyntheticMarketsConfig(enabled=True, mode="shadow")
    now = _dt("2026-06-23T12:00:00+00:00")

    first = build_candidate_from_payload(_candidate(), config=config, generated_at=now)
    second = build_candidate_from_payload(
        dict(list(_candidate().items())[::-1]),
        config=config,
        generated_at=now,
    )

    assert first.accepted
    assert second.accepted
    assert first.event is not None
    assert second.event is not None
    assert first.event.synthetic_event_id == second.event.synthetic_event_id
    assert first.event.semantic_hash == second.event.semantic_hash


def test_candidate_policy_rejects_default_deny_and_unresolvable() -> None:
    config = SyntheticMarketsConfig(enabled=True, mode="shadow")
    now = _dt("2026-06-23T12:00:00+00:00")
    denied = build_candidate_from_payload(
        {
            **_candidate(),
            "canonical_title": "Will a private person's death of illness be announced?",
        },
        config=config,
        generated_at=now,
    )
    unresolvable = build_candidate_from_payload(
        {
            **_candidate(),
            "settlement_rule": {},
        },
        config=config,
        generated_at=now,
    )

    assert denied.accepted is False
    assert denied.rejection is not None
    assert "default_deny_topic" in denied.rejection["reason_codes"]
    assert unresolvable.accepted is False
    assert unresolvable.rejection is not None
    assert "missing_settlement_source" in unresolvable.rejection["reason_codes"]


def test_listing_unknown_without_local_market_inventory(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        result = run_synthetic_markets(
            session,
            candidates=[_candidate()],
            estimate_as_of="2026-06-23T12:00:00+00:00",
            output_path=tmp_path / "synthetic.md",
            json_output_path=tmp_path / "synthetic.json",
            settings=_settings(),
        )
        session.commit()

    assert result.cards == ()
    assert result.rejected_candidates[0]["reason_codes"] == ["listing_status_unknown"]
    assert result.listing_checks[0].status == "LISTING_STATUS_UNKNOWN"
    assert "cannot claim not-listed" in result.markdown


def test_exact_listed_market_rejects_duplicate(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        upsert_market(
            session,
            {
                "ticker": "KXEXACT-YES",
                "event_ticker": "KXEXACT",
                "series_ticker": "KX",
                "title": _candidate()["canonical_title"],
                "status": "open",
            },
        )
        result = run_synthetic_markets(
            session,
            candidates=[_candidate()],
            estimate_as_of="2026-06-23T12:00:00+00:00",
            output_path=tmp_path / "synthetic.md",
            json_output_path=tmp_path / "synthetic.json",
            settings=_settings(),
        )
        session.commit()

    assert result.cards == ()
    assert result.rejected_candidates[0]["reason_codes"] == ["exact_equivalent_listed"]
    assert result.listing_checks[0].status == "EXACT_EQUIVALENT_LISTED"


def test_synthetic_run_persists_cards_memory_and_no_trade_state(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    output = tmp_path / "synthetic.md"
    json_output = tmp_path / "synthetic.json"
    with session_factory() as session:
        upsert_market(
            session,
            {
                "ticker": "KXUNRELATED-YES",
                "event_ticker": "KXUNRELATED",
                "series_ticker": "KX",
                "title": "Will an unrelated public event happen?",
                "status": "open",
            },
        )
        result = run_synthetic_markets(
            session,
            candidates=[_candidate(base_probability="0.64")],
            estimate_as_of="2026-06-23T12:00:00+00:00",
            output_path=output,
            json_output_path=json_output,
            settings=_settings(),
        )
        session.commit()
        run_count = session.scalar(select(func.count()).select_from(SyntheticMarketRun))
        estimate = session.scalar(select(SyntheticProbabilityEstimate))
        market_memory_count = session.scalar(select(func.count()).select_from(MarketMemory))
        forecast_memory_count = session.scalar(select(func.count()).select_from(ForecastMemory))
        trade_memory_count = session.scalar(select(func.count()).select_from(TradeMemory))
        paper_order_count = session.scalar(select(func.count()).select_from(PaperOrder))
        ranking_count = session.scalar(select(func.count()).select_from(MarketRanking))

    assert result.status == "COMPLETED"
    assert result.candidate_counts == {"generated": 1, "accepted": 1, "rejected": 0}
    assert run_count == 1
    assert estimate is not None
    assert estimate.disclaimer == DISCLAIMER
    assert market_memory_count == 1
    assert forecast_memory_count == 1
    assert trade_memory_count == 0
    assert paper_order_count == 0
    assert ranking_count == 0
    assert DISCLAIMER in output.read_text(encoding="utf-8")
    payload = json.loads(json_output.read_text(encoding="utf-8"))
    assert payload["cards"][0]["governance"]["tradable"] is False


def test_synthetic_run_idempotent_retry_publishes_once(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        upsert_market(session, {"ticker": "KXOTHER", "title": "Other market", "status": "open"})
        first = run_synthetic_markets(
            session,
            candidates=[_candidate()],
            estimate_as_of="2026-06-23T12:00:00+00:00",
            output_path=tmp_path / "first.md",
            json_output_path=tmp_path / "first.json",
            settings=_settings(),
        )
        session.commit()
        second = run_synthetic_markets(
            session,
            candidates=[_candidate()],
            estimate_as_of="2026-06-23T12:00:00+00:00",
            output_path=tmp_path / "second.md",
            json_output_path=tmp_path / "second.json",
            settings=_settings(),
        )
        run_count = session.scalar(select(func.count()).select_from(SyntheticMarketRun))

    assert first.idempotent is False
    assert second.idempotent is True
    assert run_count == 1


def test_synthetic_status_report_and_scheduler_smoke(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        status = synthetic_markets_status(session)

    assert status["latest_status"] == "NOT_RUN"
    assert scheduler_plan("synthetic-markets-nightly")[0].command.startswith(
        "kalshi-bot synthetic-markets-run"
    )
    runner = CliRunner()
    for command in (
        "synthetic-markets-status",
        "synthetic-markets-run",
        "synthetic-markets-report",
    ):
        result = runner.invoke(app, [command, "--help"])
        assert result.exit_code == 0
        assert "Usage" in result.output


def test_synthetic_markets_cli_run_writes_json_errors_as_report(tmp_path) -> None:
    get_settings.cache_clear()
    db_url = f"sqlite:///{tmp_path / 'cli.db'}"
    input_file = tmp_path / "candidates.json"
    output = tmp_path / "cli.md"
    json_output = tmp_path / "cli.json"
    input_file.write_text(json.dumps({"candidates": [_candidate()]}), encoding="utf-8")

    result = CliRunner().invoke(
        app,
        [
            "synthetic-markets-run",
            "--enable-research",
            "--mode",
            "shadow",
            "--input-file",
            str(input_file),
            "--output",
            str(output),
            "--json-output",
            str(json_output),
        ],
        env={"DATABASE_URL": db_url},
    )
    get_settings.cache_clear()

    assert result.exit_code == 0
    assert "Safety: internal research only" in result.output
    assert output.exists()
    assert json.loads(json_output.read_text(encoding="utf-8"))["status"] == "COMPLETED"


def test_synthetic_markets_cli_missing_input_file_is_friendly(tmp_path) -> None:
    get_settings.cache_clear()
    db_url = f"sqlite:///{tmp_path / 'cli-missing.db'}"

    result = CliRunner().invoke(
        app,
        [
            "synthetic-markets-run",
            "--enable-research",
            "--mode",
            "shadow",
            "--input-file",
            str(tmp_path / "missing-candidates.json"),
        ],
        env={"DATABASE_URL": db_url},
    )
    get_settings.cache_clear()

    assert result.exit_code == 1
    assert "Phase 3R synthetic markets: BLOCKED" in result.output
    assert "candidate input file not found" in result.output
    assert "Traceback" not in result.output


def test_default_synthetic_candidate_inventory_exists_and_is_valid() -> None:
    payload = json.loads(
        Path("data/synthetic_markets_candidates.json").read_text(encoding="utf-8")
    )

    assert payload["candidates"]
    assert payload["candidates"][0]["settlement_rule"]["primary_source_id"]


def _session_factory(tmp_path):
    engine = init_db(f"sqlite:///{Path(tmp_path) / 'phase3r.db'}")
    return get_session_factory(engine)


def _settings() -> Settings:
    return Settings(
        phase_3r_synthetic_markets_enabled=True,
        phase_3r_mode="shadow",
        phase_3r_probability_floor=Decimal("0.01"),
        phase_3r_probability_ceiling=Decimal("0.99"),
    )


def _candidate(*, base_probability: str | None = None) -> dict:
    candidate = {
        "candidate_id": "weather-kc-heat-index",
        "category": "WEATHER",
        "canonical_title": "Kansas City heat index reaches at least 100 on July 1 2026",
        "plain_language_summary": "Internal forecast for a public weather threshold.",
        "observation_window": {
            "start_at": "2026-07-01T00:00:00+00:00",
            "end_at": "2026-07-01T23:59:59+00:00",
            "timezone": "America/Chicago",
        },
        "settlement_rule": {
            "primary_source_id": "noaa_public_weather",
            "primary_source_locator": "https://www.weather.gov/",
            "source_field": "daily_max_heat_index",
            "rule_text": "Resolve YES if the official public NOAA value is at least 100.",
        },
        "contracts": [
            {
                "canonical_question": (
                    "Will Kansas City heat index reach at least 100 on July 1 2026?"
                ),
                "contract_type": "THRESHOLD",
                "outcome_code": "YES",
                "condition": {
                    "type": "COMPARE",
                    "operator": ">=",
                    "threshold": "100",
                    "unit": "heat_index_f",
                },
            }
        ],
        "feature_snapshot_id": "weather-feature-snapshot",
        "calibration_id": "weather-calibration-v1",
    }
    if base_probability is not None:
        candidate["base_probability"] = base_probability
    return candidate


def _dt(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed
