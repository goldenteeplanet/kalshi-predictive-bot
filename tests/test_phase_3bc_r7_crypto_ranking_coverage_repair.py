from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import func, select
from typer.testing import CliRunner

import kalshi_predictor.phase3bc_r7 as phase3bc_r7
from kalshi_predictor.cli import app
from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.crypto.repository import insert_crypto_market_link
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.repositories import encode_json, insert_forecast, insert_market_snapshot
from kalshi_predictor.data.schema import MarketLeg, MarketRanking
from kalshi_predictor.forecasting.base import ForecastOutput
from kalshi_predictor.phase3bc_r7 import (
    ISSUE_EXPIRED_CRYPTO_WINDOW,
    ISSUE_RANKING_BEFORE_FORECAST,
    ISSUE_RANKING_MISSING,
    build_phase3bc_r7_payload,
    classify_phase3bc_r7_rows,
    write_phase3bc_r7_crypto_ranking_coverage_repair_report,
)
from kalshi_predictor.utils.time import utc_now


def test_phase3bc_r7_classifies_repairable_ranking_gaps() -> None:
    now = utc_now()
    fresh = (now - timedelta(minutes=2)).isoformat()
    stale = (now - timedelta(minutes=20)).isoformat()

    rows = classify_phase3bc_r7_rows(
        [
            _router_row("KXBTC-MISSING", snapshot_at=fresh, forecast_at=fresh),
            _router_row(
                "KXETH-STALE",
                snapshot_at=fresh,
                forecast_at=fresh,
                ranking_at=stale,
            ),
            _router_row(
                "KXSOL-MIXED",
                snapshot_at=fresh,
                forecast_at=fresh,
                structure_status="MIXED_CATEGORY",
            ),
        ],
        freshness_minutes=15,
        now=now,
    )

    by_ticker = {row["ticker"]: row for row in rows}
    assert by_ticker["KXBTC-MISSING"]["coverage_issue"] == ISSUE_RANKING_MISSING
    assert by_ticker["KXBTC-MISSING"]["repairable"] is True
    assert by_ticker["KXETH-STALE"]["coverage_issue"] == ISSUE_RANKING_BEFORE_FORECAST
    assert by_ticker["KXETH-STALE"]["repairable"] is True
    assert "KXSOL-MIXED" not in by_ticker


def test_phase3bc_r7_excludes_expired_crypto_windows_from_current_repair_gap() -> None:
    now = datetime(2026, 6, 30, 23, 30, tzinfo=UTC)
    fresh = (now - timedelta(minutes=2)).isoformat()

    diagnostics = classify_phase3bc_r7_rows(
        [
            _router_row(
                "KXBTC-26JUN3019-B59050",
                snapshot_at=fresh,
                forecast_at=fresh,
            )
        ],
        freshness_minutes=15,
        now=now,
    )
    payload = build_phase3bc_r7_payload(
        before_rows=[],
        before_diagnostics=diagnostics,
        before_phase3bc_summary={},
        repair_results=[],
        freshness_minutes=15,
        repair_rankings=False,
        repair_limit=500,
        limit=10,
    )

    row = diagnostics[0]
    assert row["coverage_issue"] == ISSUE_EXPIRED_CRYPTO_WINDOW
    assert row["active_window_status"] == "EXPIRED"
    assert row["repairable"] is False
    assert row["ticker_close_time_utc"] == "2026-06-30T23:00:00+00:00"
    summary = payload["summary"]
    assert summary["active_pure_crypto_rows"] == 1
    assert summary["current_active_window_rows"] == 0
    assert summary["expired_crypto_window_rows"] == 1
    assert summary["missing_or_stale_ranking_rows_before"] == 0
    assert summary["repairable_ranking_rows_before"] == 0
    assert summary["main_gap_before"] == "EXPIRED_CRYPTO_WINDOWS_ONLY"


def test_phase3bc_r7_reconciles_stale_local_gap_with_r5_post_refresh_truth(
    tmp_path,
    monkeypatch,
) -> None:
    reports_dir = Path(tmp_path) / "reports"
    _write_r5_ev_not_positive_status(reports_dir)
    now = utc_now()
    stale = (now - timedelta(minutes=30)).isoformat()
    fresh = (now - timedelta(minutes=2)).isoformat()

    def fake_router(*args, **kwargs):
        return {
            "summary": {"main_blocker": "SNAPSHOT_STALE"},
            "rows": [
                _router_row(
                    "KXBTC-R5-ALIGNMENT",
                    snapshot_at=stale,
                    forecast_at=fresh,
                    ranking_at=fresh,
                )
            ],
        }

    monkeypatch.setattr(
        phase3bc_r7,
        "build_phase3bc_crypto_clean_opportunity_router",
        fake_router,
    )
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        artifacts = phase3bc_r7.write_phase3bc_r7_crypto_ranking_coverage_repair_report(
            session,
            output_dir=reports_dir / "phase3bc_r7",
            settings=_settings(),
            limit=10,
            repair_rankings=False,
        )

    payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))
    summary = payload["summary"]
    assert summary["raw_main_gap_before"] == "SNAPSHOT_STALE"
    assert summary["main_gap_before"] == "EV_NOT_POSITIVE"
    assert summary["main_gap_after"] == "EV_NOT_POSITIVE"
    assert summary["r5_alignment_applied"] is True
    assert payload["next_commands"] == [
        "kalshi-bot phase3bc-r5-status --output-dir reports/phase3bc_r5"
    ]


