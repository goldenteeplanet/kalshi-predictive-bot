from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select
from typer.testing import CliRunner

from kalshi_predictor.cli import app
from kalshi_predictor.config import Settings
from kalshi_predictor.crypto.repository import (
    get_latest_crypto_features,
    insert_crypto_market_link,
    insert_crypto_price,
)
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.repositories import (
    encode_json,
    insert_forecast,
    insert_market_snapshot,
    upsert_market,
)
from kalshi_predictor.data.schema import LearningRejectionLog, LearningTradeTarget, PaperOrder
from kalshi_predictor.forecasting.base import ForecastOutput
from kalshi_predictor.learning.targets import generate_learning_targets
from kalshi_predictor.opportunities.repository import (
    insert_market_opportunity,
    insert_market_ranking,
)
from kalshi_predictor.opportunities.scanner import scan_opportunities
from kalshi_predictor.opportunities.window_eligibility import EXPIRED_WINDOW_EXCLUDED
from kalshi_predictor.paper.models import BUY_YES, ORDER_FILLED
from kalshi_predictor.paper.strategy import generate_paper_decisions
from kalshi_predictor.phase3at import (
    CURRENT_PAPER_SCAN,
    build_active_crypto_router,
    build_phase3at_forecast_ranking_diagnostic,
    build_phase3at_opportunity_funnel,
    current_crypto_opportunity_scope,
    warm_crypto_history,
    write_phase3at_handoff_report,
)
from kalshi_predictor.utils.time import utc_now


