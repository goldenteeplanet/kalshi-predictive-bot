from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

from fastapi.testclient import TestClient
from typer.testing import CliRunner

from kalshi_predictor.cli import app
from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.repositories import upsert_market
from kalshi_predictor.data.schema import Market
from kalshi_predictor.institutional_dashboard.contracts import API_SCHEMA_VERSION
from kalshi_predictor.opportunities.link_audit import write_opportunity_link_audit
from kalshi_predictor.opportunities.market_identity import (
    BUILT_FROM_EXACT_CATALOG,
    COMPOSITE_LOCAL_ONLY,
    GENERAL_SOURCE_NOT_SAFE,
    MARKET_NOT_IN_CATALOG,
    MISSING_MARKET_TICKER,
    PARTIAL_PROVENANCE_BLOCKED,
    PLACEHOLDER_BLOCKED,
    STALE_CATALOG,
    SYNTHETIC_ONLY,
    VERIFIED,
    VERIFIED_BUT_CLOSED,
    verify_market_identity,
)
from kalshi_predictor.opportunities.repository import insert_market_ranking
from kalshi_predictor.ui.app import create_app
from kalshi_predictor.utils.time import utc_now


def test_phase3ao_market_identity_verifies_exact_url_and_blocks_unsafe_rows(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    settings = _settings(tmp_path, stale_after_seconds=60)
    with session_factory() as session:
        verified = _seed_opportunity(session, "KXP3AO-26JUL07-YES", url=True)
        no_url = _seed_opportunity(session, "KXP3AO-26JUL07-NOURL", url=False)
        synthetic = _seed_opportunity(
            session,
            "KXP3AO-SYNTHETIC",
            url=True,
            market_raw={"synthetic_only": True},
        )
        composite = _seed_opportunity(
            session,
            "KXMVECROSSCATEGORY-P3AO",
            url=True,
            market_raw={"composite_local_only": True},
        )
        placeholder = _seed_opportunity(
            session,
            "KXP3AO-SPORTS-PLACEHOLDER",
            url=True,
            title="Will Team 1 win the game?",
            series_ticker="KXMLB",
            market_raw={"provenance_status": "ROUND_PLACEHOLDER"},
        )
        partial = _seed_opportunity(
            session,
            "KXP3AO-PARTIAL",
            url=True,
            market_raw={"provenance_status": "PARTIAL_PROVENANCE"},
        )
        general = _seed_opportunity(
            session,
            "KXP3AO-GENERAL",
            url=True,
            market_raw={"general_source_candidate": True},
        )
        stale = _seed_opportunity(session, "KXP3AO-STALE", url=True)
        closed = _seed_opportunity(session, "KXP3AO-CLOSED", url=True, status="closed")
        stale_market = session.get(Market, stale.ticker)
        stale_market.last_seen_at = utc_now() - timedelta(seconds=3600)
        session.flush()

        assert (
            verify_market_identity(session, ranking=verified, settings=settings).url_verification_status
            == VERIFIED
        )
        no_url_identity = verify_market_identity(session, ranking=no_url, settings=settings)
        assert no_url_identity.url_verification_status == BUILT_FROM_EXACT_CATALOG
        assert no_url_identity.kalshi_url.startswith("https://kalshi.com/markets/")
        assert no_url_identity.url_verified is False
        assert no_url_identity.tradeable is False
        assert (
            verify_market_identity(session, ticker="", settings=settings).url_verification_status
            == MISSING_MARKET_TICKER
        )
        assert (
            verify_market_identity(session, ticker="KXP3AO-MISSING", settings=settings).url_verification_status
            == MARKET_NOT_IN_CATALOG
        )
        assert (
            verify_market_identity(session, ranking=synthetic, settings=settings).url_verification_status
            == SYNTHETIC_ONLY
        )
        assert (
            verify_market_identity(session, ranking=composite, settings=settings).url_verification_status
            == COMPOSITE_LOCAL_ONLY
        )
        assert (
            verify_market_identity(session, ranking=placeholder, settings=settings).url_verification_status
            == PLACEHOLDER_BLOCKED
        )
        assert (
            verify_market_identity(session, ranking=partial, settings=settings).url_verification_status
            == PARTIAL_PROVENANCE_BLOCKED
        )
        assert (
            verify_market_identity(session, ranking=general, settings=settings).url_verification_status
            == GENERAL_SOURCE_NOT_SAFE
        )
        assert (
            verify_market_identity(session, ranking=stale, settings=settings).url_verification_status
            == STALE_CATALOG
        )
        assert (
            verify_market_identity(session, ranking=closed, settings=settings).url_verification_status
            == VERIFIED_BUT_CLOSED
        )


def test_phase3ao_ui_and_institutional_api_expose_verified_links(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    settings = _settings(tmp_path)
    with session_factory() as session:
        verified = _seed_opportunity(session, "KXP3AO-UI-YES", url=True)
        _seed_opportunity(session, "KXP3AO-UI-NOURL", url=False)
        session.commit()

    client = TestClient(create_app(session_factory=session_factory, settings=settings))

    opportunities = client.get("/opportunities")
    today = client.get("/today")
    root = client.get("/")
    detail = client.get(f"/opportunities/{verified.ticker}")
    payouts = client.get("/opportunities/best-payouts")
    institutional = client.post(
        "/api/dashboard/v1/query/opportunities",
        json={"schema_version": API_SCHEMA_VERSION, "filters": {"model_id": "ensemble_v2"}},
    )

    assert opportunities.status_code == 200
    assert today.status_code == 200
    assert root.status_code == 200
    assert detail.status_code == 200
    assert payouts.status_code == 200
    assert institutional.status_code == 200
    assert "Open on Kalshi" in opportunities.text
    assert "BUILT_FROM_EXACT_CATALOG" in opportunities.text
    assert "Open on Kalshi" in today.text
    assert "Open on Kalshi" in root.text
    assert "Link status" in detail.text
    assert "Open on Kalshi" in payouts.text
    api_rows = institutional.json()["data"]
    api_row = next(row for row in api_rows if row["ticker"] == verified.ticker)
    blocked_row = next(row for row in api_rows if row["ticker"] == "KXP3AO-UI-NOURL")
    assert api_row["kalshi_url_verified"] is True
    assert api_row["kalshi_url_status"] == VERIFIED
    assert api_row["kalshi_url"].startswith("https://kalshi.com/markets/")
    assert api_row["market_identity"]["tradeable"] is True
    assert blocked_row["kalshi_url_status"] == BUILT_FROM_EXACT_CATALOG
    assert blocked_row["kalshi_url_verified"] is False
    assert blocked_row["kalshi_url"].startswith("https://kalshi.com/markets/")
    assert blocked_row["diagnostic_only"] is True


def test_phase3ao_opportunity_link_audit_artifacts_and_cli_are_read_only(tmp_path) -> None:
    db_url = f"sqlite:///{Path(tmp_path) / 'phase3ao_cli.db'}"
    engine = init_db(db_url)
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        _seed_opportunity(session, "KXP3AO-AUDIT-YES", url=True)
        _seed_opportunity(session, "KXP3AO-AUDIT-NOURL", url=False)
        artifacts = write_opportunity_link_audit(
            session,
            output_dir=Path(tmp_path) / "direct",
            limit=20,
            settings=_settings(tmp_path),
        )
        payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))

    assert artifacts.markdown_path.exists()
    assert artifacts.broken_links_csv_path.exists()
    assert artifacts.manifest_path.exists()
    assert payload["summary"]["total_opportunities_scanned"] == 2
    assert payload["summary"]["verified_urls"] == 1
    assert payload["summary"]["ui_visible_opportunities_without_clickable_verified_url"] == 0
    assert payload["safety"]["live_execution_enabled"] is False
    assert payload["safety"]["paper_trade_creation_enabled"] is False

    output_dir = Path(tmp_path) / "cli"
    get_settings.cache_clear()
    result = CliRunner().invoke(
        app,
        ["opportunity-link-audit", "--output-dir", str(output_dir), "--limit", "20"],
        env={"DATABASE_URL": db_url, "KALSHI_DB_URL": db_url},
    )
    get_settings.cache_clear()

    assert result.exit_code == 0, result.output
    assert "Mode: PAPER / READ ONLY" in result.output
    assert "Order submission/cancel/replace: blocked" in result.output
    assert output_dir.joinpath("opportunity_link_audit.json").exists()


