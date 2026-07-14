from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import func, select
from typer.testing import CliRunner

from kalshi_predictor.cli import app
from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.repositories import insert_forecast, insert_market_snapshot
from kalshi_predictor.data.schema import PaperOrder
from kalshi_predictor.phase3ay_positive_ev import (
    build_phase3ay_positive_ev_accelerator,
    write_phase3ay_positive_ev_accelerator_report,
)
from kalshi_predictor.phase3bc_r5 import MODEL_NAME
from kalshi_predictor.utils.time import utc_now


def test_phase3ay_positive_ev_accelerator_ranks_current_near_miss_only(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    output_dir = reports_dir / "phase3ay"
    with session_factory() as session:
        _seed_crypto_row(session, "KXBTC-30JAN0101-B50000", ev_cents=Decimal("-0.5"))
        _seed_crypto_row(
            session,
            "KXXRP-30JAN0101-B2",
            ev_cents=Decimal("-0.4"),
            expired=True,
        )
        before_orders = session.scalar(select(func.count(PaperOrder.id)))
        payload = build_phase3ay_positive_ev_accelerator(
            session,
            reports_dir=reports_dir,
            settings=_settings(),
            symbols="BTC,ETH,XRP,DOGE",
            near_miss_cents=Decimal("1.0"),
            max_candidates=50,
            refresh_snapshots=False,
            registered_commands={
                "phase3ay-positive-ev-accelerator",
                "phase3bc-r5-status",
                "phase3ax-gap-analysis",
            },
        )
        artifacts = write_phase3ay_positive_ev_accelerator_report(
            session,
            output_dir=output_dir,
            reports_dir=reports_dir,
            settings=_settings(),
            near_miss_cents=Decimal("1.0"),
            max_candidates=50,
            refresh_snapshots=False,
            registered_commands={"phase3ay-positive-ev-accelerator"},
        )
        after_orders = session.scalar(select(func.count(PaperOrder.id)))

    assert payload["summary"]["current_near_miss_rows"] == 1
    assert payload["summary"]["positive_ev_crossed_after_refresh"] == 0
    assert payload["summary"]["first_hard_blocker"] == "NEAR_MISS_NO_POSITIVE_EV"
    assert payload["near_miss_rows"][0]["ticker"] == "KXBTC-30JAN0101-B50000"
    assert all(row["ticker"] != "KXXRP-30JAN0101-B2" for row in payload["near_miss_rows"])
    assert payload["paper_trade_creation"] is False
    assert payload["thresholds_lowered"] is False
    assert before_orders == after_orders == 0
    assert artifacts.json_path.exists()
    assert "KXBTC-30JAN0101-B50000" in artifacts.near_miss_rows_path.read_text(
        encoding="utf-8"
    )


def test_phase3ay_positive_ev_accelerator_skips_duplicate_watcher_refresh(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_active_r5_status(reports_dir)
    with session_factory() as session:
        _seed_crypto_row(session, "KXDOGE-30JAN0101-B1", ev_cents=Decimal("-0.2"))
        payload = build_phase3ay_positive_ev_accelerator(
            session,
            reports_dir=reports_dir,
            settings=_settings(),
            symbols="DOGE",
            refresh_snapshots=True,
            allow_concurrent_refresh=False,
            registered_commands={"phase3ay-positive-ev-accelerator"},
        )

    assert payload["summary"]["current_near_miss_rows"] == 1
    assert payload["refresh_result"]["status"] == "SKIPPED_ACTIVE_CRYPTO_WATCHER"
    assert payload["summary"]["first_hard_blocker"] == "NEAR_MISS_WAIT_FOR_ACTIVE_R5_REFRESH"
    assert payload["refresh_result"]["paper_trade_creation"] is False


def test_phase3ay_positive_ev_accelerator_cli_help_registered() -> None:
    result = CliRunner().invoke(app, ["phase3ay-positive-ev-accelerator", "--help"])
    assert result.exit_code == 0
    assert "--near-miss-cents" in result.output
    assert "--max-candidates" in result.output


def _session_factory(tmp_path):
    get_settings.cache_clear()
    engine = init_db(f"sqlite:///{Path(tmp_path) / 'phase3ay_positive_ev.db'}")
    return get_session_factory(engine)


def _seed_crypto_row(
    session,
    ticker: str,
    *,
    ev_cents: Decimal,
    expired: bool = False,
) -> None:
    now = utc_now()
    close_time = now - timedelta(hours=1) if expired else now + timedelta(hours=3)
    yes_ask = Decimal("0.50")
    probability = yes_ask + (ev_cents / Decimal("100"))
    market_json = {
        "ticker": ticker,
        "title": f"{ticker} test crypto window",
        "event_ticker": ticker.rsplit("-", 1)[0],
        "series_ticker": ticker.split("-", 1)[0],
        "market_type": "binary",
        "status": "active",
        "close_time": close_time.isoformat(),
        "expected_expiration_time": (close_time + timedelta(minutes=5)).isoformat(),
        "yes_bid_dollars": "0.49",
        "yes_ask_dollars": "0.50",
        "no_bid_dollars": "0.50",
        "no_ask_dollars": "0.51",
        "liquidity_dollars": "1000",
        "volume_fp": "1000",
        "open_interest_fp": "1000",
    }
    insert_market_snapshot(
        session,
        market_json,
        {
            "orderbook": {
                "yes": [[49, 10]],
                "no": [[50, 10]],
            }
        },
        now,
    )
    insert_forecast(
        session,
        {
            "ticker": ticker,
            "forecasted_at": now,
            "model_name": MODEL_NAME,
            "yes_probability": probability,
            "market_mid_probability": Decimal("0.495"),
            "best_yes_bid": Decimal("0.49"),
            "best_yes_ask": yes_ask,
            "feature_json": {"source": "phase3ay-test"},
        },
    )
    session.flush()


def _write_active_r5_status(reports_dir: Path) -> None:
    r5_dir = reports_dir / "phase3bc_r5"
    r5_dir.mkdir(parents=True, exist_ok=True)
    now = utc_now().isoformat()
    payload = {
        "generated_at": now,
        "process": {"status": "RUNNING"},
        "guard": {"status": "RUNNING"},
        "latest_summary": {"watch_state": "WAITING_FOR_POSITIVE_EV"},
    }
    (r5_dir / "phase3bc_r5_status.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )


def _settings() -> Settings:
    return Settings(
        learning_mode=False,
        opportunity_max_spread=Decimal("0.02"),
        opportunity_min_time_to_close_minutes=1,
    )
