from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import func, select
from typer.testing import CliRunner

from kalshi_predictor.cli import app
from kalshi_predictor.config import Settings
from kalshi_predictor.crypto.linker import detect_crypto_market, link_crypto_markets
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.repositories import upsert_market, upsert_settlement
from kalshi_predictor.data.schema import (
    CryptoMarketLink,
    MarketLeg,
    PaperOrder,
    SportsFeature,
    SportsGame,
    SportsMarketLink,
    SportsTeam,
)
from kalshi_predictor.learning.cycle import LearningCycleResult
from kalshi_predictor.market_legs import parse_and_store_market_legs
from kalshi_predictor.paper.models import ORDER_FILLED
from kalshi_predictor.paper.settlement_reconciliation import (
    build_paper_settlement_reconciliation,
    write_paper_settlement_reconciliation,
)
from kalshi_predictor.phase3y import (
    SettlementWatchJobs,
    generate_phase3y_report,
    run_settlement_watcher,
)
from kalshi_predictor.sports.derived_schedule import derive_sports_schedule_from_market_legs
from kalshi_predictor.sports.linker import link_sports_markets
from kalshi_predictor.utils.time import utc_now
from kalshi_predictor.weather.linker import detect_weather_market


def test_crypto_linker_uses_ticker_prefix_and_raw_payload(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        market = upsert_market(
            session,
            {
                "ticker": "KXBTC-PRICE-TEST",
                "title": "Will the contract close above the stated threshold?",
                "raw_json": {"series_title": "Bitcoin price daily market"},
            },
        )

        symbol, confidence, reason = detect_crypto_market(market)

    assert symbol == "BTC"
    assert confidence == Decimal("1.0")
    assert "BTC" in reason


def test_crypto_linker_maps_supported_target_price_ranges(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        btc_market = upsert_market(
            session,
            {
                "ticker": "KXMVECROSSCATEGORY-HIGH",
                "title": "yes Target Price: $62,382.15",
                "series_ticker": "KXMVECROSSCATEGORY",
            },
        )
        eth_market = upsert_market(
            session,
            {
                "ticker": "KXMVECROSSCATEGORY-MID",
                "title": "yes Target Price: $1,668.64",
                "series_ticker": "KXMVECROSSCATEGORY",
            },
        )

        btc_symbol, btc_confidence, btc_reason = detect_crypto_market(btc_market)
        eth_symbol, eth_confidence, eth_reason = detect_crypto_market(eth_market)

    assert btc_symbol == "BTC"
    assert btc_confidence == Decimal("0.75")
    assert "target price" in btc_reason.lower()
    assert eth_symbol == "ETH"
    assert eth_confidence == Decimal("0.75")
    assert "target price" in eth_reason.lower()


def test_crypto_linker_maps_supported_low_price_target_assets(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        cases = [
            ("DOGE", "$0.075"),
            ("XRP", "$1.06"),
            ("SOL", "$150"),
        ]
        markets = [
            upsert_market(
                session,
                {
                    "ticker": f"KXMVECROSSCATEGORY-{symbol}",
                    "title": f"yes Target Price: {target}",
                    "series_ticker": "KXMVECROSSCATEGORY",
                },
            )
            for symbol, target in cases
        ]

        detected = [detect_crypto_market(market) for market in markets]

    assert [symbol for symbol, _confidence, _reason in detected] == ["DOGE", "XRP", "SOL"]
    assert all("target price" in reason.lower() for _symbol, _confidence, reason in detected)


def test_crypto_linker_maps_multi_asset_cross_category_target_prices(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        market = upsert_market(
            session,
            {
                "ticker": "KXMVECROSSCATEGORY-MIXED",
                "title": "yes Target Price: $62,000,no Target Price: $1,660",
                "series_ticker": "KXMVECROSSCATEGORY",
            },
        )

        symbol, confidence, reason = detect_crypto_market(market)

    assert symbol == "BTC+ETH"
    assert confidence == Decimal("0.70")
    assert "multi-asset" in reason.lower()


def test_crypto_linker_rejects_unsupported_target_price_assets(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        market = upsert_market(
            session,
            {
                "ticker": "KXMVECROSSCATEGORY-UNSUPPORTED",
                "title": "yes Target Price: $62,000,no Target Price: $0.0000001",
                "series_ticker": "KXMVECROSSCATEGORY",
            },
        )

        symbol, confidence, reason = detect_crypto_market(market)

    assert symbol is None
    assert confidence == Decimal("0.0")
    assert "unsupported" in reason.lower()


def test_crypto_linker_trusts_explicit_doge_series_below_heuristic_price_floor(
    tmp_path,
) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        market = upsert_market(
            session,
            {
                "ticker": "KXDOGE-26JUL2417-B0.007",
                "event_ticker": "KXDOGE-26JUL2417",
                "series_ticker": "KXDOGE",
                "title": "Dogecoin price range on Jul 24, 2026?",
            },
        )
        session.add(
            MarketLeg(
                ticker=market.ticker,
                leg_index=0,
                parsed_at=utc_now(),
                side="YES",
                category="crypto",
                market_type="TARGET_PRICE",
                entity_name="DOGE",
                operator="ABOVE",
                threshold_value="0.007",
                unit="USD",
                confidence="0.95",
                raw_text="Dogecoin above $0.007",
                reason="test DOGE leg",
                raw_json="{}",
            )
        )

        result = link_crypto_markets(session, tickers=[market.ticker])
        link = session.scalar(
            select(CryptoMarketLink).where(CryptoMarketLink.ticker == market.ticker)
        )

    assert result.links_created == 1
    assert result.exact_semantic_links == 1
    assert link is not None
    assert link.symbol == "DOGE"
    assert "explicit_event_symbol_target_price" in link.raw_json


def test_crypto_link_remediation_is_idempotent_for_target_links(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        upsert_market(
            session,
            {
                "ticker": "KXMVECROSSCATEGORY-IDEMPOTENT",
                "title": "yes Target Price: $62,382.15",
                "series_ticker": "KXMVECROSSCATEGORY",
            },
        )

        first = link_crypto_markets(session)
        second = link_crypto_markets(session)
        links = session.scalar(select(func.count(CryptoMarketLink.id)))

    assert first.links_created == 1
    assert first.target_price_links == 1
    assert second.links_created == 0
    assert second.already_linked == 1
    assert links == 1


def test_crypto_linker_can_scope_to_near_money_snapshot_tickers(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        upsert_market(
            session,
            {
                "ticker": "KXBTC-SCOPED",
                "title": "Bitcoin Price Market",
                "series_ticker": "KXBTC",
            },
        )
        upsert_market(
            session,
            {
                "ticker": "KXETH-UNSCOPED",
                "title": "Ethereum Price Market",
                "series_ticker": "KXETH",
            },
        )

        result = link_crypto_markets(session, tickers=["KXBTC-SCOPED"])
        links = session.scalars(select(CryptoMarketLink)).all()

    assert result.markets_scanned == 1
    assert result.links_created == 1
    assert [link.ticker for link in links] == ["KXBTC-SCOPED"]


def test_crypto_linker_uses_cross_category_associated_events(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        upsert_market(
            session,
            {
                "ticker": "KXMVECROSSCATEGORY-DOGE-ETH-XRP",
                "title": (
                    "yes Target Price: $0.075,"
                    "yes Target Price: $1,625,"
                    "yes Target Price: $1.06"
                ),
                "raw_json": {
                    "custom_strike": {
                        "Associated Events": (
                            "KXDOGE15M-26JUN241230,"
                            "KXETH15M-26JUN241230,"
                            "KXXRP15M-26JUN241230"
                        ),
                        "Associated Market Sides": "yes,yes,yes",
                        "Associated Markets": (
                            "KXDOGE15M-26JUN241230-30,"
                            "KXETH15M-26JUN241230-30,"
                            "KXXRP15M-26JUN241230-30"
                        ),
                    }
                },
            },
        )

        result = link_crypto_markets(session)
        link = session.scalar(select(CryptoMarketLink))

    assert result.links_created == 1
    assert result.multi_asset_links == 1
    assert link.symbol == "DOGE+ETH+XRP"
    assert "DOGE" in link.raw_json
    assert "XRP" in link.raw_json


def test_weather_linker_uses_weather_series_hints(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        market = upsert_market(
            session,
            {
                "ticker": "KXHIGHCHI-85",
                "title": "Will Chicago high be above 85 F?",
                "series_ticker": "KXHIGHCHI",
            },
        )

        detection = detect_weather_market(market)

    assert detection.weather_metric == "TEMPERATURE"
    assert detection.location_key == "chicago"
    assert detection.target_operator == "ABOVE"
    assert detection.target_value == Decimal("85")


def test_sports_linker_creates_market_derived_link_when_no_games(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        upsert_market(
            session,
            {
                "ticker": "KXMLB-DODGERS-YANKEES",
                "title": "Will the Dodgers beat the Yankees in tonight's MLB game?",
                "series_ticker": "KXMLB",
            },
        )

        summary = link_sports_markets(
            session,
            league="ALL",
            settings=Settings(sports_min_link_confidence=Decimal("0.50")),
        )
        links = session.scalar(select(func.count(SportsMarketLink.id)))

    assert summary.games_scanned == 0
    assert summary.market_derived_links == 1
    assert links == 1


def test_derive_sports_schedule_creates_usable_links_and_features(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        market = upsert_market(
            session,
            {
                "ticker": "KXMVESPORTSMULTIGAMEEXTENDED-CAL-RALEIGH",
                "title": "yes Cal Raleigh: 1+",
                "series_ticker": "KXMVESPORTSMULTIGAMEEXTENDED",
            },
        )
        market.close_time = datetime(2026, 6, 24, 12, 0, 0)
        parse_and_store_market_legs(session, refresh=True)

        summary = derive_sports_schedule_from_market_legs(session)
        rerun = derive_sports_schedule_from_market_legs(session)
        teams = session.scalar(select(func.count(SportsTeam.id)))
        game_rows = list(session.scalars(select(SportsGame)))
        links = list(session.scalars(select(SportsMarketLink)))
        features = list(session.scalars(select(SportsFeature)))

    assert summary.sports_markets_seen == 1
    assert summary.teams_created == 2
    assert summary.games_created == 1
    assert summary.links_created == 1
    assert summary.features_created == 1
    assert rerun.links_existing == 1
    assert rerun.features_existing == 1
    assert teams == 2
    assert len(game_rows) == 1
    assert game_rows[0].scheduled_at is not None
    assert len(links) == 1
    assert len(features) == 1
    assert links[0].game_key.startswith("SPORTS:kalshi-event-derived:")
    assert "KALSHI_EVENT_DERIVED" in links[0].raw_json
    assert features[0].ticker == "KXMVESPORTSMULTIGAMEEXTENDED-CAL-RALEIGH"


def test_settlement_watcher_skips_learning_when_daily_cap_reached(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed_paper_order(session)
        session.commit()

    def fail_learning(_session, _settings):
        raise AssertionError("learning should not run while daily cap is reached")

    result = run_settlement_watcher(
        session_factory,
        settings=Settings(learning_max_daily_paper_trades=1),
        jobs=SettlementWatchJobs(
            sync_settlements=lambda _session, _settings: 2,
            paper_pnl=lambda _session, _settings: None,
            learning_once=fail_learning,
        ),
        cycles=1,
        interval_minutes=0,
        sleeper=lambda _seconds: None,
    )

    assert result.status == "COMPLETED"
    assert result.skipped_due_to_cap == 1
    assert result.learning_cycles_started == 0


def test_settlement_watcher_resumes_with_lower_paper_thresholds_after_cap_reset(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    captured: dict[str, object] = {}

    def fake_learning(_session, settings):
        captured["min_score"] = settings.learning_min_opportunity_score
        captured["min_edge"] = settings.learning_min_edge
        captured["scan_limit"] = settings.learning_candidate_scan_limit
        captured["execution_enabled"] = settings.execution_enabled
        return LearningCycleResult(
            run_id=1,
            cycle_id=1,
            cycle_number=1,
            status="COMPLETED",
            markets_scanned=0,
            forecasts_generated=0,
            opportunities_found=0,
            paper_trades_created=1,
            settlements_synced=0,
            settled_paper_trades_total=0,
            errors=[],
            summary={},
        )

    result = run_settlement_watcher(
        session_factory,
        settings=Settings(learning_max_daily_paper_trades=1),
        jobs=SettlementWatchJobs(
            sync_settlements=lambda _session, _settings: 1,
            paper_pnl=lambda _session, _settings: None,
            learning_once=fake_learning,
        ),
        cycles=1,
        interval_minutes=0,
        sleeper=lambda _seconds: None,
    )

    assert result.learning_cycles_started == 1
    assert captured == {
        "min_score": Decimal("25"),
        "min_edge": Decimal("0.01"),
        "scan_limit": 500,
        "execution_enabled": False,
    }


def test_phase3y_report_renders(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    output = Path(tmp_path) / "phase3y.md"
    with session_factory() as session:
        path = generate_phase3y_report(session, output_path=output, settings=Settings())

    text = path.read_text(encoding="utf-8")
    assert "Phase 3Y Settlement & Link Remediation" in text
    assert "PAPER ONLY" in text
    assert "Resume thresholds" in text


def test_paper_settlement_doctor_marks_exact_settlement_eligible(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        order = _seed_paper_order(session)
        order.ticker = "KXCAP-TEST"
        upsert_market(
            session,
            {
                "ticker": order.ticker,
                "title": "Will test settle yes?",
                "status": "settled",
                "result": "yes",
            },
        )
        upsert_settlement(
            session,
            {
                "ticker": order.ticker,
                "result": "yes",
                "settlement_ts": "2026-06-24T12:00:00Z",
            },
        )

        payload = build_paper_settlement_reconciliation(session)

    assert payload["summary"]["eligible_to_settle_now"] == 1
    assert payload["rows"][0]["reason"] == "ELIGIBLE_TO_SETTLE_NOW"
    assert payload["recommended_next_action"].startswith("Run kalshi-bot paper-pnl")


def test_paper_settlement_doctor_flags_possible_ticker_mismatch(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        order = _seed_paper_order(session)
        order.ticker = "KXTEST-S20260624-ABC"
        upsert_market(
            session,
            {
                "ticker": order.ticker,
                "event_ticker": "KXTEST-S20260624",
                "status": "settled",
                "title": "Mismatch paper ticker",
            },
        )
        upsert_market(
            session,
            {
                "ticker": "KXTEST-S20260624-XYZ",
                "event_ticker": "KXTEST-S20260624",
                "status": "settled",
                "title": "Settled sibling ticker",
            },
        )
        upsert_settlement(
            session,
            {
                "ticker": "KXTEST-S20260624-XYZ",
                "result": "no",
                "settlement_ts": "2026-06-24T12:00:00Z",
            },
        )

        payload = build_paper_settlement_reconciliation(session)
        row = payload["rows"][0]

    assert row["reason"] == "POSSIBLE_TICKER_MISMATCH"
    assert row["possible_settlement_matches"][0]["ticker"] == "KXTEST-S20260624-XYZ"
    assert payload["summary"]["possible_ticker_mismatches"] == 1


def test_paper_settlement_doctor_explains_sibling_different_contract_leg(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        order = _seed_paper_order(session)
        order.ticker = "KXTEST-S20260624-ABC"
        upsert_market(
            session,
            {
                "ticker": order.ticker,
                "event_ticker": "KXTEST-S20260624",
                "series_ticker": "KXTEST",
                "status": "settled",
                "title": "Paper target price leg",
            },
        )
        upsert_market(
            session,
            {
                "ticker": "KXTEST-S20260624-XYZ",
                "event_ticker": "KXTEST-S20260624",
                "series_ticker": "KXTEST",
                "status": "settled",
                "title": "Sibling target price leg",
            },
        )
        session.add_all(
            [
                _market_leg(order.ticker, threshold="100"),
                _market_leg("KXTEST-S20260624-XYZ", threshold="200"),
            ]
        )
        upsert_settlement(
            session,
            {
                "ticker": "KXTEST-S20260624-XYZ",
                "result": "yes",
                "settlement_ts": "2026-06-24T12:00:00Z",
            },
        )

        payload = build_paper_settlement_reconciliation(session)
        row = payload["rows"][0]

    assert row["reason"] == "SIBLING_DIFFERENT_CONTRACT_LEG"
    assert not row["eligible_to_settle_now"]
    assert row["settlement_resolution_policy"] == "EXACT_TICKER_ONLY_DO_NOT_RESOLVE_SIBLING"
    match = row["possible_settlement_matches"][0]
    assert match["same_event"] is True
    assert match["same_stem"] is True
    assert match["leg_identity_status"] == "DIFFERENT_CONTRACT_LEG"
    assert payload["summary"]["sibling_different_contract_leg"] == 1


def test_paper_settlement_doctor_reports_close_buckets_and_learning_guidance(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        now = utc_now()
        for ticker, close_time in (
            ("KXFAST-TEST", now + timedelta(hours=4)),
            ("KXMID-TEST", now + timedelta(hours=60)),
            ("KXSLOW-TEST", now + timedelta(days=10)),
        ):
            order = _seed_paper_order(session)
            order.ticker = ticker
            upsert_market(
                session,
                {
                    "ticker": ticker,
                    "status": "open",
                    "title": ticker,
                    "close_time": close_time,
                },
            )

        payload = build_paper_settlement_reconciliation(session)

    assert payload["close_time_buckets"]["0-6h"] == 1
    assert payload["close_time_buckets"]["2-3d"] == 1
    assert payload["close_time_buckets"].get("7d+", 0) + payload["close_time_buckets"].get(
        "unknown",
        0,
    ) == 1
    guidance = payload["learning_slow_settlement_guidance"]
    assert guidance["active_unsettled_trades"] == 3
    assert guidance["recommended_env"]["LEARNING_PRIORITIZE_FAST_SETTLEMENT"] == "true"
    assert guidance["recommended_env"]["EXECUTION_ENABLED"] == "false"


def test_paper_settlement_doctor_reports_open_or_malformed_tickers(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        open_order = _seed_paper_order(session)
        open_order.ticker = "KXOPEN-TEST"
        upsert_market(
            session,
            {
                "ticker": open_order.ticker,
                "status": "open",
                "title": "Still open test market",
            },
        )
        malformed = _seed_paper_order(session)
        malformed.ticker = "bad ticker"

        payload = build_paper_settlement_reconciliation(session)
        reasons = {row["ticker"]: row["reason"] for row in payload["rows"]}

    assert reasons["KXOPEN-TEST"] == "MARKET_STILL_OPEN"
    assert reasons["bad ticker"] == "MALFORMED_TICKER"


def test_paper_settlement_doctor_writes_reports(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    output_dir = Path(tmp_path) / "settlement_doctor"
    with session_factory() as session:
        _seed_paper_order(session)
        artifacts = write_paper_settlement_reconciliation(session, output_dir=output_dir)

    text = artifacts.markdown_path.read_text(encoding="utf-8")
    assert artifacts.json_path.exists()
    assert artifacts.rows_path.exists()
    assert "Phase 3Y-SR Paper Settlement Reconciliation Doctor" in text
    assert "Sibling / Contract Leg Reconciliation" in text
    assert "Learning Slow-Settlement Guidance" in text
    assert "Recommended Next Action" in text


def test_phase3y_cli_help() -> None:
    runner = CliRunner()
    for command in (
        "link-remediate",
        "derive-sports-schedule",
        "settlement-watch",
        "phase3y-report",
        "paper-settlement-doctor",
    ):
        result = runner.invoke(app, [command, "--help"])
        assert result.exit_code == 0
        assert "Usage" in result.output


def _session_factory(tmp_path):
    engine = init_db(f"sqlite:///{Path(tmp_path) / 'phase3y.db'}")
    return get_session_factory(engine)


def _seed_paper_order(session) -> PaperOrder:
    order = PaperOrder(
        ticker="CAP-TEST",
        forecast_id=None,
        created_at=utc_now(),
        model_name="ensemble_v2",
        side="BUY_YES",
        probability="0.55",
        market_price="0.50",
        limit_price="0.50",
        edge="0.05",
        quantity=1,
        status=ORDER_FILLED,
        reason="daily cap test",
        raw_decision_json="{}",
    )
    session.add(order)
    session.flush()
    return order


def _market_leg(ticker: str, *, threshold: str) -> MarketLeg:
    return MarketLeg(
        ticker=ticker,
        leg_index=0,
        parsed_at=utc_now(),
        side="YES",
        category="crypto",
        market_type="TARGET_PRICE",
        entity_name="BTC",
        operator="ABOVE",
        threshold_value=threshold,
        unit="USD",
        confidence="0.95",
        raw_text=f"yes Target Price: ${threshold}",
        reason="test leg",
        raw_json="{}",
    )
