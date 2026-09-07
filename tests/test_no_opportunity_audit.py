import json
from datetime import timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from kalshi_predictor.cli import app
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.repositories import upsert_market
from kalshi_predictor.data.schema import Forecast, MarketRanking, MarketSnapshot
from kalshi_predictor.no_opportunity_audit import (
    build_ev_attribution,
    write_no_opportunity_root_cause_audit,
)
from kalshi_predictor.utils.time import utc_now


def test_no_opportunity_audit_writes_all_prompt_one_artifacts(tmp_path) -> None:
    database_path = tmp_path / "canonical.db"
    engine = init_db(f"sqlite:///{database_path.as_posix()}")
    now = utc_now()
    with get_session_factory(engine)() as session:
        upsert_market(
            session,
            {
                "ticker": "KXBTC-TEST",
                "event_ticker": "KXBTC-EVENT",
                "series_ticker": "KXBTC",
                "title": "Bitcoin test window",
                "status": "open",
                "close_time": now + timedelta(hours=2),
            },
        )
        session.add(_snapshot("KXBTC-TEST", now))
        session.add(_forecast("KXBTC-TEST", now))
        session.add(_ranking("KXBTC-TEST", now))
        session.commit()
    env_path = tmp_path / ".env"
    env_path.write_text(f"KALSHI_DB_URL=sqlite:///{database_path.as_posix()}\n")
    output_dir = tmp_path / "reports" / "no_opportunity_root_cause"

    artifacts = write_no_opportunity_root_cause_audit(
        database_url=f"sqlite:///{database_path.as_posix()}",
        output_dir=output_dir,
        runtime_worktree=Path.cwd(),
        runtime_reports_dir=tmp_path / "runtime_reports",
        env_path=env_path,
        recent_limit=100,
    )

    required = {
        "00_RUNTIME_FINGERPRINT.json",
        "00_RUNTIME_TRUTH.md",
        "00_DATABASE_BASELINE.json",
        "01_FUNNEL_ROWS.csv",
        "01_FUNNEL_SUMMARY.json",
        "01_FUNNEL_AUDIT.md",
        "01_ROTATION_FAIRNESS.md",
        "01_COVERAGE_BY_SYMBOL.csv",
        "02_EV_ATTRIBUTION.csv",
        "02_EV_MATH_AUDIT.md",
        "02_SCORE_GATE_AUDIT.md",
        "02_PROTOCOL_CONFORMANCE.md",
        "03_MODEL_VS_MARKET.csv",
        "03_FEATURE_HEALTH.csv",
        "03_ABLATION_RESULTS.md",
        "03_MODEL_INDEPENDENCE.md",
        "ROOT_CAUSE_VERDICT.md",
        "ROOT_CAUSE_VERDICT.json",
        "SAFETY_INVARIANTS.md",
        "TEST_RESULTS.md",
        "NEXT_GOAL.md",
        "NEXT_CODEX_PROMPT.md",
    }
    assert required == {path.name for path in output_dir.iterdir()}
    assert artifacts.verdict_json.exists()
    baseline = json.loads((output_dir / "00_DATABASE_BASELINE.json").read_text())
    assert baseline["safety"]["guarded_counts_unchanged"] is True
    assert baseline["safety"]["guarded_count_deltas"]["paper_orders"] == 0
    summary = json.loads((output_dir / "01_FUNNEL_SUMMARY.json").read_text())
    assert summary["observed_active_crypto_markets"] == 1
    assert summary["unknown_rows"] == 0


def test_no_opportunity_audit_refuses_empty_database_as_canonical(tmp_path) -> None:
    database_path = tmp_path / "empty.db"
    init_db(f"sqlite:///{database_path.as_posix()}")
    env_path = tmp_path / ".env"
    env_path.write_text(f"KALSHI_DB_URL=sqlite:///{database_path.as_posix()}\n")

    with pytest.raises(RuntimeError, match="zero markets"):
        write_no_opportunity_root_cause_audit(
            database_url=f"sqlite:///{database_path.as_posix()}",
            output_dir=tmp_path / "reports",
            runtime_worktree=Path.cwd(),
            runtime_reports_dir=tmp_path,
            env_path=env_path,
            recent_limit=100,
        )


def test_ev_attribution_recomputes_yes_and_no_executable_prices() -> None:
    now = utc_now()
    snapshot = _snapshot("YES", now)
    yes = _ranking("YES", now)
    no = _ranking("NO", now, side="BUY_NO", probability="0.40", price="0.59")

    rows = build_ev_attribution({"YES": yes, "NO": no}, {"YES": snapshot, "NO": snapshot})

    yes_row = next(row for row in rows if row["ticker"] == "YES")
    no_row = next(row for row in rows if row["ticker"] == "NO")
    assert yes_row["model_minus_executable_ask"] == "-0.005"
    assert no_row["model_minus_executable_ask"] == "0.01"
    assert yes_row["best_side"] == "BUY_YES"
    assert no_row["best_side"] == "BUY_NO"


def test_no_opportunity_cli_help() -> None:
    result = CliRunner().invoke(app, ["no-opportunity-root-cause-audit", "--help"])
    assert result.exit_code == 0
    assert "query-only" in result.output


def _snapshot(ticker: str, now) -> MarketSnapshot:
    return MarketSnapshot(
        ticker=ticker,
        captured_at=now,
        status="open",
        yes_bid_dollars="0.49",
        yes_ask_dollars="0.50",
        no_bid_dollars="0.50",
        no_ask_dollars="0.51",
        best_yes_bid="0.49",
        best_yes_ask="0.50",
        best_no_bid="0.50",
        best_no_ask="0.51",
        spread="0.01",
        raw_market_json="{}",
        raw_orderbook_json="{}",
    )


def _forecast(ticker: str, now) -> Forecast:
    return Forecast(
        ticker=ticker,
        forecasted_at=now,
        model_name="crypto_v2",
        yes_probability="0.495",
        market_mid_probability="0.495",
        best_yes_bid="0.49",
        best_yes_ask="0.50",
        feature_json='{"external_signal": 0.01, "constant": 0}',
        notes="test",
    )


def _ranking(
    ticker: str,
    now,
    *,
    side: str = "BUY_YES",
    probability: str = "0.495",
    price: str = "0.50",
) -> MarketRanking:
    gross = "-0.005" if side == "BUY_YES" else "0.01"
    return MarketRanking(
        ticker=ticker,
        ranked_at=now,
        title="test",
        status="open",
        series_ticker="KXBTC",
        event_ticker="EVENT",
        volume="100",
        open_interest="100",
        liquidity="100",
        spread="0.01",
        midpoint="0.495",
        time_to_close_minutes="120",
        forecast_model="crypto_v2",
        forecast_probability=probability,
        best_side=side,
        best_price=price,
        estimated_edge=gross,
        liquidity_score="80",
        spread_score="80",
        time_score="80",
        model_confidence_score="50",
        opportunity_score="35",
        reason="test",
        raw_json="{}",
    )
