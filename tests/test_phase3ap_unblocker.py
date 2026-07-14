from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from typer.testing import CliRunner

from kalshi_predictor.cli import app
from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.repositories import encode_json
from kalshi_predictor.data.schema import (
    AdvancedRiskDecisionLog,
    Forecast,
    Market,
    MarketRanking,
    MarketSnapshot,
    PositionSizingDecisionLog,
)
from kalshi_predictor.opportunities.market_identity import BUILT_FROM_EXACT_CATALOG
from kalshi_predictor.paper.models import BUY_YES
from kalshi_predictor.phase3ap import (
    build_phase3ap_book_diagnostic,
    build_phase3ap_paper_ready_gate,
    write_phase3ap_paper_ready_unblock_report,
)
from kalshi_predictor.phase3aq import (
    build_phase3aq_positive_ev_link_audit,
    build_phase3aq_refresh_verified_opportunity_books,
    write_phase3aq_link_and_book_unblock_report,
)
from kalshi_predictor.ui.service import (
    _extend_phase3ap_blockers,
    _extend_phase3aq_blockers,
    _phase3aq_dashboard_status_payload,
    _phase3ap_dashboard_status_payload,
)
from kalshi_predictor.utils.time import utc_now


def test_phase3ap_explains_positive_ev_empty_book_without_lowering_thresholds(tmp_path) -> None:
    settings = _settings(tmp_path)
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed_ranked_market(session, ticker="KXP3AP-EMPTY", orderbook={})

        payload = build_phase3ap_book_diagnostic(session, settings=settings)

    row = payload["positive_ev_rows"][0]
    assert payload["summary"]["positive_ev_rows"] == 1
    assert payload["summary"]["positive_ev_no_executable_book_rows"] == 1
    assert row["executable_book"] is False
    assert row["no_book_reason"] == "EMPTY_ORDERBOOK"
    assert payload["thresholds"]["thresholds_lowered"] == "False"
    assert payload["live_or_demo_execution"] is False
    assert payload["order_submission"] is False


def test_phase3ap_stale_quote_is_not_executable(tmp_path) -> None:
    settings = _settings(tmp_path)
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed_ranked_market(
            session,
            ticker="KXP3AP-STALE",
            snapshot_age_minutes=45,
        )

        gate = build_phase3ap_paper_ready_gate(session, settings=settings)

    row = gate["rows"][0]
    assert row["paper_ready"] is False
    assert row["book_freshness_state"] == "STALE_ORDERBOOK"
    assert row["primary_blocker"] == "STALE_QUOTE"
    assert "NO_ORDERBOOK_SNAPSHOT" in row["secondary_blockers"]


def test_phase3ap_wide_spread_and_thin_book_are_specific_secondary_blockers(tmp_path) -> None:
    settings = _settings(tmp_path)
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed_ranked_market(
            session,
            ticker="KXP3AP-WIDE",
            orderbook=_orderbook(yes_bid="0.38", no_bid="0.50"),
            ranking_spread="0.12",
        )
        _seed_ranked_market(
            session,
            ticker="KXP3AP-THIN",
            liquidity_score="5",
        )

        gate = build_phase3ap_paper_ready_gate(session, settings=settings)

    rows = {row["ticker"]: row for row in gate["rows"]}
    assert rows["KXP3AP-WIDE"]["no_book_reason"] == "WIDE_SPREAD"
    assert rows["KXP3AP-WIDE"]["primary_blocker"] == "SPREAD_TOO_WIDE"
    assert rows["KXP3AP-THIN"]["no_book_reason"] == "INSUFFICIENT_DEPTH"
    assert rows["KXP3AP-THIN"]["primary_blocker"] == "LIQUIDITY_TOO_LOW"


def test_phase3ap_clean_open_market_with_known_terms_can_be_paper_ready(tmp_path) -> None:
    settings = _settings(tmp_path)
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed_ranked_market(session, ticker="KXP3AP-READY")

        gate = build_phase3ap_paper_ready_gate(session, settings=settings)

    row = gate["rows"][0]
    assert row["paper_ready"] is True
    assert row["primary_blocker"] == "PAPER_READY"
    assert row["settlement_specific_reason"] == "MARKET_NOT_SETTLEABLE_YET"
    assert row["settlement_terms_known"] is True
    assert row["paper_entry_settlement_eligible"] is True
    assert row["kalshi_url_verified"] is True


