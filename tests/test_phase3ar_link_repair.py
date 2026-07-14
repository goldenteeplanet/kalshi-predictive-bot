from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import func, select
from typer.testing import CliRunner

from kalshi_predictor.cli import app
from kalshi_predictor.config import Settings
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.repositories import decode_json, encode_json
from kalshi_predictor.data.schema import (
    AdvancedRiskDecisionLog,
    Forecast,
    Market,
    MarketRanking,
    MarketSnapshot,
    PaperOrder,
    PositionSizingDecisionLog,
)
from kalshi_predictor.opportunities.market_identity import (
    BUILT_FROM_EXACT_CATALOG,
    CATALOG_MATCH_MISSING,
    CATALOG_STALE,
    COMPOSITE_LOCAL_ONLY,
    MISSING_MARKET_TICKER,
    SYNTHETIC_ONLY,
    TICKER_MISMATCH,
    VERIFIED,
    build_canonical_kalshi_url,
)
from kalshi_predictor.paper.models import BUY_YES
from kalshi_predictor.phase3ap import build_phase3ap_paper_ready_gate
from kalshi_predictor import phase3ar as phase3ar_module
from kalshi_predictor.kalshi.client import RATE_LIMITED_RETRY_EXHAUSTED, KalshiRetryError
from kalshi_predictor.phase3ar import (
    build_phase3ar_catalog_stale_diagnostic,
    build_phase3ar_refresh_catalog_for_opportunities,
    build_phase3ar_refresh_books_for_verified_links,
    build_phase3ar_url_audit,
    build_phase3ar_url_repair,
    write_phase3ar_link_repair_report,
)
from kalshi_predictor.ui.service import _extend_phase3ar_blockers, _phase3ar_positive_ev_rows_for_ui
from kalshi_predictor.utils.time import utc_now


def test_phase3ar_url_builder_exact_identity_and_rejection_rules(tmp_path) -> None:
    settings = _settings(tmp_path, stale_after_seconds=60)
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        fresh = _seed_ranked_market(session, ticker="KXP3AR-BUILT", include_url_fields=False)
        mismatch = _seed_ranked_market(
            session,
            ticker="KXP3AR-MISMATCH",
            include_url_fields=True,
            stored_url="https://kalshi.com/markets/kxp3ar/sibling/kxp3ar-other-event",
        )
        synthetic = _seed_ranked_market(
            session,
            ticker="KXP3AR-SYNTH",
            include_url_fields=False,
            market_raw={"synthetic_only": True},
        )
        composite = _seed_ranked_market(
            session,
            ticker="KXMVECROSSCATEGORY-P3AR",
            include_url_fields=False,
        )
        stale = _seed_ranked_market(session, ticker="KXP3AR-STALE", include_url_fields=False)
        stale_market = session.get(Market, stale.ticker)
        stale_market.last_seen_at = utc_now() - timedelta(minutes=30)
        session.flush()

        built = build_canonical_kalshi_url(
            market=session.get(Market, fresh.ticker),
            settings=settings,
            allow_deterministic_slug=True,
        )
        stale_result = build_canonical_kalshi_url(
            market=stale_market,
            settings=settings,
            allow_deterministic_slug=True,
            allow_stale_proposal=True,
        )

        assert built.kalshi_url_status == BUILT_FROM_EXACT_CATALOG
        assert built.kalshi_url is not None
        assert fresh.event_ticker.lower() in built.kalshi_url
        assert build_canonical_kalshi_url(market=None, market_ticker="", settings=settings).kalshi_url_status == MISSING_MARKET_TICKER
        assert (
            build_canonical_kalshi_url(
                market=None,
                market_ticker="KXP3AR-TITLEONLY",
                market_title="Will title-only matching be rejected?",
                settings=settings,
                allow_deterministic_slug=True,
            ).kalshi_url_status
            == CATALOG_MATCH_MISSING
        )
        assert build_canonical_kalshi_url(market=session.get(Market, mismatch.ticker), settings=settings).kalshi_url_status == TICKER_MISMATCH
        assert build_canonical_kalshi_url(market=session.get(Market, synthetic.ticker), settings=settings).kalshi_url_status == SYNTHETIC_ONLY
        assert build_canonical_kalshi_url(market=session.get(Market, composite.ticker), settings=settings).kalshi_url_status == COMPOSITE_LOCAL_ONLY
        assert stale_result.kalshi_url_status == CATALOG_STALE
        assert stale_result.kalshi_url is not None


