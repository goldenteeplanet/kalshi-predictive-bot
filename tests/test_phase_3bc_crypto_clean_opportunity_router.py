from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from typer.testing import CliRunner

from kalshi_predictor.cli import app
from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.crypto.repository import insert_crypto_market_link
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.repositories import encode_json, insert_forecast, insert_market_snapshot
from kalshi_predictor.data.schema import MarketLeg
from kalshi_predictor.forecasting.base import ForecastOutput
from kalshi_predictor.opportunities.repository import insert_market_ranking
from kalshi_predictor.paper.models import BUY_YES
from kalshi_predictor.phase3bc import build_phase3bc_crypto_clean_opportunity_router
from kalshi_predictor.utils.time import utc_now


def test_phase3bc_marks_pure_crypto_paper_ready(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed_crypto_candidate(session, ticker="KXBTC-PURE-READY")

        payload = build_phase3bc_crypto_clean_opportunity_router(
            session,
            settings=_settings(),
            limit=10,
        )

    assert payload["summary"]["paper_ready_candidates"] == 1
    assert payload["summary"]["strict_turn_on_candidates"] == 1
    assert payload["summary"]["active_pure_crypto_markets"] == 1
    row = payload["paper_ready_rows"][0]
    assert row["structure_status"] == "PURE_CRYPTO"
    assert row["readiness_status"] == "PAPER_READY_CANDIDATE"


def test_phase3bc_blocks_mixed_crypto_sports_bundle(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed_crypto_candidate(
            session,
            ticker="KXBTC-MIXED-BLOCKED",
            title="yes Target Price: $100,000,yes Over 5.5 runs scored",
            include_sports_leg=True,
        )

        payload = build_phase3bc_crypto_clean_opportunity_router(
            session,
            settings=_settings(),
            limit=10,
        )

    assert payload["summary"]["paper_ready_candidates"] == 0
    assert payload["summary"]["mixed_or_cross_category_markets"] == 1
    row = payload["blocked_examples"][0]
    assert row["structure_status"] == "MIXED_CATEGORY"
    assert row["readiness_status"] == "BLOCKED_MIXED_CATEGORY"
    assert "sports" in row["non_crypto_leg_categories"]


def test_phase3bc_blocks_mixed_title_when_stored_legs_are_missing(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed_crypto_candidate(
            session,
            ticker="KXBTC-MIXED-FALLBACK-BLOCKED",
            title="yes Target Price: $100,000,yes Over 5.5 runs scored",
            store_legs=False,
        )

        payload = build_phase3bc_crypto_clean_opportunity_router(
            session,
            settings=_settings(),
            limit=10,
        )

    assert payload["summary"]["paper_ready_candidates"] == 0
    row = payload["blocked_examples"][0]
    assert row["structure_status"] == "MIXED_CATEGORY"
    assert row["readiness_status"] == "BLOCKED_MIXED_CATEGORY"
    assert "sports" in row["non_crypto_leg_categories"]


def test_phase3bc_blocks_zero_liquidity_crypto_row(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed_crypto_candidate(
            session,
            ticker="KXBTC-NO-LIQUIDITY",
            liquidity="0",
            liquidity_score="0",
        )

        payload = build_phase3bc_crypto_clean_opportunity_router(
            session,
            settings=_settings(),
            limit=10,
        )

    assert payload["summary"]["paper_ready_candidates"] == 0
    row = payload["blocked_examples"][0]
    assert row["readiness_status"] == "BLOCKED_NO_LIQUIDITY"
    assert "Liquidity score is too low" in row["blockers"][0]


def test_phase3bc_blocks_missing_executable_orderbook_depth(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed_crypto_candidate(
            session,
            ticker="KXBTC-NO-EXECUTABLE-DEPTH",
            no_depth="0",
        )

        payload = build_phase3bc_crypto_clean_opportunity_router(
            session,
            settings=_settings(),
            limit=10,
        )

    assert payload["summary"]["paper_ready_candidates"] == 0
    row = payload["blocked_examples"][0]
    assert row["readiness_status"] == "BLOCKED_NO_EXECUTABLE_BOOK"
    assert row["book_state"] == "NO_EXECUTABLE_BOOK"
    assert row["book_usable"] is False
    assert "bid/ask book" in row["blockers"][0]


def test_phase3bc_cli_smoke(tmp_path) -> None:
    get_settings.cache_clear()
    runner = CliRunner()
    db_url = f"sqlite:///{Path(tmp_path) / 'phase3bc_cli.db'}"
    output_dir = Path(tmp_path) / "phase3bc"

    result = runner.invoke(
        app,
        [
            "phase3bc-crypto-clean-opportunity-router",
            "--output-dir",
            str(output_dir),
            "--limit",
            "10",
        ],
        env={"KALSHI_DB_URL": db_url},
    )

    get_settings.cache_clear()
    assert result.exit_code == 0
    assert (output_dir / "phase3bc_crypto_clean_opportunity_router.json").exists()
    assert "PAPER ONLY" in result.output


def _seed_crypto_candidate(
    session,
    *,
    ticker: str,
    title: str = "yes Target Price: $100,000",
    include_sports_leg: bool = False,
    store_legs: bool = True,
    liquidity: str = "1000",
    liquidity_score: str = "80",
    yes_depth: str = "10",
    no_depth: str = "10",
) -> None:
    now = utc_now()
    insert_market_snapshot(
        session,
        {
            "ticker": ticker,
            "status": "open",
            "title": title,
            "series_ticker": "KXBTC",
            "event_ticker": f"{ticker}-EVENT",
            "yes_bid_dollars": "0.38",
            "yes_ask_dollars": "0.40",
            "no_bid_dollars": "0.59",
            "no_ask_dollars": "0.62",
            "volume_fp": "500",
            "open_interest_fp": "250",
            "liquidity_dollars": liquidity,
            "close_time": (now + timedelta(hours=6)).isoformat(),
        },
        {
            "orderbook_fp": {
                "yes_dollars": [["0.38", yes_depth]],
                "no_dollars": [["0.59", no_depth]],
            }
        },
        now,
    )
    insert_crypto_market_link(
        session,
        ticker=ticker,
        symbol="BTC",
        confidence="1.0",
        reason="test exact crypto link",
        raw_json={"structured_terms": _btc_terms(ticker)},
    )
    if store_legs:
        session.add(
            MarketLeg(
                ticker=ticker,
                leg_index=0,
                parsed_at=now,
                side="YES",
                category="crypto",
                market_type="target_price",
                entity_name="BTC",
                operator="ABOVE",
                threshold_value="100000",
                unit="USD",
                confidence="0.95",
                raw_text="yes Target Price: $100,000",
                reason="test crypto leg",
                raw_json=encode_json({"source": "test"}),
            )
        )
    if include_sports_leg and store_legs:
        session.add(
            MarketLeg(
                ticker=ticker,
                leg_index=1,
                parsed_at=now,
                side="YES",
                category="sports",
                market_type="team_total",
                entity_name="Over 5.5 runs scored",
                operator="ABOVE",
                threshold_value="5.5",
                unit="runs",
                confidence="0.90",
                raw_text="yes Over 5.5 runs scored",
                reason="test sports leg",
                raw_json=encode_json({"source": "test"}),
            )
        )
    forecast = insert_forecast(
        session,
        ForecastOutput(
            ticker=ticker,
            forecasted_at=now,
            model_name="crypto_v2",
            yes_probability=Decimal("0.75"),
            market_mid_probability=Decimal("0.39"),
            best_yes_bid=Decimal("0.38"),
            best_yes_ask=Decimal("0.40"),
            feature_json={"source": "test"},
        ),
    )
    insert_market_ranking(
        session,
        {
            "ticker": ticker,
            "ranked_at": now,
            "title": title,
            "status": "open",
            "series_ticker": "KXBTC",
            "event_ticker": f"{ticker}-EVENT",
            "forecast_model": "crypto_v2",
            "forecast_probability": "0.75",
            "best_side": BUY_YES,
            "best_price": "0.40",
            "midpoint": "0.39",
            "estimated_edge": "0.35",
            "liquidity": liquidity,
            "liquidity_score": liquidity_score,
            "spread": "0.02",
            "spread_score": "90",
            "time_to_close_minutes": "360",
            "time_score": "90",
            "model_confidence_score": "80",
            "opportunity_score": "85",
            "reason": "test crypto opportunity",
            "raw_json": {"forecast_id": forecast.id, "snapshot_id": 1},
        },
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
                "source_market": ticker,
            }
        ],
        "reason_codes": ["test_terms"],
        "reference_price_source": "unknown_public_reference",
        "settlement_timezone": "UTC",
        "series_ticker": "KXBTC",
        "event_ticker": f"{ticker}-EVENT",
        "market_type": "binary",
        "idempotency_key": f"{ticker}:btc:above:100000",
    }


def _settings() -> Settings:
    return Settings(
        learning_mode=False,
        opportunity_min_edge=Decimal("0.03"),
        opportunity_min_score=Decimal("60"),
        opportunity_max_spread=Decimal("0.10"),
        opportunity_min_liquidity=Decimal("0"),
        crypto_v2_min_link_confidence=Decimal("0.6"),
    )


def _session_factory(tmp_path):
    engine = init_db(f"sqlite:///{Path(tmp_path) / 'phase3bc.db'}")
    return get_session_factory(engine)
