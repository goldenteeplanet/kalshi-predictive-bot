from decimal import Decimal
from pathlib import Path

from sqlalchemy import func, select
from typer.testing import CliRunner

from kalshi_predictor.cli import app
from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.repositories import upsert_market
from kalshi_predictor.data.schema import SportsMarketLink
from kalshi_predictor.sports.link_cleanup import (
    build_sports_link_cleanup,
    write_sports_link_cleanup_report,
)
from kalshi_predictor.sports.repository import insert_sports_market_link
from kalshi_predictor.utils.time import utc_now


def test_sports_link_cleanup_dry_run_identifies_noisy_fanout(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed_noisy_direct_links(session, "KXSPORT-NOISY", count=5)
        _seed_noisy_direct_links(session, "KXSPORT-SMALL", count=2)
        _seed_verified_link(session, "KXSPORT-NOISY")

        payload = build_sports_link_cleanup(
            session,
            settings=Settings(sports_max_direct_links_per_market=3),
            apply=False,
        )
        link_count = session.scalar(select(func.count(SportsMarketLink.id)))

    assert payload["dry_run"] is True
    assert payload["summary"]["noisy_tickers"] == 1
    assert payload["summary"]["noisy_rows_eligible_for_cleanup"] == 5
    assert payload["summary"]["rows_deleted"] == 0
    assert link_count == 8


def test_sports_link_cleanup_apply_deletes_only_legacy_noisy_rows(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed_noisy_direct_links(session, "KXSPORT-NOISY", count=5)
        _seed_noisy_direct_links(session, "KXSPORT-SMALL", count=2)
        _seed_verified_link(session, "KXSPORT-NOISY")
        _seed_derived_link(session, "KXSPORT-NOISY")
        _seed_market_derived_link(session, "KXSPORT-NOISY")
        _seed_quarantine_link(session, "KXSPORT-NOISY")

        payload = build_sports_link_cleanup(
            session,
            settings=Settings(sports_max_direct_links_per_market=3),
            apply=True,
        )
        remaining = list(
            session.scalars(select(SportsMarketLink).order_by(SportsMarketLink.ticker))
        )

    assert payload["dry_run"] is False
    assert payload["summary"]["rows_deleted"] == 5
    assert {link.game_key for link in remaining} == {
        "MLB:derived",
        "MLB:market-derived:kxsport-noisy",
        "MLB:quarantine",
        "MLB:small-0",
        "MLB:small-1",
        "MLB:verified",
    }


def test_sports_link_cleanup_targets_derived_fanout_not_base_derived(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed_derived_fanout_links(session, "KXSPORT-DERIVED-FANOUT", count=4)
        _seed_base_derived_link(session, "KXSPORT-DERIVED-FANOUT")

        payload = build_sports_link_cleanup(
            session,
            settings=Settings(sports_max_direct_links_per_market=3),
            apply=True,
        )
        remaining = list(session.scalars(select(SportsMarketLink)))

    assert payload["summary"]["rows_deleted"] == 4
    assert [link.game_key for link in remaining] == [
        "MLB:kalshi-event-derived:kxsport-derived-fanout"
    ]
    assert "Kalshi-event-derived sports link" in remaining[0].link_reason


def test_sports_link_cleanup_report_and_cli_help(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed_noisy_direct_links(session, "KXSPORT-REPORT", count=4)
        artifacts = write_sports_link_cleanup_report(
            session,
            output_path=Path(tmp_path) / "sports_link_cleanup.md",
            settings=Settings(sports_max_direct_links_per_market=2),
        )

    report = artifacts.output_path.read_text(encoding="utf-8")
    assert "Sports Link Cleanup" in report
    assert "noisy_rows_eligible_for_cleanup: 4" in report
    assert artifacts.json_path.exists()
    assert artifacts.rows_path.exists()

    result = CliRunner().invoke(app, ["sports-link-cleanup", "--help"])
    assert result.exit_code == 0
    assert "--apply" in result.output


def test_sports_link_cleanup_cli_writes_empty_report(tmp_path) -> None:
    get_settings.cache_clear()
    runner = CliRunner()
    db_url = f"sqlite:///{Path(tmp_path) / 'sports_cleanup_cli.db'}"
    output = Path(tmp_path) / "sports_link_cleanup.md"

    result = runner.invoke(
        app,
        ["sports-link-cleanup", "--output", str(output)],
        env={"KALSHI_DB_URL": db_url},
    )

    get_settings.cache_clear()
    assert result.exit_code == 0
    assert output.exists()
    assert "PAPER ONLY" in result.output


def _session_factory(tmp_path):
    engine = init_db(f"sqlite:///{Path(tmp_path) / 'sports_cleanup.db'}")
    return get_session_factory(engine)


def _seed_noisy_direct_links(session, ticker: str, *, count: int) -> None:
    _seed_market(session, ticker)
    label = "small" if "SMALL" in ticker else "noisy"
    for index in range(count):
        insert_sports_market_link(
            session,
            ticker=ticker,
            league="MLB",
            game_key=f"MLB:{label}-{index}",
            market_type="MONEYLINE",
            link_confidence=Decimal("0.60"),
            link_reason="MLB market matched 3 game term(s).",
            matched_terms=["mlb", "game_time", "moneyline"],
            raw_json={"market_ticker": ticker},
        )
    session.flush()


def _seed_verified_link(session, ticker: str) -> None:
    _seed_market(session, ticker)
    insert_sports_market_link(
        session,
        ticker=ticker,
        league="MLB",
        game_key="MLB:verified",
        market_type="MONEYLINE",
        link_confidence=Decimal("0.95"),
        link_reason="Verified schedule team match.",
        matched_terms=["verified"],
        raw_json={"source": "verified_schedule"},
    )


def _seed_derived_link(session, ticker: str) -> None:
    _seed_market(session, ticker)
    insert_sports_market_link(
        session,
        ticker=ticker,
        league="MLB",
        game_key="MLB:derived",
        market_type="MONEYLINE",
        link_confidence=Decimal("0.80"),
        link_reason="Kalshi event derived sports link.",
        matched_terms=["derived"],
        raw_json={"source": "kalshi_event_derived"},
    )


def _seed_base_derived_link(session, ticker: str) -> None:
    _seed_market(session, ticker)
    insert_sports_market_link(
        session,
        ticker=ticker,
        league="MLB",
        game_key="MLB:kalshi-event-derived:kxsport-derived-fanout",
        market_type="MONEYLINE",
        link_confidence=Decimal("0.70"),
        link_reason="Kalshi-event-derived sports link built from parsed market legs.",
        matched_terms=["derived"],
        raw_json={"source": "KALSHI_EVENT_DERIVED"},
    )


def _seed_derived_fanout_links(session, ticker: str, *, count: int) -> None:
    _seed_market(session, ticker)
    for index in range(count):
        insert_sports_market_link(
            session,
            ticker=ticker,
            league="MLB",
            game_key=f"MLB:kalshi-event-derived:other-game-{index}",
            market_type="MONEYLINE",
            link_confidence=Decimal("0.60"),
            link_reason="MLB market matched 3 game term(s).",
            matched_terms=["mlb", "game_time", "moneyline"],
            raw_json={"market_ticker": ticker},
        )
    session.flush()


def _seed_market_derived_link(session, ticker: str) -> None:
    _seed_market(session, ticker)
    insert_sports_market_link(
        session,
        ticker=ticker,
        league="MLB",
        game_key="MLB:market-derived:kxsport-noisy",
        market_type="MONEYLINE",
        link_confidence=Decimal("0.40"),
        link_reason="Market-derived fallback sports link.",
        matched_terms=["market-derived"],
        raw_json={"source": "market-derived-fallback"},
    )


def _seed_quarantine_link(session, ticker: str) -> None:
    _seed_market(session, ticker)
    insert_sports_market_link(
        session,
        ticker=ticker,
        league="MLB",
        game_key="MLB:quarantine",
        market_type="MONEYLINE",
        link_confidence=Decimal("0.40"),
        link_reason="Broad sports match rejected: market matched 9 games.",
        matched_terms=["quarantine"],
        raw_json={"source": "broad-match-quarantine"},
    )


def _seed_market(session, ticker: str) -> None:
    upsert_market(
        session,
        {
            "ticker": ticker,
            "title": f"{ticker} sports market",
            "series_ticker": "KXMLB",
            "event_ticker": f"{ticker}-EVENT",
            "status": "open",
            "close_time": utc_now().isoformat(),
        },
    )