def test_phase3ar_url_audit_splits_malformed_reasons_and_keeps_stale_proposals(tmp_path) -> None:
    settings = _settings(tmp_path, stale_after_seconds=60)
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        fresh = _seed_ranked_market(session, ticker="KXP3AR-AUDIT", include_url_fields=False)
        stale = _seed_ranked_market(session, ticker="KXP3AR-AUDIT-STALE", include_url_fields=False)
        stale_market = session.get(Market, stale.ticker)
        stale_market.last_seen_at = utc_now() - timedelta(minutes=30)
        session.flush()

        payload = build_phase3ar_url_audit(
            session,
            output_dir=Path(tmp_path) / "phase3ar",
            reports_dir=Path(tmp_path),
            settings=settings,
        )

    rows = {row["market_ticker"]: row for row in payload["rows"]}
    assert payload["summary"]["positive_ev_rows"] == 2
    assert rows[fresh.ticker]["specific_malformed_reason"] == "URL_MISSING"
    assert rows[fresh.ticker]["safe_to_persist"] is True
    assert rows[stale.ticker]["specific_malformed_reason"] == "STALE_CATALOG"
    assert rows[stale.ticker]["safe_to_persist"] is False
    assert rows[stale.ticker]["proposed_official_url"] is not None
    assert payload["summary"]["specific_malformed_reason_counts"]["URL_MISSING"] == 1
    assert payload["summary"]["specific_malformed_reason_counts"]["STALE_CATALOG"] == 1


def test_phase3ar_catalog_stale_diagnostic_and_refresh_safety(tmp_path, monkeypatch) -> None:
    settings = _settings(tmp_path, stale_after_seconds=60)
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        stale = _seed_ranked_market(session, ticker="KXP3AR-R2-STALE", include_url_fields=False)
        stale_market = session.get(Market, stale.ticker)
        stale_market.last_seen_at = utc_now() - timedelta(minutes=30)
        before_last_seen = stale_market.last_seen_at
        before_raw = decode_json(stale_market.raw_json)
        before_orders = session.scalar(select(func.count()).select_from(PaperOrder))
        session.flush()

        diagnostic = build_phase3ar_catalog_stale_diagnostic(
            session,
            output_dir=Path(tmp_path) / "phase3ar",
            reports_dir=Path(tmp_path),
            settings=settings,
        )
        dry_run = build_phase3ar_refresh_catalog_for_opportunities(
            session,
            output_dir=Path(tmp_path) / "phase3ar",
            reports_dir=Path(tmp_path),
            settings=settings,
        )
        after_dry_run = session.get(Market, stale.ticker)
        after_dry_run_last_seen = after_dry_run.last_seen_at
        after_dry_run_raw = decode_json(after_dry_run.raw_json)

        monkeypatch.setattr(
            phase3ar_module,
            "db_writer_monitor",
            lambda settings: {"status": "ACTIVE_WRITER", "safe_to_start_write": False},
        )
        blocked = build_phase3ar_refresh_catalog_for_opportunities(
            session,
            output_dir=Path(tmp_path) / "phase3ar",
            reports_dir=Path(tmp_path),
            apply_readonly_refresh=True,
            settings=settings,
            client=_FakeCatalogClient(),
        )
        after_blocked_orders = session.scalar(select(func.count()).select_from(PaperOrder))

    row = diagnostic["rows"][0]
    assert diagnostic["summary"]["stale_catalog_rows"] == 1
    assert row["stale_reason"] == "CATALOG_LAST_SEEN_TOO_OLD"
    assert row["exact_market_exists_in_active_catalog"] is True
    assert dry_run["status"] == "DRY_RUN"
    assert dry_run["catalog_metadata_writes"] is False
    assert dry_run["summary"]["exact_positive_ev_tickers"] == 1
    assert dry_run["summary"]["exact_ticker_not_refreshed_rows"] == 1
    assert dry_run["freshness_views"]["exact_opportunity_catalog"]["status"] == "EXACT_TICKER_NOT_REFRESHED"
    assert dry_run["exact_catalog_handoff_rows"][0]["refresh_status"] == "EXACT_TICKER_NOT_REFRESHED"
    assert dry_run["exact_catalog_handoff_rows"][0]["catalog_freshness_reason"] == "CATALOG_LAST_SEEN_TOO_OLD"
    assert after_dry_run_last_seen == before_last_seen
    assert after_dry_run_raw == before_raw
    assert blocked["status"] == "BLOCKED_BY_ACTIVE_WRITER"
    assert blocked["catalog_metadata_writes"] is False
    assert after_blocked_orders == before_orders