def test_phase3ap_unknown_settlement_terms_block_paper_ready(tmp_path) -> None:
    settings = _settings(tmp_path)
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed_ranked_market(session, ticker="KXP3AP-NORULES", rules=False)

        gate = build_phase3ap_paper_ready_gate(session, settings=settings)

    row = gate["rows"][0]
    assert row["paper_ready"] is False
    assert row["primary_blocker"] == "SETTLEMENT_TERMS_UNKNOWN"
    assert row["settlement_specific_reason"] == "SETTLEMENT_RULE_MISSING"


def test_phase3ap_report_artifacts_and_ui_payload(tmp_path) -> None:
    settings = _settings(tmp_path)
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed_ranked_market(session, ticker="KXP3AP-REPORT")
        artifacts = write_phase3ap_paper_ready_unblock_report(
            session,
            output_dir=Path(tmp_path) / "phase3ap",
            reports_dir=Path(tmp_path),
            settings=settings,
        )

    gate = json.loads(artifacts.paper_ready_gate_path.read_text(encoding="utf-8"))
    payload = _phase3ap_dashboard_status_payload(
        gate_path=artifacts.paper_ready_gate_path,
        book_path=artifacts.book_json_path,
    )
    blockers: list[dict] = []
    _extend_phase3ap_blockers(blockers, payload)

    assert artifacts.executive_summary_path.exists()
    assert artifacts.next_actions_path.exists()
    assert artifacts.book_json_path.exists()
    assert artifacts.settlement_json_path.exists()
    assert artifacts.positive_ev_csv_path.exists()
    assert artifacts.no_executable_book_csv_path.exists()
    assert artifacts.settlement_blocked_csv_path.exists()
    assert artifacts.manifest_path.exists()
    assert gate["summary"]["paper_ready_rows"] == 1
    assert gate["live_or_demo_execution"] is False
    assert blockers[0]["area"] == "Canonical paper-ready gate"


def test_phase3ap_cli_commands_are_registered_and_read_only(tmp_path) -> None:
    db_url = f"sqlite:///{Path(tmp_path) / 'phase3ap_cli.db'}"
    engine = init_db(db_url)
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        _seed_ranked_market(session, ticker="KXP3AP-CLI")

    runner = CliRunner()
    for command in (
        "phase3ap-book-diagnostic",
        "phase3ap-refresh-positive-ev-books",
        "phase3ap-settlement-check-diagnostic",
        "phase3ap-paper-ready-unblock-report",
    ):
        result = runner.invoke(app, [command, "--help"])
        assert result.exit_code == 0
        assert command in result.output

    get_settings.cache_clear()
    output_dir = Path(tmp_path) / "cli_report"
    result = runner.invoke(
        app,
        [
            "phase3ap-book-diagnostic",
            "--output-dir",
            str(output_dir),
            "--limit",
            "20",
        ],
        env={"DATABASE_URL": db_url, "KALSHI_DB_URL": db_url},
    )
    get_settings.cache_clear()

    assert result.exit_code == 0, result.output
    assert "Mode: PAPER / READ ONLY" in result.output
    assert "Order submission/cancel/replace: blocked" in result.output
    assert output_dir.joinpath("book_diagnostic.json").exists()


def test_phase3aq_replaces_generic_link_blocker_with_specific_url_status(tmp_path) -> None:
    settings = _settings(tmp_path)
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed_ranked_market(
            session,
            ticker="KXP3AQ-NOURL",
            include_url_fields=False,
        )

        payload = build_phase3aq_positive_ev_link_audit(session, settings=settings)

    row = payload["positive_ev_rows"][0]
    assert payload["summary"]["positive_ev_rows"] == 1
    assert payload["summary"]["url_status_counts"][BUILT_FROM_EXACT_CATALOG] == 1
    assert payload["summary"]["generic_unverified_link_rows_remaining"] == 0
    assert row["primary_blocker"] == BUILT_FROM_EXACT_CATALOG
    assert row["book_status"] == "BOOK_HELD_BEHIND_LINK_VERIFICATION"
    assert row["kalshi_url"] is None
    assert payload["acceptance"]["generic_unverified_link_removed"] is True


