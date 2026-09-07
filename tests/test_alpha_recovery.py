import csv
import json
from datetime import timedelta

from kalshi_predictor.alpha_recovery import (
    build_shadow_ledger,
    paper_readiness,
    simulate_selector,
    write_alpha_recovery_reports,
)
from kalshi_predictor.cli import app
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.repositories import upsert_market, upsert_settlement
from kalshi_predictor.data.schema import Forecast, MarketRanking
from kalshi_predictor.utils.time import utc_now
from typer.testing import CliRunner


def test_shadow_ledger_requires_preceding_forecast_and_uses_exact_settlement() -> None:
    now = utc_now()
    forecast = _forecast("KXBTC-SETTLED", now - timedelta(minutes=2))
    ranking = _ranking("KXBTC-SETTLED", now)
    settlement = type("SettlementStub", (), {"result": "yes", "settled_at": now})()

    rows = build_shadow_ledger(
        {
            "rankings": [ranking],
            "settlements": {ranking.ticker: settlement},
            "forecasts_by_ticker": {ranking.ticker: [forecast]},
        }
    )

    assert rows[0]["lineage_valid"] is True
    assert rows[0]["no_lookahead"] is True
    assert rows[0]["settlement"] == "yes"
    assert rows[0]["guarded_table_write"] is False


def test_shadow_ledger_rejects_future_forecast_lineage() -> None:
    now = utc_now()
    ranking = _ranking("KXBTC-FUTURE", now)
    future = _forecast("KXBTC-FUTURE", now + timedelta(minutes=1))

    row = build_shadow_ledger(
        {
            "rankings": [ranking],
            "settlements": {},
            "forecasts_by_ticker": {ranking.ticker: [future]},
        }
    )[0]

    assert row["forecast_id"] is None
    assert row["lineage_valid"] is False
    assert row["no_lookahead"] is False


def test_fair_selector_rotates_across_symbols() -> None:
    rows = [
        {"ticker": f"BTC-{index}", "series_ticker": "KXBTC", "volume": "100"} for index in range(5)
    ]
    rows.extend(
        {"ticker": f"ETH-{index}", "series_ticker": "KXETH", "volume": "10"} for index in range(5)
    )

    selected = simulate_selector(rows, limit=4, method="fair")

    assert [row["series_ticker"] for row in selected] == ["KXBTC", "KXETH"] * 2


def test_activation_approval_does_not_bypass_readiness_gates() -> None:
    readiness = paper_readiness(
        [],
        {"guarded_counts_unchanged": True},
        activation_approved=True,
    )

    assert readiness["explicit_activation_token_received"] is True
    assert readiness["ready"] is False
    assert readiness["paper_order_creation_enabled"] is False


def test_alpha_recovery_writer_emits_required_reports_without_guarded_writes(tmp_path) -> None:
    database_path = tmp_path / "canonical.db"
    engine = init_db(f"sqlite:///{database_path.as_posix()}")
    now = utc_now()
    with get_session_factory(engine)() as session:
        upsert_market(
            session,
            {
                "ticker": "KXBTC-SETTLED",
                "status": "settled",
                "title": "settled test",
            },
        )
        forecast = _forecast("KXBTC-SETTLED", now - timedelta(minutes=2))
        session.add(forecast)
        session.flush()
        session.add(_ranking("KXBTC-SETTLED", now))
        upsert_settlement(
            session,
            {
                "ticker": "KXBTC-SETTLED",
                "result": "yes",
                "settlement_ts": now.isoformat(),
            },
        )
        session.commit()
    prompt1 = tmp_path / "prompt1"
    prompt1.mkdir()
    (prompt1 / "ROOT_CAUSE_VERDICT.json").write_text("{}")
    (prompt1 / "02_EV_MATH_AUDIT.md").write_text("ok")
    (prompt1 / "03_MODEL_INDEPENDENCE.md").write_text("ok")
    with (prompt1 / "01_FUNNEL_ROWS.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "ticker",
                "series_ticker",
                "current_window_eligible",
                "snapshot_timestamp",
                "volume",
            ],
        )
        writer.writeheader()
        writer.writerow(
            {
                "ticker": "KXBTC-OPEN",
                "series_ticker": "KXBTC",
                "current_window_eligible": True,
                "snapshot_timestamp": "",
                "volume": "100",
            }
        )

    output = tmp_path / "alpha_recovery"
    write_alpha_recovery_reports(
        database_url=f"sqlite:///{database_path.as_posix()}",
        prompt1_dir=prompt1,
        output_dir=output,
        ranking_limit=100,
        replay_limit=100,
        activation_approved=True,
    )

    required = {
        "SETTLEMENT_LINEAGE.md",
        "SHADOW_LEDGER_STATUS.md",
        "NEAR_MISS_RESULTS.md",
        "THRESHOLD_SENSITIVITY.md",
        "COVERAGE_EXPERIMENT.md",
        "ALPHA_REMEDIATION.md",
        "DOMAIN_SELECTION.md",
        "PAPER_READINESS.md",
        "SAFETY_INVARIANTS.md",
        "TEST_RESULTS.md",
        "NEXT_GOAL.md",
        "NEXT_CODEX_PROMPT.md",
    }
    assert required <= {path.name for path in output.iterdir()}
    readiness = (output / "PAPER_READINESS.md").read_text()
    assert "Explicit activation token received: `True`" in readiness
    assert "Paper-order creation enabled: `False`" in readiness
    assert (
        json.loads((output / "SHADOW_LEDGER.json").read_text())[0]["guarded_table_write"] is False
    )


def test_alpha_recovery_cli_help() -> None:
    result = CliRunner().invoke(app, ["alpha-recovery-audit", "--help"])
    assert result.exit_code == 0
    assert "shadow" in result.output.lower()


def _forecast(ticker: str, forecasted_at) -> Forecast:
    row = Forecast(
        ticker=ticker,
        forecasted_at=forecasted_at,
        model_name="crypto_v2",
        yes_probability="0.55",
        market_mid_probability="0.54",
        best_yes_bid="0.53",
        best_yes_ask="0.56",
        feature_json='{"external_signal": 0.01}',
        notes="test",
    )
    row.id = 1
    return row


def _ranking(ticker: str, ranked_at) -> MarketRanking:
    return MarketRanking(
        ticker=ticker,
        ranked_at=ranked_at,
        title="test",
        status="open",
        series_ticker="KXBTC",
        event_ticker="EVENT",
        volume="100",
        open_interest="100",
        liquidity="100",
        spread="0.02",
        midpoint="0.54",
        time_to_close_minutes="120",
        forecast_model="crypto_v2",
        forecast_probability="0.55",
        best_side="BUY_YES",
        best_price="0.56",
        estimated_edge="-0.01",
        liquidity_score="80",
        spread_score="80",
        time_score="80",
        model_confidence_score="50",
        opportunity_score="35",
        reason="test",
        raw_json="{}",
    )