def test_phase3ar_excludes_expired_crypto_window_before_catalog_stale(tmp_path) -> None:
    settings = _settings(tmp_path, stale_after_seconds=60)
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        expired = _seed_ranked_market(
            session,
            ticker="KXBTC-26JUL0809-B61750",
            include_url_fields=False,
            close_delta=timedelta(hours=-4),
            expected_expiration_delta=timedelta(hours=-3, minutes=-55),
            last_seen_delta=timedelta(hours=5),
        )
        before_orders = session.scalar(select(func.count()).select_from(PaperOrder))

        gate = build_phase3ap_paper_ready_gate(session, settings=settings)
        audit = build_phase3ar_url_audit(
            session,
            output_dir=Path(tmp_path) / "phase3ar",
            reports_dir=Path(tmp_path),
            settings=settings,
        )
        catalog = build_phase3ar_catalog_stale_diagnostic(
            session,
            output_dir=Path(tmp_path) / "phase3ar",
            reports_dir=Path(tmp_path),
            settings=settings,
        )
        after_orders = session.scalar(select(func.count()).select_from(PaperOrder))

    row = next(row for row in gate["rows"] if row["market_ticker"] == expired.ticker)
    assert row["primary_blocker"] == "EXPIRED_WINDOW_EXCLUDED"
    assert row["diagnostic_only"] is True
    assert gate["summary"]["positive_ev_rows"] == 0
    assert gate["summary"]["current_positive_ev_rows"] == 0
    assert gate["summary"]["expired_positive_ev_rows"] == 1
    assert gate["summary"]["expired_excluded_rows"] == 1
    assert gate["summary"]["stale_catalog_rows"] == 0
    assert gate["summary"]["first_hard_blocker"] == "NO_CURRENT_POSITIVE_EV"
    assert audit["summary"]["positive_ev_rows"] == 0
    assert audit["summary"]["expired_positive_ev_rows"] == 1
    assert audit["rows"] == []
    assert catalog["summary"]["stale_catalog_rows"] == 0
    assert before_orders == after_orders


def test_phase3ar_current_open_market_can_still_be_stale_catalog(tmp_path) -> None:
    settings = _settings(tmp_path, stale_after_seconds=60)
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        stale = _seed_ranked_market(
            session,
            ticker="KXP3AR-R8-STALE-CURRENT",
            include_url_fields=False,
            close_delta=timedelta(hours=2),
            last_seen_delta=timedelta(minutes=30),
        )

        gate = build_phase3ap_paper_ready_gate(session, settings=settings)
        audit = build_phase3ar_url_audit(
            session,
            output_dir=Path(tmp_path) / "phase3ar",
            reports_dir=Path(tmp_path),
            settings=settings,
        )

    row = next(row for row in gate["rows"] if row["market_ticker"] == stale.ticker)
    audit_row = next(row for row in audit["rows"] if row["market_ticker"] == stale.ticker)
    assert row["primary_blocker"] == "STALE_CATALOG"
    assert row["current_positive_ev_eligible"] is True
    assert gate["summary"]["positive_ev_rows"] == 1
    assert gate["summary"]["stale_catalog_rows"] == 1
    assert audit_row["specific_malformed_reason"] == "STALE_CATALOG"