def test_crypto_history_warmup_inserts_flagged_history_and_ready_feature(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        insert_crypto_price(
            session,
            symbol="BTC",
            source="coinbase",
            observed_at=utc_now(),
            price_usd=Decimal("100000"),
            raw_json={"source": "test"},
        )

        payload = warm_crypto_history(session, symbols=["BTC"], history_minutes=60)
        feature = get_latest_crypto_features(session, "BTC")

    assert payload["summary"]["price_rows_inserted"] > 0
    assert payload["summary"]["symbols_ready_after_warmup"] == 1
    assert feature is not None
    assert feature.source == "history_warmup"


def test_learning_targets_reject_closed_active_universe_market(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed_ranking(session, ticker="KXBTC-CLOSED-TARGET", status="closed")

        result = generate_learning_targets(
            session,
            settings=_learning_settings(),
            model_name="crypto_v2",
            limit=10,
        )
        rejection = session.scalar(select(LearningRejectionLog))

    assert result.inserted == 0
    assert rejection is not None
    assert rejection.reason == "inactive_market"


def test_paper_decisions_reject_closed_active_universe_market(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed_forecast(session, ticker="KXBTC-CLOSED-PAPER", status="closed")

        result = generate_paper_decisions(
            session,
            settings=_learning_settings(),
            model_name="crypto_v2",
        )
        rejection = session.scalar(select(LearningRejectionLog))

    assert result.decisions_generated == 0
    assert result.skipped_due_to_risk_limits == 1
    assert rejection is not None
    assert rejection.reason == "inactive_market"


def test_phase3at_active_router_reports_connected_crypto_funnel(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        ticker = "KXBTC-ACTIVE-ROUTER"
        forecast = _seed_forecast(session, ticker=ticker, status="open")
        _seed_crypto_link(session, ticker)
        _seed_ranking(session, ticker=ticker)
        insert_market_opportunity(
            session,
            {
                "ticker": ticker,
                "model_name": "crypto_v2",
                "side": BUY_YES,
                "price": "0.50",
                "forecast_probability": "0.65",
                "estimated_edge": "0.15",
                "opportunity_score": "80",
                "status": "OPEN",
                "reason": "test",
            },
        )
        session.add(
            LearningTradeTarget(
                generated_at=utc_now(),
                ticker=ticker,
                model_name="crypto_v2",
                category="crypto",
                settlement_speed_score="90",
                learning_priority_score="85",
                reason="test target",
                raw_json="{}",
            )
        )
        session.add(
            PaperOrder(
                ticker=ticker,
                forecast_id=forecast.id,
                created_at=utc_now(),
                model_name="crypto_v2",
                side=BUY_YES,
                probability="0.65",
                market_price="0.50",
                limit_price="0.50",
                edge="0.15",
                quantity=1,
                status=ORDER_FILLED,
                reason="test order",
                raw_decision_json="{}",
            )
        )
        session.flush()

        payload = build_active_crypto_router(
            session,
            settings=Settings(crypto_v2_min_history_minutes=60),
            symbols=["BTC"],
            limit=10,
        )

    assert payload["summary"]["active_crypto_links"] == 1
    assert payload["summary"]["active_crypto_forecasts"] == 1
    assert payload["summary"]["active_opportunities"] == 1
    assert payload["summary"]["learning_candidates"] == 1
    assert payload["summary"]["paper_trades"] == 1
    assert payload["summary"]["main_router_blocker"] == "paper_trade_created"


def test_phase3at_current_scope_excludes_expired_crypto_window(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        ticker = "KXBTC-EXPIRED-ROUTER"
        upsert_market(
            session,
            _market_payload(ticker, status="open", close_delta=timedelta(hours=-1)),
        )
        _seed_crypto_link(session, ticker)

        scope = current_crypto_opportunity_scope(
            session,
            settings=Settings(opportunity_min_time_to_close_minutes=1),
            limit=10,
        )

    assert scope["tickers"] == []
    assert scope["summary"]["expired_crypto_window_links_excluded"] == 1
    assert scope["excluded_rows"][0]["excluded_reason"] == EXPIRED_WINDOW_EXCLUDED


def test_phase3at_current_scope_prioritizes_fresh_snapshot_over_newer_stale_link(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        fresh_ticker = "KXBTC-FRESH-SNAPSHOT"
        stale_ticker = "KXBTC-STALE-SNAPSHOT"
        now = utc_now()
        insert_market_snapshot(
            session,
            _market_payload(fresh_ticker, status="open"),
            {"orderbook_fp": {"yes_dollars": [["0.45", "10"]], "no_dollars": [["0.50", "10"]]}},
            now,
        )
        insert_market_snapshot(
            session,
            _market_payload(stale_ticker, status="open"),
            {"orderbook_fp": {"yes_dollars": [["0.45", "10"]], "no_dollars": [["0.50", "10"]]}},
            now - timedelta(hours=2),
        )
        _seed_crypto_link(session, stale_ticker, detected_at=now)
        _seed_crypto_link(session, fresh_ticker, detected_at=now - timedelta(hours=1))

        scope = current_crypto_opportunity_scope(
            session,
            settings=Settings(opportunity_min_time_to_close_minutes=1),
            limit=1,
        )

    assert scope["tickers"] == [fresh_ticker]
    assert scope["summary"]["current_snapshot_total"] == 1
    assert scope["rows"][0]["snapshot_join_status"] == "CURRENT_SNAPSHOT_JOINED"


def test_phase3at_funnel_uses_precise_snapshot_blocker(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        ticker = "KXBTC-NO-SNAPSHOT"
        upsert_market(session, _market_payload(ticker, status="open"))
        _seed_crypto_link(session, ticker)

        payload = build_phase3at_opportunity_funnel(
            session,
            settings=Settings(opportunity_min_time_to_close_minutes=1),
            limit=10,
        )

    assert payload["summary"]["first_hard_blocker"] == "NO_CURRENT_SNAPSHOT"
    assert payload["stages"][1]["reason_code"] == "NO_CURRENT_SNAPSHOT"


def test_phase3at_forecast_ranking_diagnostic_joins_current_rows(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        ticker = "KXBTC-CURRENT-JOIN"
        _seed_forecast(session, ticker=ticker, status="open")
        _seed_crypto_link(session, ticker)
        _seed_ranking(session, ticker=ticker)

        payload = build_phase3at_forecast_ranking_diagnostic(
            session,
            settings=Settings(opportunity_min_time_to_close_minutes=1),
            limit=10,
        )

    assert payload["summary"]["current_active_crypto_markets"] == 1
    assert payload["summary"]["current_forecasts"] == 1
    assert payload["summary"]["current_rankings"] == 1
    assert payload["current_rows"][0]["first_hard_blocker"] == "CURRENT_FORECAST_RANKED"


def test_phase3at_diagnostic_classifies_current_forecast_missing_ranking(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        ticker = "KXBTC-MISSING-CURRENT-RANKING"
        _seed_forecast(session, ticker=ticker, status="open")
        _seed_crypto_link(session, ticker)

        payload = build_phase3at_forecast_ranking_diagnostic(
            session,
            settings=Settings(opportunity_min_time_to_close_minutes=1),
            limit=10,
        )

    assert payload["summary"]["current_forecasts"] == 1
    assert payload["summary"]["current_rankings"] == 0
    assert payload["current_rows"][0]["first_hard_blocker"] == (
        "CURRENT_FORECAST_MISSING_RANKING"
    )
    assert payload["blocked_forecast_rows"][0]["ticker"] == ticker


def test_phase3at_diagnostic_classifies_ticker_normalization_mismatch(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        ticker = "kxbtc-ticker-mismatch"
        _seed_forecast(session, ticker=ticker, status="open")
        _seed_crypto_link(session, ticker)

        payload = build_phase3at_forecast_ranking_diagnostic(
            session,
            settings=Settings(opportunity_min_time_to_close_minutes=1),
            limit=10,
        )

    assert payload["current_rows"][0]["first_hard_blocker"] == "TICKER_MISMATCH"
    assert payload["summary"]["ticker_normalization_mismatches"] == 1


def test_phase3at_diagnostic_classifies_model_and_timestamp_mismatch(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        model_ticker = "KXBTC-MODEL-MISMATCH"
        time_ticker = "KXBTC-TIME-MISMATCH"
        _seed_forecast(session, ticker=model_ticker, status="open", model_name="other_model")
        _seed_crypto_link(session, model_ticker)
        forecast = _seed_forecast(session, ticker=time_ticker, status="open")
        _seed_crypto_link(session, time_ticker)
        _seed_ranking(
            session,
            ticker=time_ticker,
            ranked_at=forecast.forecasted_at - timedelta(minutes=5),
        )

        payload = build_phase3at_forecast_ranking_diagnostic(
            session,
            settings=Settings(opportunity_min_time_to_close_minutes=1),
            limit=10,
        )
        rows = {row["ticker"]: row for row in payload["current_rows"]}

    assert rows[model_ticker]["first_hard_blocker"] == "MODEL_NAME_MISMATCH"
    assert rows[time_ticker]["first_hard_blocker"] == "TIMESTAMP_WINDOW_MISMATCH"


def test_current_scan_scope_excludes_historical_forecasts(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        current = "KXBTC-CURRENT-SCAN"
        historical = "KXBTC-HISTORICAL-SCAN"
        _seed_forecast(session, ticker=current, status="open")
        _seed_forecast(session, ticker=historical, status="open")

        summary = scan_opportunities(
            session,
            model_name="crypto_v2",
            ticker_scope={current},
            scan_mode=CURRENT_PAPER_SCAN,
            settings=Settings(opportunity_min_time_to_close_minutes=1),
            limit=10,
        )

    assert summary.scan_mode == CURRENT_PAPER_SCAN
    assert summary.markets_scanned == 1
    assert summary.current_ticker_scope_count == 1
    assert summary.historical_rows_excluded == 1
    assert all(row["ticker"] == current for row in summary.rankings)


def test_phase3at_handoff_report_generates_all_artifacts(tmp_path) -> None:
    output_dir = Path(tmp_path) / "phase3at"
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        ticker = "KXBTC-HANDOFF-REPORT"
        _seed_forecast(session, ticker=ticker, status="open")
        _seed_crypto_link(session, ticker)

        artifacts = write_phase3at_handoff_report(
            session,
            output_dir=output_dir,
            reports_dir=Path(tmp_path) / "reports",
            settings=Settings(opportunity_min_time_to_close_minutes=1),
            limit=10,
        )

    expected = {
        "EXECUTIVE_SUMMARY.md",
        "NEXT_ACTIONS.md",
        "forecast_ranking_diagnostic.json",
        "forecast_ranking_diagnostic.md",
        "opportunity_funnel.json",
        "opportunity_funnel.md",
        "current_snapshot_join_diagnostic.json",
        "current_vs_historical_rankings.csv",
        "blocked_forecast_rows.csv",
        "blocked_ranking_rows.csv",
        "performance_diagnostic.json",
        "MANIFEST.sha256",
    }
    assert artifacts.json_path.exists()
    assert expected.issubset({path.name for path in output_dir.iterdir()})
    snapshot_join = output_dir / "current_snapshot_join_diagnostic.json"
    assert "\"db_fingerprint\"" in snapshot_join.read_text(encoding="utf-8")


def test_phase3at_handoff_reconciles_with_r5_ev_not_positive_truth(tmp_path) -> None:
    output_dir = Path(tmp_path) / "phase3at"
    reports_dir = Path(tmp_path) / "reports"
    _write_r5_ev_not_positive_status(reports_dir)
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        ticker = "KXBTC-R5-HANDOFF-ALIGN"
        _seed_forecast(session, ticker=ticker, status="open")
        _seed_crypto_link(session, ticker)

        artifacts = write_phase3at_handoff_report(
            session,
            output_dir=output_dir,
            reports_dir=reports_dir,
            settings=Settings(opportunity_min_time_to_close_minutes=1),
            limit=10,
        )

    diagnostic = json.loads(
        (output_dir / "forecast_ranking_diagnostic.json").read_text(encoding="utf-8")
    )
    funnel = json.loads((output_dir / "opportunity_funnel.json").read_text(encoding="utf-8"))
    next_actions = (output_dir / "NEXT_ACTIONS.md").read_text(encoding="utf-8")

    assert diagnostic["summary"]["raw_first_hard_blocker"] == (
        "CURRENT_FORECAST_MISSING_RANKING"
    )
    assert diagnostic["summary"]["first_hard_blocker"] == "EV_NOT_POSITIVE"
    assert funnel["summary"]["raw_first_hard_blocker"] == (
        "CURRENT_FORECAST_MISSING_RANKING"
    )
    assert funnel["summary"]["first_hard_blocker"] == "EV_NOT_POSITIVE"
    assert funnel["next_action"] == "kalshi-bot phase3bc-r5-status --output-dir reports/phase3bc_r5"
    assert "kalshi-bot phase3bc-r5-status --output-dir reports/phase3bc_r5" in next_actions
    assert artifacts.json_path.exists()


def test_phase3at_cli_help_smoke() -> None:
    runner = CliRunner()
    for command in (
        "crypto-history-warmup",
        "phase3at-active-router",
        "active-crypto-router",
        "phase3at-forecast-ranking-diagnostic",
        "phase3at-opportunity-funnel",
        "phase3at-handoff-report",
    ):
        result = runner.invoke(app, [command, "--help"])
        assert result.exit_code == 0
        assert "3AT" in result.output or "crypto" in result.output.lower()


def _session_factory(tmp_path):
    engine = init_db(f"sqlite:///{Path(tmp_path) / 'phase3at.db'}")
    return get_session_factory(engine)


def _write_r5_ev_not_positive_status(reports_dir: Path) -> None:
    r5_dir = reports_dir / "phase3bc_r5"
    r5_dir.mkdir(parents=True, exist_ok=True)
    now = utc_now().isoformat()
    payload = {
        "generated_at": now,
        "latest_report_generated_at": now,
        "latest_summary": {
            "watch_state": "WAITING_FOR_POSITIVE_EV",
            "snapshot_stale_rows": 0,
            "forecast_stale_rows": 0,
            "ranking_missing_rows": 0,
            "ranking_stale_rows": 0,
            "ranking_before_forecast_rows": 0,
            "true_ranking_gap_after_repair": 0,
            "ranking_coverage_gap_after_repair": 0,
            "primary_gap_after_refresh": "EV_NOT_POSITIVE",
            "positive_ev_rows": 0,
            "clean_execution_rows": 4,
            "paper_ready_candidates": 0,
        },
    }
    (r5_dir / "phase3bc_r5_status.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )


def _learning_settings() -> Settings:
    return Settings(
        learning_mode=True,
        learning_min_edge=Decimal("0.01"),
        learning_min_opportunity_score=Decimal("35"),
        learning_candidate_scan_limit=50,
        learning_target_trades_per_cycle=10,
        learning_min_trades_per_cycle=5,
        paper_min_edge=Decimal("0.01"),
        paper_max_order_quantity=1,
        paper_max_position_per_market=5,
        paper_max_open_orders=100,
        paper_allow_buy_no=True,
    )


def _market_payload(
    ticker: str,
    *,
    status: str,
    close_delta: timedelta = timedelta(hours=4),
) -> dict[str, str]:
    now = utc_now()
    return {
        "ticker": ticker,
        "status": status,
        "title": "Will Bitcoin price be above 100000?",
        "series_ticker": "KXBTC",
        "event_ticker": f"{ticker}-EVENT",
        "yes_bid_dollars": "0.45",
        "yes_ask_dollars": "0.50",
        "last_price_dollars": "0.48",
        "liquidity_dollars": "1000",
        "close_time": (now + close_delta).isoformat(),
        "expected_expiration_time": (now + close_delta + timedelta(minutes=5)).isoformat(),
        "rules_primary": "Pays according to the BTC reference price.",
    }


def _seed_forecast(
    session,
    *,
    ticker: str,
    status: str,
    model_name: str = "crypto_v2",
):
    now = utc_now()
    insert_market_snapshot(
        session,
        _market_payload(ticker, status=status),
        {"orderbook_fp": {"yes_dollars": [["0.45", "10"]], "no_dollars": [["0.50", "10"]]}},
        now,
    )
    forecast = insert_forecast(
        session,
        ForecastOutput(
            ticker=ticker,
            forecasted_at=now,
            model_name=model_name,
            yes_probability=Decimal("0.70"),
            market_mid_probability=Decimal("0.50"),
            best_yes_bid=Decimal("0.45"),
            best_yes_ask=Decimal("0.50"),
            feature_json={"source": "test"},
        ),
    )
    session.flush()
    return forecast


def _seed_ranking(
    session,
    *,
    ticker: str,
    status: str = "open",
    ranked_at=None,
) -> None:
    upsert_market(session, _market_payload(ticker, status=status))
    insert_market_ranking(
        session,
        {
            "ticker": ticker,
            "ranked_at": ranked_at or utc_now(),
            "title": "Will Bitcoin price be above 100000?",
            "status": status,
            "series_ticker": "KXBTC",
            "event_ticker": f"{ticker}-EVENT",
            "forecast_model": "crypto_v2",
            "forecast_probability": "0.70",
            "best_side": BUY_YES,
            "best_price": "0.50",
            "midpoint": "0.50",
            "estimated_edge": "0.20",
            "liquidity": "100",
            "liquidity_score": "80",
            "spread": "0.05",
            "spread_score": "90",
            "time_to_close_minutes": "240",
            "time_score": "90",
            "model_confidence_score": "70",
            "opportunity_score": "80",
            "reason": "test ranking",
        },
    )


def _seed_crypto_link(session, ticker: str, detected_at=None) -> None:
    insert_crypto_market_link(
        session,
        ticker=ticker,
        symbol="BTC",
        confidence="1.0",
        reason="test exact link",
        raw_json={"structured_terms": _btc_terms(ticker)},
        detected_at=detected_at,
    )
    session.flush()


def _btc_terms(ticker: str) -> dict[str, object]:
    return {
        "ticker": ticker,
        "status": "EXACT_LINK",
        "symbol": "BTC",
        "component_symbols": ["BTC"],
        "components": [
            {
                "symbol": "BTC",
                "side": "YES",
                "comparator": "ABOVE",
                "threshold_value": "100000",
                "reference_price_source": "unknown_public_reference",
            }
        ],
        "reason_codes": ["test_terms"],
        "reference_price_source": "unknown_public_reference",
        "settlement_timezone": "UTC",
        "idempotency_key": f"{ticker}:btc:above:100000",
        "raw_json": encode_json({"source": "test"}),
    }