def _session_factory(tmp_path):
    engine = init_db(f"sqlite:///{Path(tmp_path) / 'phase3ao.db'}")
    return get_session_factory(engine)


def _settings(tmp_path, *, stale_after_seconds: int = 3600) -> Settings:
    return Settings(
        kalshi_db_url=f"sqlite:///{Path(tmp_path) / 'phase3ao.db'}",
        execution_enabled=False,
        execution_dry_run=True,
        ui_read_only=True,
        phase_3x_professional_ux_enabled=True,
        phase_3x_mode="preview",
        phase_3t_institutional_dashboard_enabled=True,
        phase_3t_mode="read_only_shadow",
        phase_3t_stale_after_seconds=stale_after_seconds,
    )


def _seed_opportunity(
    session,
    ticker: str,
    *,
    url: bool,
    title: str | None = None,
    series_ticker: str = "KXP3AO",
    status: str = "open",
    market_raw: dict | None = None,
):
    event_ticker = ticker
    market_payload = {
        "ticker": ticker,
        "event_ticker": event_ticker,
        "series_ticker": series_ticker,
        "title": title or f"Will {ticker} finish above the test threshold?",
        "subtitle": "Phase 3AO fixture",
        "status": status,
        "event_title": "Phase 3AO fixture event",
        "parser_version": "phase3ao-test",
        "linker_version": "phase3ao-test",
        **(market_raw or {}),
    }
    if url:
        market_payload["event_slug"] = "phase-3ao-fixture-event"
        market_payload["series_slug"] = series_ticker.lower()
        market_payload["kalshi_url"] = (
            f"https://kalshi.com/markets/{series_ticker.lower()}/"
            f"phase-3ao-fixture-event/{event_ticker.lower()}"
        )
    upsert_market(session, market_payload)
    ranking = insert_market_ranking(
        session,
        {
            "ticker": ticker,
            "ranked_at": utc_now(),
            "title": market_payload["title"],
            "status": status,
            "series_ticker": series_ticker,
            "event_ticker": event_ticker,
            "liquidity": "1200",
            "spread": "0.01",
            "midpoint": "0.42",
            "time_to_close_minutes": "120",
            "forecast_model": "ensemble_v2",
            "forecast_probability": "0.58",
            "best_side": "BUY_YES",
            "best_price": "0.42",
            "estimated_edge": "0.16",
            "liquidity_score": "95",
            "spread_score": "98",
            "time_score": "90",
            "model_confidence_score": "90",
            "opportunity_score": "85",
            "reason": "phase3ao fixture",
            "raw_json": {"source_lineage": "phase3ao_test", **(market_raw or {})},
        },
    )
    session.flush()
    return ranking