def test_phase3ar_current_fresh_catalog_with_old_snapshot_is_stale_quote(tmp_path) -> None:
    settings = _settings(tmp_path, stale_after_seconds=3600)
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        current = _seed_ranked_market(
            session,
            ticker="KXP3AR-R8-STALE-QUOTE",
            include_url_fields=True,
            close_delta=timedelta(hours=2),
            snapshot_age=timedelta(minutes=30),
        )

        gate = build_phase3ap_paper_ready_gate(session, settings=settings)

    row = next(row for row in gate["rows"] if row["market_ticker"] == current.ticker)
    assert row["primary_blocker"] == "STALE_QUOTE"
    assert row["book_freshness_state"] == "STALE_ORDERBOOK"
    assert gate["summary"]["positive_ev_rows"] == 1
    assert gate["summary"]["stale_quote_rows"] == 1
    assert gate["summary"]["paper_ready_rows"] == 0


def test_phase3ar_finalized_market_is_diagnostic_only(tmp_path) -> None:
    settings = _settings(tmp_path, stale_after_seconds=60)
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        settled = _seed_ranked_market(
            session,
            ticker="KXP3AR-R8-SETTLED",
            include_url_fields=True,
            market_status="settled",
            close_delta=timedelta(hours=-2),
            expected_expiration_delta=timedelta(hours=-1, minutes=-55),
            market_result="yes",
        )

        gate = build_phase3ap_paper_ready_gate(session, settings=settings)

    row = next(row for row in gate["rows"] if row["market_ticker"] == settled.ticker)
    assert row["primary_blocker"] == "MARKET_CLOSED_OR_SETTLED"
    assert row["diagnostic_only"] is True
    assert gate["summary"]["positive_ev_rows"] == 0
    assert gate["summary"]["finalized_or_settled_rows"] == 1


def test_phase3ar_ui_shows_expired_positive_ev_separately() -> None:
    blockers: list[dict[str, object]] = []

    _extend_phase3ar_blockers(
        blockers,
        {
            "gate_summary": {
                "positive_ev_rows": 0,
                "expired_positive_ev_rows": 2,
                "paper_ready_rows": 0,
                "first_hard_blocker": "NO_CURRENT_POSITIVE_EV",
            },
            "positive_ev_rows": [],
        },
    )

    assert blockers[0]["status"] == "EXPIRED_WINDOW_EXCLUDED"
    assert "2 expired positive-EV" in str(blockers[0]["evidence"])


def test_phase3ar_catalog_refresh_apply_requires_explicit_flag_and_writes_only_catalog(tmp_path) -> None:
    settings = _settings(tmp_path, stale_after_seconds=60)
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        stale = _seed_ranked_market(session, ticker="KXP3AR-R2-APPLY", include_url_fields=False)
        stale_market = session.get(Market, stale.ticker)
        stale_market.last_seen_at = utc_now() - timedelta(minutes=30)
        before_forecasts = session.scalar(select(func.count()).select_from(Forecast))
        before_orders = session.scalar(select(func.count()).select_from(PaperOrder))
        session.flush()

        no_apply = build_phase3ar_refresh_catalog_for_opportunities(
            session,
            output_dir=Path(tmp_path) / "phase3ar",
            reports_dir=Path(tmp_path),
            settings=settings,
            client=_FakeCatalogClient(),
        )
        old_seen = session.get(Market, stale.ticker).last_seen_at
        no_apply_seen = old_seen
        applied = build_phase3ar_refresh_catalog_for_opportunities(
            session,
            output_dir=Path(tmp_path) / "phase3ar",
            reports_dir=Path(tmp_path),
            apply_readonly_refresh=True,
            settings=settings,
            client=_FakeCatalogClient(),
        )
        refreshed = session.get(Market, stale.ticker)
        refreshed_last_seen = refreshed.last_seen_at
        refreshed_raw = decode_json(refreshed.raw_json)
        after_forecasts = session.scalar(select(func.count()).select_from(Forecast))
        after_orders = session.scalar(select(func.count()).select_from(PaperOrder))

    assert no_apply["status"] == "DRY_RUN"
    assert no_apply_seen == old_seen
    assert applied["status"] == "READONLY_REFRESH_COMPLETED"
    assert applied["summary"]["refreshed_rows"] == 1
    assert applied["summary"]["exact_catalog_fresh_rows"] == 1
    assert applied["summary"]["exact_ticker_not_refreshed_rows"] == 0
    assert applied["freshness_views"]["exact_opportunity_catalog"]["status"] == "COMPLETE"
    handoff_row = applied["exact_catalog_handoff_rows"][0]
    assert handoff_row["refresh_status"] == "REFRESHED"
    assert handoff_row["exact_catalog_fresh"] is True
    assert handoff_row["title"].startswith("Refreshed")
    assert handoff_row["event_ticker"] == f"{stale.ticker}-EVENT"
    assert handoff_row["series_ticker"] == "KXP3AR"
    assert handoff_row["url_verification_status"] == "MALFORMED_URL"
    assert handoff_row["proposed_url_status"] == BUILT_FROM_EXACT_CATALOG
    assert refreshed_last_seen > old_seen
    assert refreshed_raw["title"].startswith("Refreshed")
    assert after_forecasts == before_forecasts
    assert after_orders == before_orders


