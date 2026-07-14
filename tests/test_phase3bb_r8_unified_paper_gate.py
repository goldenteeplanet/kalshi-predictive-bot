from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from kalshi_predictor.cli import app
from kalshi_predictor.config import get_settings
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.repositories import upsert_market
from kalshi_predictor.data.schema import (
    CryptoMarketLink,
    MarketSnapshot,
    WeatherForecast,
    WeatherMarketLink,
)
from kalshi_predictor.phase3bb_r8_unified_paper_gate import (
    build_phase3bb_r8_unified_paper_gate,
    write_phase3bb_r8_unified_paper_gate_report,
)
from kalshi_predictor.utils.time import utc_now


def test_phase3bb_r8_crypto_linked_row_gets_source_missing_blocker(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)

    with session_factory() as session:
        _seed_market(session, ticker="KXBTC-R8", title="Bitcoin price market")
        session.add(
            CryptoMarketLink(
                ticker="KXBTC-R8",
                symbol="BTC",
                detected_at=utc_now(),
                confidence="1.0",
                reason="exact test crypto link",
                raw_json=json.dumps({"test": "r8"}),
            )
        )
        session.flush()
        payload = build_phase3bb_r8_unified_paper_gate(
            session,
            reports_dir=Path(tmp_path) / "reports",
            limit_per_category=50,
        )

    rows = {row["ticker"]: row for row in payload["paper_gate_rows"]}
    assert rows["KXBTC-R8"]["first_blocker"] == "SOURCE_MISSING"
    assert rows["KXBTC-R8"]["verified_kalshi_link"] is True
    assert rows["KXBTC-R8"]["paper_ready"] is False
    assert payload["summary"]["paper_ready_rows"] == 0
    assert payload["summary"]["stale_3ap_only_truth_used"] is False


def test_phase3bb_r8_weather_row_gets_feature_missing_after_source_snapshot(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    now = utc_now()

    with session_factory() as session:
        _seed_market(session, ticker="KXTEMPNY-R8", title="NY temperature market")
        session.add(
            WeatherMarketLink(
                ticker="KXTEMPNY-R8",
                location_key="new_york",
                detected_at=now,
                weather_metric="temperature",
                target_operator="above",
                target_value="80",
                target_time=now,
                confidence="0.99",
                reason="exact test weather link",
                raw_json=json.dumps({"test": "r8"}),
            )
        )
        session.add(
            WeatherForecast(
                location_key="new_york",
                source="test",
                forecast_generated_at=now,
                forecast_time=now,
                raw_json=json.dumps({"test": "r8"}),
                created_at=now,
            )
        )
        session.add(
            MarketSnapshot(
                ticker="KXTEMPNY-R8",
                captured_at=now,
                status="open",
                best_yes_bid="0.40",
                best_yes_ask="0.45",
                raw_market_json=json.dumps({"test": "r8"}),
                raw_orderbook_json=json.dumps({"yes": [["0.40", 10]]}),
            )
        )
        session.flush()
        payload = build_phase3bb_r8_unified_paper_gate(
            session,
            reports_dir=Path(tmp_path) / "reports",
            limit_per_category=50,
        )

    rows = {row["ticker"]: row for row in payload["paper_gate_rows"]}
    row = rows["KXTEMPNY-R8"]
    assert row["source_evidence_fresh"] is True
    assert row["snapshot_fresh"] is True
    assert row["feature_exists"] is False
    assert row["first_blocker"] == "FEATURE_MISSING"


def test_phase3bb_r8_writes_requested_artifacts(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = Path(tmp_path) / "reports"

    with session_factory() as session:
        _seed_market(session, ticker="KXBTC-R8A", title="Bitcoin price market")
        session.add(
            CryptoMarketLink(
                ticker="KXBTC-R8A",
                symbol="BTC",
                detected_at=utc_now(),
                confidence="1.0",
                reason="exact test crypto link",
                raw_json=json.dumps({"test": "r8"}),
            )
        )
        session.flush()
        artifacts = write_phase3bb_r8_unified_paper_gate_report(
            session,
            output_dir=reports_dir / "phase3bb_r8",
            reports_dir=reports_dir,
            limit_per_category=50,
        )

    assert artifacts.executive_summary_path.exists()
    assert artifacts.markdown_path.exists()
    assert artifacts.rows_csv_path.exists()
    assert artifacts.category_blockers_csv_path.exists()
    assert artifacts.manifest_path.exists()
    assert "KXBTC-R8A" in artifacts.rows_csv_path.read_text(encoding="utf-8")
    assert "SOURCE_MISSING" in artifacts.category_blockers_csv_path.read_text(
        encoding="utf-8"
    )


def test_phase3bb_r8_cli_help_registered() -> None:
    result = CliRunner().invoke(app, ["phase3bb-r8-unified-paper-gate", "--help"])

    assert result.exit_code == 0
    assert "phase3bb-r8-unified-paper-gate" in result.output
    assert "--limit-per-category" in result.output


def _session_factory(tmp_path: Path):
    get_settings.cache_clear()
    engine = init_db(f"sqlite:///{Path(tmp_path) / 'phase3bb_r8.db'}")
    return get_session_factory(engine)


def _seed_market(session, *, ticker: str, title: str) -> None:
    upsert_market(
        session,
        {
            "ticker": ticker,
            "title": title,
            "event_ticker": ticker,
            "series_ticker": ticker.split("-", 1)[0],
            "status": "open",
            "close_time": "2030-01-01T19:00:00Z",
            "market_type": "binary",
            "rules_primary": "Test settlement terms.",
        },
    )
    session.flush()