def test_phase3bc_r7_repairs_only_active_pure_crypto_rankings(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed_crypto_candidate(session, ticker="KXBTC-PURE-NO-RANKING")
        _seed_crypto_candidate(
            session,
            ticker="KXBTC-MIXED-NO-RANKING",
            include_sports_leg=True,
        )

        artifacts = write_phase3bc_r7_crypto_ranking_coverage_repair_report(
            session,
            output_dir=Path(tmp_path) / "phase3bc_r7",
            settings=_settings(),
            limit=10,
            repair_rankings=True,
            repair_limit=10,
        )
        session.commit()
        ranking_count = session.scalar(select(func.count()).select_from(MarketRanking))
        pure_ranking = session.scalar(
            select(MarketRanking).where(MarketRanking.ticker == "KXBTC-PURE-NO-RANKING")
        )
        mixed_ranking = session.scalar(
            select(MarketRanking).where(MarketRanking.ticker == "KXBTC-MIXED-NO-RANKING")
        )

    payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))
    assert payload["summary"]["rankings_inserted"] == 1
    assert payload["summary"]["missing_or_stale_ranking_rows_after"] == 0
    assert ranking_count == 1
    assert pure_ranking is not None
    assert mixed_ranking is None
    assert payload["live_or_demo_execution"] is False
    assert payload["order_submission"] is False


def test_phase3bc_r7_cli_smoke(tmp_path) -> None:
    get_settings.cache_clear()
    runner = CliRunner()
    db_url = f"sqlite:///{Path(tmp_path) / 'phase3bc_r7_cli.db'}"
    output_dir = Path(tmp_path) / "phase3bc_r7"

    result = runner.invoke(
        app,
        [
            "phase3bc-r7-crypto-ranking-coverage-repair",
            "--output-dir",
            str(output_dir),
            "--limit",
            "10",
        ],
        env={"KALSHI_DB_URL": db_url},
    )

    get_settings.cache_clear()
    assert result.exit_code == 0
    assert "PAPER ONLY" in result.output
    assert "Order submission/cancel/replace: blocked" in result.output
    assert (output_dir / "phase3bc_r7_crypto_ranking_coverage_repair.json").exists()


def _router_row(
    ticker: str,
    *,
    snapshot_at: str | None = None,
    forecast_at: str | None = None,
    ranking_at: str | None = None,
    structure_status: str = "PURE_CRYPTO",
) -> dict[str, object]:
    return {
        "ticker": ticker,
        "clean_title": ticker,
        "active_market": True,
        "structure_status": structure_status,
        "latest_snapshot_at": snapshot_at,
        "latest_forecast_at": forecast_at,
        "latest_ranking_at": ranking_at,
        "readiness_status": "BLOCKED_FORECAST_NOT_RANKED",
    }


def _seed_crypto_candidate(
    session,
    *,
    ticker: str,
    include_sports_leg: bool = False,
) -> None:
    now = utc_now()
    insert_market_snapshot(
        session,
        {
            "ticker": ticker,
            "status": "open",
            "title": "yes Target Price: $100,000",
            "series_ticker": "KXBTC",
            "event_ticker": f"{ticker}-EVENT",
            "yes_bid_dollars": "0.38",
            "yes_ask_dollars": "0.40",
            "no_bid_dollars": "0.59",
            "no_ask_dollars": "0.62",
            "volume_fp": "500",
            "open_interest_fp": "250",
            "liquidity_dollars": "1000",
            "close_time": (now + timedelta(hours=6)).isoformat(),
        },
        {"orderbook_fp": {"yes_dollars": [["0.38", "10"]], "no_dollars": [["0.59", "10"]]}},
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
    if include_sports_leg:
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
    insert_forecast(
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
    engine = init_db(f"sqlite:///{Path(tmp_path) / 'phase3bc_r7.db'}")
    return get_session_factory(engine)