def test_phase3ar_catalog_refresh_reports_rate_limit_without_paper_trades(tmp_path) -> None:
    settings = _settings(tmp_path, stale_after_seconds=60)
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        stale = _seed_ranked_market(session, ticker="KXP3AR-R2-429", include_url_fields=False)
        stale_market = session.get(Market, stale.ticker)
        stale_market.last_seen_at = utc_now() - timedelta(minutes=30)
        before_orders = session.scalar(select(func.count()).select_from(PaperOrder))
        session.flush()

        payload = build_phase3ar_refresh_catalog_for_opportunities(
            session,
            output_dir=Path(tmp_path) / "phase3ar",
            reports_dir=Path(tmp_path),
            apply_readonly_refresh=True,
            settings=settings,
            client=_FakeRateLimitedCatalogClient(),
        )
        after_orders = session.scalar(select(func.count()).select_from(PaperOrder))

    assert payload["status"] == RATE_LIMITED_RETRY_EXHAUSTED
    assert payload["rate_limit"]["blocker"] == "RATE_LIMITED_KALSHI_API"
    assert payload["rate_limit"]["data_completeness"] == "partial"
    assert payload["summary"]["data_completeness"] == "partial"
    assert payload["summary"]["exact_ticker_not_refreshed_rows"] == 1
    assert payload["freshness_views"]["exact_opportunity_catalog"]["status"] == RATE_LIMITED_RETRY_EXHAUSTED
    assert payload["failed_rows"][0]["status"] == RATE_LIMITED_RETRY_EXHAUSTED
    assert payload["exact_catalog_handoff_rows"][0]["refresh_status"] == RATE_LIMITED_RETRY_EXHAUSTED
    assert payload["paper_trade_creation"] is False
    assert after_orders == before_orders


def test_phase3ar_url_repair_dry_run_apply_and_paper_gate_transition(tmp_path) -> None:
    settings = _settings(tmp_path)
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        seeded = _seed_ranked_market(
            session,
            ticker="KXP3AR-REPAIR",
            include_url_fields=False,
            orderbook={},
        )
        before_raw = decode_json(session.get(Market, seeded.ticker).raw_json)
        before_forecasts = session.scalar(select(func.count()).select_from(Forecast))
        before_orders = session.scalar(select(func.count()).select_from(PaperOrder))

        dry_run = build_phase3ar_url_repair(
            session,
            output_dir=Path(tmp_path) / "phase3ar",
            reports_dir=Path(tmp_path),
            dry_run=True,
            apply=False,
            settings=settings,
        )
        after_dry_run_raw = decode_json(session.get(Market, seeded.ticker).raw_json)
        blocked = build_phase3ar_url_repair(
            session,
            output_dir=Path(tmp_path) / "phase3ar",
            reports_dir=Path(tmp_path),
            dry_run=False,
            apply=True,
            backup_first=False,
            settings=settings,
        )
        before_gate = build_phase3ap_paper_ready_gate(session, settings=settings)
        applied = build_phase3ar_url_repair(
            session,
            output_dir=Path(tmp_path) / "phase3ar",
            reports_dir=Path(tmp_path),
            dry_run=False,
            apply=True,
            backup_first=True,
            settings=settings,
        )
        after_gate = build_phase3ap_paper_ready_gate(session, settings=settings)
        after_raw = decode_json(session.get(Market, seeded.ticker).raw_json)
        after_forecasts = session.scalar(select(func.count()).select_from(Forecast))
        after_orders = session.scalar(select(func.count()).select_from(PaperOrder))

    assert dry_run["status"] == "DRY_RUN"
    assert dry_run["summary"]["safe_to_persist"] == 1
    assert after_dry_run_raw == before_raw
    assert blocked["status"] == "BLOCKED_REQUIRES_BACKUP_FIRST"
    assert applied["status"] == "APPLIED"
    assert applied["summary"]["repaired_rows"] == 1
    assert after_raw["url_verification_status"] == VERIFIED
    assert after_raw["kalshi_url"].startswith("https://kalshi.com/markets/")
    assert before_forecasts == after_forecasts
    assert before_orders == after_orders
    assert before_gate["rows"][0]["primary_blocker"] == BUILT_FROM_EXACT_CATALOG
    assert after_gate["rows"][0]["primary_blocker"] == "EMPTY_ORDERBOOK"