def test_phase3aq_verified_missing_book_becomes_refresh_candidate(tmp_path) -> None:
    settings = _settings(tmp_path)
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed_ranked_market(
            session,
            ticker="KXP3AQ-EMPTY",
            orderbook={},
        )

        payload = build_phase3aq_refresh_verified_opportunity_books(
            session,
            settings=settings,
            dry_run=True,
            apply_readonly_refresh=False,
        )

    row = payload["verified_refresh_candidates"][0]
    assert payload["status"] == "DRY_RUN"
    assert payload["market_data_writes"] is False
    assert row["url_status"] == "VERIFIED"
    assert row["book_status"] == "EMPTY_ORDERBOOK"
    assert row["book_refresh_needed"] is True


def test_phase3aq_report_artifacts_and_ui_payload(tmp_path) -> None:
    settings = _settings(tmp_path)
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed_ranked_market(
            session,
            ticker="KXP3AQ-REPORT",
            include_url_fields=False,
        )
        artifacts = write_phase3aq_link_and_book_unblock_report(
            session,
            output_dir=Path(tmp_path) / "phase3aq",
            reports_dir=Path(tmp_path),
            settings=settings,
        )

    payload = _phase3aq_dashboard_status_payload(
        gate_path=artifacts.paper_ready_gate_summary_path,
        audit_path=artifacts.positive_ev_link_audit_path,
    )
    blockers: list[dict] = []
    _extend_phase3aq_blockers(blockers, payload)

    assert artifacts.executive_summary_path.exists()
    assert artifacts.next_actions_path.exists()
    assert artifacts.positive_ev_link_audit_path.exists()
    assert artifacts.verified_book_refresh_plan_path.exists()
    assert artifacts.settlement_check_split_path.exists()
    assert artifacts.paper_ready_gate_summary_path.exists()
    assert artifacts.blocked_positive_ev_csv_path.exists()
    assert artifacts.manifest_path.exists()
    assert payload["gate_summary"]["positive_ev_rows"] == 1
    assert blockers[0]["area"] == "Verified Kalshi link gate"
    assert "Built From Exact Catalog" in blockers[0]["evidence"]


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        kalshi_db_url=f"sqlite:///{Path(tmp_path) / 'phase3ap.db'}",
        execution_enabled=False,
        execution_dry_run=True,
        ui_read_only=True,
        opportunity_min_edge=Decimal("0.03"),
        opportunity_min_score=Decimal("60"),
        opportunity_max_spread=Decimal("0.10"),
        opportunity_min_liquidity=Decimal("10"),
        opportunity_min_time_to_close_minutes=Decimal("10"),
        phase_3t_stale_after_seconds=3600,
    )


def _session_factory(tmp_path: Path):
    engine = init_db(f"sqlite:///{Path(tmp_path) / 'phase3ap.db'}")
    return get_session_factory(engine)


