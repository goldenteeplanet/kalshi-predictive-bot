from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from kalshi_predictor.cli import app
from kalshi_predictor.config import get_settings
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.repositories import upsert_market
from kalshi_predictor.data.schema import MarketLeg, NewsItem, NewsMarketLink
from kalshi_predictor.phase3bb_r7_news_event import (
    build_phase3bb_r7_news_event_discovery,
    write_phase3bb_r7_news_event_discovery_report,
)
from kalshi_predictor.utils.time import utc_now


def test_phase3bb_r7_marks_official_linked_row_parse_ready_but_forecast_blocked(
    tmp_path,
) -> None:
    session_factory = _session_factory(tmp_path)

    with session_factory() as session:
        ticker = "KXFDA-APPROVAL"
        _seed_market(session, ticker=ticker, title="Will the FDA approve Drug X?")
        _add_news_leg(session, ticker=ticker, text="yes FDA approve Drug X")
        item = _add_news_item(
            session,
            source_url="https://www.fda.gov/news-events/press-announcements/drug-x",
            title="FDA Drug X approval announcement",
        )
        _add_news_link(session, ticker=ticker, news_item_id=int(item.id))
        payload = build_phase3bb_r7_news_event_discovery(session, limit=100)

    rows = {row["ticker"]: row for row in payload["news_event_candidates"]}
    row = rows[ticker]
    assert row["source_status"] == "OFFICIAL_SOURCE_BACKED"
    assert row["candidate_bucket"] == "PARSE_READY"
    assert row["parse_ready"] is True
    assert row["source_ready"] is True
    assert row["forecast_allowed_by_this_phase"] is False
    assert row["first_blocker"] == "DISCOVERY_ONLY_FORECASTS_BLOCKED"
    assert payload["summary"]["parse_ready"] == 1
    assert payload["summary"]["forecasts_created"] == 0
    assert payload["summary"]["paper_trades_created"] == 0
    assert payload["safety_flags"]["headline_only_forecasting"] is False
    assert payload["safety_flags"]["uses_fuzzy_event_matching"] is False


def test_phase3bb_r7_keeps_headline_only_ambiguous_row_review_gated(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)

    with session_factory() as session:
        ticker = "KXNEWS-SANCTIONS"
        _seed_market(
            session,
            ticker=ticker,
            title="Will the White House announce new oil sanctions?",
        )
        payload = build_phase3bb_r7_news_event_discovery(session, limit=100)

    rows = {row["ticker"]: row for row in payload["news_event_candidates"]}
    row = rows[ticker]
    assert row["source_status"] == "NO_SOURCE_FOUND"
    assert row["candidate_bucket"] == "AMBIGUOUS"
    assert row["ambiguous"] is True
    assert row["needs_review"] is True
    assert row["parse_ready"] is False
    assert row["first_blocker"] == "AMBIGUOUS_EVENT_SOURCE_MAPPING"
    assert "manual official-source family split" in row["next_parser_source_work"]
    assert payload["summary"]["no_source_found"] == 1
    assert payload["summary"]["db_writes_performed"] == 0


def test_phase3bb_r7_writes_requested_artifacts(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = Path(tmp_path) / "reports"

    with session_factory() as session:
        _seed_market(
            session,
            ticker="KXCOURT-RULING",
            title="Will the Supreme Court issue a ruling?",
        )
        artifacts = write_phase3bb_r7_news_event_discovery_report(
            session,
            output_dir=reports_dir / "phase3bb_r7",
            reports_dir=reports_dir,
            limit=100,
        )

    assert artifacts.executive_summary_path.exists()
    assert artifacts.markdown_path.exists()
    assert artifacts.candidates_csv_path.exists()
    assert artifacts.source_backlog_csv_path.exists()
    assert artifacts.manifest_path.exists()
    assert "Supreme Court" in artifacts.candidates_csv_path.read_text(encoding="utf-8")
    assert "DB fingerprint" in artifacts.executive_summary_path.read_text(encoding="utf-8")


def test_phase3bb_r7_cli_help_registered() -> None:
    result = CliRunner().invoke(app, ["phase3bb-r7-news-event-discovery", "--help"])

    assert result.exit_code == 0
    assert "phase3bb-r7-news-event-discovery" in result.output
    assert "--limit" in result.output


def _session_factory(tmp_path: Path):
    get_settings.cache_clear()
    engine = init_db(f"sqlite:///{Path(tmp_path) / 'phase3bb_r7.db'}")
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
        },
    )
    session.flush()


def _add_news_leg(session, *, ticker: str, text: str) -> None:
    session.add(
        MarketLeg(
            ticker=ticker,
            leg_index=0,
            parsed_at=utc_now(),
            side="YES",
            category="news",
            market_type="EVENT",
            entity_name="fda",
            operator="EQUALS",
            threshold_value="approved",
            unit="EVENT",
            confidence="0.95",
            raw_text=text,
            reason="test news leg",
            raw_json=json.dumps({"phase": "3bb-r7-test"}),
        )
    )


def _add_news_item(session, *, source_url: str, title: str) -> NewsItem:
    now = utc_now()
    item = NewsItem(
        source="official",
        source_url=source_url,
        published_at=now,
        ingested_at=now,
        title=title,
        summary=title,
        body=title,
        author=None,
        category="regulatory",
        entities_json=json.dumps(["FDA"]),
        sentiment_score="0",
        importance_score="0.8",
        freshness_score="0.9",
        raw_json=json.dumps({"source": "test"}),
    )
    session.add(item)
    session.flush()
    return item


def _add_news_link(session, *, ticker: str, news_item_id: int) -> None:
    session.add(
        NewsMarketLink(
            created_at=utc_now(),
            news_item_id=news_item_id,
            ticker=ticker,
            link_confidence="0.95",
            link_reason="exact official FDA source link",
            matched_terms_json=json.dumps(["FDA", "Drug X"]),
            raw_json=json.dumps({"source": "official"}),
        )
    )
    session.flush()