def test_phase3ar_refresh_books_uses_only_verified_exact_links(tmp_path) -> None:
    settings = _settings(tmp_path)
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        verified = _seed_ranked_market(
            session,
            ticker="KXP3AR-VERIFIED-EMPTY",
            include_url_fields=True,
            orderbook={},
        )
        _seed_ranked_market(
            session,
            ticker="KXP3AR-UNVERIFIED-EMPTY",
            include_url_fields=False,
            orderbook={},
        )

        payload = build_phase3ar_refresh_books_for_verified_links(
            session,
            output_dir=Path(tmp_path) / "phase3ar",
            dry_run=True,
            apply_readonly_refresh=False,
            settings=settings,
        )

    tickers = {row["market_ticker"] for row in payload["verified_refresh_candidates"]}
    assert payload["market_data_writes"] is False
    assert verified.ticker in tickers
    assert "KXP3AR-UNVERIFIED-EMPTY" not in tickers
    assert payload["unverified_refresh_allowed"] is False


def test_phase3ar_ui_payload_and_unified_report_artifacts(tmp_path) -> None:
    settings = _settings(tmp_path)
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        verified = _seed_ranked_market(session, ticker="KXP3AR-UI-VERIFIED", include_url_fields=True)
        unverified = _seed_ranked_market(session, ticker="KXP3AR-UI-BLOCKED", include_url_fields=False)

        audit = build_phase3ar_url_audit(
            session,
            output_dir=Path(tmp_path) / "phase3ar",
            reports_dir=Path(tmp_path),
            settings=settings,
        )
        rows = _phase3ar_positive_ev_rows_for_ui({"positive_ev_rows": audit["rows"]})
        artifacts = write_phase3ar_link_repair_report(
            session,
            output_dir=Path(tmp_path) / "phase3ar",
            reports_dir=Path(tmp_path),
            settings=settings,
        )

    ui_rows = {row["market_ticker"]: row for row in rows}
    assert ui_rows[verified.ticker]["kalshi_url_verified"] is True
    assert ui_rows[verified.ticker]["kalshi_url"].startswith("https://kalshi.com/markets/")
    assert ui_rows[unverified.ticker]["kalshi_url_verified"] is False
    assert ui_rows[unverified.ticker]["malformed_reason"] == "Url Missing"
    for path in (
        artifacts.executive_summary_path,
        artifacts.next_actions_path,
        artifacts.url_audit_path,
        artifacts.url_audit_markdown_path,
        artifacts.catalog_stale_diagnostic_path,
        artifacts.catalog_stale_diagnostic_markdown_path,
        artifacts.catalog_refresh_plan_path,
        artifacts.catalog_refresh_plan_markdown_path,
        artifacts.url_repair_dry_run_path,
        artifacts.book_refresh_plan_path,
        artifacts.book_refresh_candidates_path,
        artifacts.paper_ready_gate_path,
        artifacts.blocked_positive_ev_csv_path,
        artifacts.manifest_path,
    ):
        assert path.exists()
    help_text = CliRunner().invoke(app, ["--help"]).output
    next_actions = artifacts.next_actions_path.read_text(encoding="utf-8")
    commands = set()
    for line in next_actions.splitlines():
        marker = "kalshi-bot "
        if marker not in line:
            continue
        command = line.split(marker, 1)[1].split()[0].strip("`.")
        commands.add(command)
    assert commands
    assert all(command in help_text for command in commands)