def _seed_ranked_market(
    session,
    *,
    ticker: str,
    orderbook: dict | None = None,
    snapshot_age_minutes: int = 1,
    probability: str = "0.70",
    best_price: str = "0.40",
    ranking_spread: str = "0.02",
    liquidity_score: str = "80",
    rules: bool = True,
    phase3m_contracts: int = 3,
    phase3n_action: str = "ALLOW",
    include_url_fields: bool = True,
) -> None:
    now = utc_now()
    event_ticker = f"{ticker}-EVENT"
    market_payload = {
        "ticker": ticker,
        "event_ticker": event_ticker,
        "series_ticker": "KXP3AP",
        "title": f"Will {ticker} finish above the fixture threshold?",
        "subtitle": "Phase 3AP fixture",
        "status": "open",
        "close_time": (now + timedelta(hours=2)).isoformat(),
        "rules_primary": "Market resolves using the listed fixture threshold." if rules else None,
    }
    if include_url_fields:
        market_payload.update(
            {
                "event_slug": "phase-3ap-fixture-event",
                "series_slug": "kxp3ap",
                "kalshi_url": (
                    "https://kalshi.com/markets/kxp3ap/"
                    f"phase-3ap-fixture-event/{event_ticker.lower()}"
                ),
            }
        )
    market = Market(
        ticker=ticker,
        event_ticker=event_ticker,
        series_ticker="KXP3AP",
        title=market_payload["title"],
        subtitle=market_payload["subtitle"],
        status="open",
        close_time=now + timedelta(hours=2),
        rules_primary=market_payload["rules_primary"],
        raw_json=encode_json(market_payload),
        first_seen_at=now,
        last_seen_at=now,
    )
    session.add(market)
    forecast = Forecast(
        ticker=ticker,
        forecasted_at=now,
        model_name="phase3ap_test",
        yes_probability=probability,
        market_mid_probability="0.50",
        best_yes_bid="0.38",
        best_yes_ask=best_price,
        feature_json=encode_json({"source": "phase3ap_test"}),
    )
    session.add(forecast)
    session.flush()
    session.add(
        MarketSnapshot(
            ticker=ticker,
            captured_at=now - timedelta(minutes=snapshot_age_minutes),
            status="open",
            best_yes_bid="0.38",
            best_yes_ask="0.41",
            best_no_bid="0.59",
            best_no_ask="0.62",
            spread=ranking_spread,
            raw_market_json=encode_json(market_payload),
            raw_orderbook_json=encode_json(
                _orderbook() if orderbook is None else orderbook
            ),
        )
    )
    session.add(
        MarketRanking(
            ticker=ticker,
            ranked_at=now,
            title=market.title,
            status="open",
            series_ticker="KXP3AP",
            event_ticker=event_ticker,
            volume="100",
            open_interest="100",
            liquidity="1000",
            spread=ranking_spread,
            midpoint="0.50",
            time_to_close_minutes="120",
            forecast_model="phase3ap_test",
            forecast_probability=probability,
            best_side=BUY_YES,
            best_price=best_price,
            estimated_edge=str(Decimal(probability) - Decimal(best_price)),
            liquidity_score=liquidity_score,
            spread_score="80",
            time_score="80",
            model_confidence_score="80",
            opportunity_score="80",
            reason="phase3ap fixture",
            raw_json=encode_json({"forecast_id": forecast.id}),
        )
    )
    sizing = PositionSizingDecisionLog(
        decision_timestamp=now,
        created_at=now,
        version="test",
        mode="PAPER",
        strategy_id="phase3ap_test",
        instrument=ticker,
        ticker=ticker,
        model_name="phase3ap_test",
        trade_intent_id=f"intent-{ticker}",
        order_correlation_id=f"corr-{ticker}",
        paper_order_id=None,
        tier="standard" if phase3m_contracts > 0 else "zero",
        composite_score="1.0",
        proposed_contracts=phase3m_contracts,
        live_candidate_contracts=0,
        executed_contracts=0,
        factor_scores_json="{}",
        factor_weights_json="{}",
        adjusted_historical_accuracy="0.70",
        historical_sample_size=100,
        drawdown_utilization="0",
        caps_json="{}",
        limiting_factors_json="[]",
        reason_codes_json="[]",
        fallback_used=0,
        raw_json="{}",
    )
    session.add(sizing)
    session.flush()
    session.add(
        AdvancedRiskDecisionLog(
            decision_timestamp=now,
            created_at=now,
            version="test",
            mode="PAPER",
            action=phase3n_action,
            strategy_id="phase3ap_test",
            model_id="phase3ap_test",
            category_id="general",
            instrument_id=ticker,
            correlation_group_id=ticker,
            ticker=ticker,
            trade_intent_id=f"intent-{ticker}",
            order_correlation_id=f"corr-{ticker}",
            position_sizing_decision_id=sizing.id,
            paper_order_id=None,
            reservation_id=None,
            phase_3m_tier="standard",
            phase_3m_proposed_contracts=phase3m_contracts,
            live_candidate_contracts=0,
            executed_contracts=phase3m_contracts,
            risk_per_contract="1.0",
            planned_trade_risk=str(phase3m_contracts),
            raw_caps_json="{}",
            bucketed_caps_json="{}",
            limiting_factors_json="[]",
            hard_blocks_json="[]",
            reason_codes_json="[]",
            fallback_used=0,
            raw_json="{}",
        )
    )
    session.commit()


def _orderbook(*, yes_bid: str = "0.38", no_bid: str = "0.59") -> dict:
    return {
        "orderbook_fp": {
            "yes_dollars": [[yes_bid, "4"]],
            "no_dollars": [[no_bid, "4"]],
        }
    }
