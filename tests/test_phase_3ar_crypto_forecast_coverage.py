from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import select
from typer.testing import CliRunner

from kalshi_predictor.cli import app
from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.crypto.repository import insert_crypto_features, insert_crypto_market_link
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.repositories import insert_market_snapshot, upsert_market
from kalshi_predictor.data.schema import MarketSnapshot
from kalshi_predictor.phase3ap import build_phase3ap_safe_night_runner_plan
from kalshi_predictor.phase3ar import (
    STATUS_CLOSED_MARKET,
    STATUS_EXPIRED_WINDOW_EXCLUDED,
    STATUS_FUTURE_FEATURE,
    STATUS_NO_SNAPSHOT,
    STATUS_READY,
    STATUS_STALE_QUOTE,
    build_crypto_forecast_coverage,
    write_phase3ar_report,
)
from kalshi_predictor.utils.time import utc_now


def test_phase3ar_marks_ready_linked_crypto_market(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed_crypto_market(session, ticker="KXBTC-READY")

        payload = build_crypto_forecast_coverage(session, settings=_settings(), limit=10)

    assert payload["summary"]["linked_crypto_markets_checked"] == 1
    assert payload["summary"]["ready_to_forecast"] == 1
    assert payload["rows"][0]["status"] == STATUS_READY


def test_phase3ar_explains_future_feature_block(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        captured_at = utc_now()
        _seed_crypto_market(
            session,
            ticker="KXBTC-FUTURE",
            captured_at=captured_at,
            feature_at=captured_at + timedelta(hours=1),
        )

        payload = build_crypto_forecast_coverage(session, settings=_settings(), limit=10)

    assert payload["rows"][0]["status"] == STATUS_FUTURE_FEATURE
    assert payload["summary"]["main_blocker"] == STATUS_FUTURE_FEATURE
    assert "Repair snapshots" in payload["recommended_next_action"]


def test_phase3ar_repairs_missing_linked_snapshot(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        now = utc_now()
        upsert_market(session, _market_payload("KXBTC-NO-SNAPSHOT"))
        _seed_link(session, "KXBTC-NO-SNAPSHOT")
        _seed_feature(session, generated_at=now - timedelta(minutes=5))

        before = build_crypto_forecast_coverage(session, settings=_settings(), limit=10)
        artifacts = write_phase3ar_report(
            session,
            output_dir=Path(tmp_path) / "phase3ar",
            settings=_settings(),
            limit=10,
            repair_snapshots=True,
            client=_FakeCryptoSnapshotClient(),
        )
        stored = session.scalar(
            select(MarketSnapshot).where(MarketSnapshot.ticker == "KXBTC-NO-SNAPSHOT")
        )
        after = build_crypto_forecast_coverage(session, settings=_settings(), limit=10)

    assert before["rows"][0]["status"] == STATUS_NO_SNAPSHOT
    assert stored is not None
    assert after["rows"][0]["status"] == STATUS_READY
    assert artifacts.markdown_path.exists()
    assert "Phase 3AR Crypto Forecast Coverage Repair" in artifacts.markdown_path.read_text(
        encoding="utf-8"
    )


def test_phase3ar_marks_closed_linked_market_not_repairable(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        upsert_market(session, {**_market_payload("KXBTC-CLOSED"), "status": "closed"})
        _seed_link(session, "KXBTC-CLOSED")

        payload = build_crypto_forecast_coverage(session, settings=_settings(), limit=10)

    assert payload["rows"][0]["status"] == STATUS_CLOSED_MARKET
    assert "Closed market" in payload["rows"][0]["next_action"]


def test_phase3ar_excludes_expired_linked_crypto_window_from_forecast_coverage(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        now = utc_now()
        upsert_market(
            session,
            {
                **_market_payload("KXBTC-26JUL0809-B61750"),
                "status": "active",
                "close_time": (now - timedelta(hours=4)).isoformat(),
                "expected_expiration_time": (now - timedelta(hours=3, minutes=55)).isoformat(),
            },
        )
        _seed_link(session, "KXBTC-26JUL0809-B61750")
        _seed_feature(session, generated_at=now - timedelta(minutes=5))

        payload = build_crypto_forecast_coverage(session, settings=_settings(), limit=10)

    assert payload["rows"][0]["status"] == STATUS_EXPIRED_WINDOW_EXCLUDED
    assert payload["rows"][0]["diagnostic_only"] is True
    assert payload["rows"][0]["expired_window_excluded"] is True
    assert payload["summary"]["first_hard_blocker"] == "NO_CURRENT_POSITIVE_EV"
    assert "current_positive_ev_rows" in payload["summary"]
    assert "expired_positive_ev_rows" in payload["summary"]
    assert "book_refresh_candidates" in payload["summary"]


def test_phase3ar_coverage_marks_current_old_snapshot_stale_quote(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed_crypto_market(
            session,
            ticker="KXBTC-STALE-QUOTE",
            captured_at=utc_now() - timedelta(minutes=30),
        )

        payload = build_crypto_forecast_coverage(session, settings=_settings(), limit=10)

    assert payload["rows"][0]["status"] == STATUS_STALE_QUOTE
    assert payload["summary"]["stale_quote_rows"] == 1
    assert "repair-snapshots" in payload["rows"][0]["next_action"]


def test_phase3ar_can_scope_diagnostics_to_exact_tickers(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed_crypto_market(session, ticker="KXBTC-IN-SCOPE")
        _seed_crypto_market(session, ticker="KXBTC-OUT-OF-SCOPE")

        payload = build_crypto_forecast_coverage(
            session,
            settings=_settings(),
            limit=10,
            tickers=["KXBTC-IN-SCOPE"],
        )

    assert payload["diagnostic_scope"]["scope"] == "EXACT_TICKERS"
    assert payload["diagnostic_scope"]["ticker_count"] == 1
    assert payload["summary"]["linked_crypto_markets_checked"] == 1
    assert [row["ticker"] for row in payload["rows"]] == ["KXBTC-IN-SCOPE"]


def test_phase3ar_cli_help_smoke() -> None:
    runner = CliRunner()
    for command in ("crypto-forecast-doctor", "phase3ar-crypto-forecast-coverage"):
        result = runner.invoke(app, [command, "--help"])
        assert result.exit_code == 0
        assert "repair-snapshots" in result.output


def test_phase3ap_script_runs_crypto_doctor_and_gates_learning(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        plan = build_phase3ap_safe_night_runner_plan(
            session,
            settings=Settings(learning_mode=True),
        )

    assert "kalshi-bot crypto-forecast-doctor --repair-snapshots" in plan["script"]
    assert "CAN_LEARN=$(python - <<'PY'" in plan["script"]
    assert 'if [ "$CAN_LEARN" = "true" ]; then' in plan["script"]
    assert "learning skipped: Phase 3AL resume gate is closed" in plan["script"]


def test_phase3ar_cli_writes_report(tmp_path) -> None:
    get_settings.cache_clear()
    runner = CliRunner()
    db_url = f"sqlite:///{Path(tmp_path) / 'phase3ar_cli.db'}"
    output_dir = Path(tmp_path) / "reports"

    result = runner.invoke(
        app,
        ["crypto-forecast-doctor", "--output-dir", str(output_dir), "--limit", "10"],
        env={"KALSHI_DB_URL": db_url},
    )

    get_settings.cache_clear()
    assert result.exit_code == 0
    assert (output_dir / "phase3ar_crypto_forecast_coverage.json").exists()
    assert "PAPER ONLY" in result.output


class _FakeCryptoSnapshotClient:
    def get_market(self, ticker: str) -> dict[str, Any]:
        return _market_payload(ticker)

    def get_orderbook(self, ticker: str) -> dict[str, Any]:
        return {
            "orderbook_fp": {
                "yes_dollars": [["0.41", "25"]],
                "no_dollars": [["0.51", "25"]],
            }
        }


def _session_factory(tmp_path):
    engine = init_db(f"sqlite:///{Path(tmp_path) / 'phase3ar.db'}")
    return get_session_factory(engine)


def _settings() -> Settings:
    return Settings(
        crypto_v2_max_adjustment=Decimal("0.08"),
        crypto_v2_min_link_confidence=Decimal("0.6"),
        crypto_v2_min_history_minutes=60,
    )


def _seed_crypto_market(
    session,
    *,
    ticker: str,
    captured_at=None,
    feature_at=None,
) -> None:
    now = captured_at or utc_now()
    insert_market_snapshot(
        session,
        _market_payload(ticker),
        {
            "orderbook_fp": {
                "yes_dollars": [["0.40", "10"]],
                "no_dollars": [["0.50", "10"]],
            }
        },
        now,
    )
    _seed_link(session, ticker)
    _seed_feature(session, generated_at=feature_at or now - timedelta(minutes=1))


def _seed_link(session, ticker: str) -> None:
    insert_crypto_market_link(
        session,
        ticker=ticker,
        symbol="BTC",
        confidence="1.0",
        reason="test",
        raw_json={"structured_terms": _btc_terms_payload(ticker)},
    )


def _seed_feature(session, *, generated_at) -> None:
    insert_crypto_features(
        session,
        symbol="BTC",
        source="test",
        generated_at=generated_at,
        window_minutes=1440,
        features={
            "price": "100",
            "history_minutes": "120",
            "momentum_score": "0.5",
            "trend_direction": "UP",
        },
    )


def _market_payload(ticker: str) -> dict[str, Any]:
    now = utc_now()
    return {
        "ticker": ticker,
        "status": "open",
        "title": "Will BTC exceed 70000?",
        "series_ticker": "KXBTC",
        "event_ticker": "KXBTC-TEST",
        "yes_bid_dollars": "0.40",
        "yes_ask_dollars": "0.50",
        "close_time": (now + timedelta(hours=2)).isoformat(),
        "liquidity_dollars": "1000",
    }


def _btc_terms_payload(ticker: str) -> dict[str, Any]:
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
                "threshold_value": "70000",
                "reference_price_source": "unknown_public_reference",
            }
        ],
        "reason_codes": ["test_terms"],
        "reference_price_source": "unknown_public_reference",
        "observation_time": None,
        "expiration_time": None,
        "settlement_time": None,
        "settlement_timezone": "UTC",
        "settlement_rules": None,
        "series_ticker": "KXBTC",
        "event_ticker": "KXBTC-TEST",
        "market_type": "binary",
        "idempotency_key": f"{ticker}:btc:above:70000",
    }