def test_phase3ar_cli_commands_are_registered() -> None:
    runner = CliRunner()
    for command in (
        "phase3ar-url-audit",
        "phase3ar-catalog-stale-diagnostic",
        "phase3ar-refresh-catalog-for-opportunities",
        "phase3ar-url-repair",
        "phase3ar-refresh-books-for-verified-links",
        "phase3ar-settlement-check-noise-audit",
        "phase3ar-link-repair-report",
    ):
        result = runner.invoke(app, [command, "--help"])
        assert result.exit_code == 0, result.output
        assert command in result.output


def _settings(tmp_path: Path, *, stale_after_seconds: int = 3600) -> Settings:
    return Settings(
        kalshi_db_url=f"sqlite:///{Path(tmp_path) / 'phase3ar.db'}",
        execution_enabled=False,
        execution_dry_run=True,
        ui_read_only=True,
        opportunity_min_edge=Decimal("0.03"),
        opportunity_min_score=Decimal("60"),
        opportunity_max_spread=Decimal("0.10"),
        opportunity_min_liquidity=Decimal("10"),
        opportunity_min_time_to_close_minutes=Decimal("10"),
        phase_3t_stale_after_seconds=stale_after_seconds,
    )


def _session_factory(tmp_path: Path):
    engine = init_db(f"sqlite:///{Path(tmp_path) / 'phase3ar.db'}")
    return get_session_factory(engine)


def _seed_ranked_market(
    session,
    *,
    ticker: str,
    include_url_fields: bool,
    orderbook: dict | None = None,
    stored_url: str | None = None,
    market_raw: dict | None = None,
    market_status: str = "open",
    market_result: str | None = None,
    close_delta: timedelta = timedelta(hours=2),
    expected_expiration_delta: timedelta | None = None,
    snapshot_age: timedelta = timedelta(minutes=1),
    last_seen_delta: timedelta = timedelta(0),
    ranking_time_to_close_minutes: str = "120",
):
    now = utc_now()
    event_ticker = f"{ticker}-EVENT"
    close_time = now + close_delta
    expected_expiration_time = (
        now + expected_expiration_delta if expected_expiration_delta is not None else None
    )
    market_payload = {
        "ticker": ticker,
        "event_ticker": event_ticker,
        "series_ticker": "KXP3AR",
        "title": f"Will {ticker} finish above the fixture threshold?",
        "subtitle": "Phase 3AR fixture",
        "status": market_status,
        "result": market_result,
        "close_time": close_time.isoformat(),
        "rules_primary": "Market resolves using the listed fixture threshold.",
        "event_title": f"{ticker} fixture event",
        **(market_raw or {}),
    }
    if expected_expiration_time is not None:
        market_payload["expected_expiration_time"] = expected_expiration_time.isoformat()
    if include_url_fields:
        market_payload.update(
            {
                "event_slug": "phase-3ar-fixture-event",
                "series_slug": "kxp3ar",
                "kalshi_url": stored_url
                or (
                    "https://kalshi.com/markets/kxp3ar/"
                    f"phase-3ar-fixture-event/{event_ticker.lower()}"
                ),
            }
        )
    market = Market(
        ticker=ticker,
        event_ticker=event_ticker,
        series_ticker="KXP3AR",
        title=market_payload["title"],
        subtitle=market_payload["subtitle"],
        status=market_status,
        result=market_result,
        close_time=close_time,
        expected_expiration_time=expected_expiration_time,
        rules_primary=market_payload["rules_primary"],
        raw_json=encode_json(market_payload),
        first_seen_at=now,
        last_seen_at=now - last_seen_delta,
    )
    session.add(market)
    forecast = Forecast(
        ticker=ticker,
        forecasted_at=now,
        model_name="phase3ar_test",
        yes_probability="0.70",
        market_mid_probability="0.50",
        best_yes_bid="0.38",
        best_yes_ask="0.40",
        feature_json=encode_json({"source": "phase3ar_test"}),
    )
    session.add(forecast)
    session.flush()
    session.add(
        MarketSnapshot(
            ticker=ticker,
            captured_at=now - snapshot_age,
            status=market_status,
            best_yes_bid="0.38",
            best_yes_ask="0.41",
            best_no_bid="0.59",
            best_no_ask="0.62",
            spread="0.02",
            raw_market_json=encode_json(market_payload),
            raw_orderbook_json=encode_json(_orderbook() if orderbook is None else orderbook),
        )
    )
    session.add(
        MarketRanking(
            ticker=ticker,
            ranked_at=now,
            title=market.title,
            status=market_status,
            series_ticker="KXP3AR",
            event_ticker=event_ticker,
            volume="100",
            open_interest="100",
            liquidity="1000",
            spread="0.02",
            midpoint="0.50",
            time_to_close_minutes=ranking_time_to_close_minutes,
            forecast_model="phase3ar_test",
            forecast_probability="0.70",
            best_side=BUY_YES,
            best_price="0.40",
            estimated_edge="0.30",
            liquidity_score="80",
            spread_score="80",
            time_score="80",
            model_confidence_score="80",
            opportunity_score="80",
            reason="phase3ar fixture",
            raw_json=encode_json({"forecast_id": forecast.id}),
        )
    )
    sizing = PositionSizingDecisionLog(
        decision_timestamp=now,
        created_at=now,
        version="test",
        mode="PAPER",
        strategy_id="phase3ar_test",
        instrument=ticker,
        ticker=ticker,
        model_name="phase3ar_test",
        trade_intent_id=f"intent-{ticker}",
        order_correlation_id=f"corr-{ticker}",
        paper_order_id=None,
        tier="standard",
        composite_score="1.0",
        proposed_contracts=3,
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
            action="ALLOW",
            strategy_id="phase3ar_test",
            model_id="phase3ar_test",
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
            phase_3m_proposed_contracts=3,
            live_candidate_contracts=0,
            executed_contracts=3,
            risk_per_contract="1.0",
            planned_trade_risk="3",
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
    return market


def _orderbook(*, yes_bid: str = "0.38", no_bid: str = "0.59") -> dict:
    return {
        "orderbook_fp": {
            "yes_dollars": [[yes_bid, "4"]],
            "no_dollars": [[no_bid, "4"]],
        }
    }


class _FakeCatalogClient:
    def get_market(self, ticker: str) -> dict:
        return {
            "ticker": ticker,
            "event_ticker": f"{ticker}-EVENT",
            "series_ticker": "KXP3AR",
            "title": f"Refreshed {ticker} fixture market",
            "subtitle": "Phase 3AR refreshed fixture",
            "status": "open",
            "rules_primary": "Market resolves using the refreshed fixture threshold.",
            "event_title": f"Refreshed {ticker} fixture event",
            "event_slug": "refreshed-phase-3ar-fixture-event",
            "series_slug": "kxp3ar",
        }

    def get_orderbook(self, ticker: str) -> dict:
        return {}


class _FakeRateLimitedCatalogClient:
    def __init__(self) -> None:
        self.telemetry = SimpleNamespace(
            as_dict=lambda rows_fetched_before_limit=0: {
                "status": RATE_LIMITED_RETRY_EXHAUSTED,
                "rate_limited": True,
                "request_count": 3,
                "retry_count": 3,
                "rate_limited_count": 3,
                "retry_exhausted_count": 1,
                "total_sleep_seconds": 7.0,
                "rows_fetched_before_limit": rows_fetched_before_limit,
                "data_completeness": "partial",
                "endpoints": [
                    {
                        "endpoint": "GET /markets/KXP3AR-R2-429",
                        "status_code": 429,
                        "retry_count": 3,
                        "total_sleep_seconds": 7.0,
                        "retry_exhausted": True,
                    }
                ],
                "events": [],
            }
        )

    def get_market(self, ticker: str) -> dict:
        raise KalshiRetryError(f"Kalshi GET /markets/{ticker} failed after retry budget: HTTP 429")

    def get_orderbook(self, ticker: str) -> dict:
        return {}
