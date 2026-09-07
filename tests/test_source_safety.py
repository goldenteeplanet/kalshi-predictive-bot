import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import func, select

from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.schema import NewsItem
from kalshi_predictor.news.ingestion import ingest_news_items
from kalshi_predictor.news.providers import parse_rss_feed
from kalshi_predictor.news.repository import upsert_news_item
from kalshi_predictor.source_safety import (
    canonicalize_source_url,
    validate_official_economic_evidence,
)

FIXTURES = Path(__file__).parent / "fixtures" / "source_safety"


def test_canonicalize_source_url_removes_tracking_and_fragment() -> None:
    left = canonicalize_source_url(
        "HTTPS://Example.COM/story/?utm_source=rss&topic=fed&gclid=abc#latest"
    )
    right = canonicalize_source_url("https://example.com/story?topic=fed")

    assert left == right == "https://example.com/story?topic=fed"


def test_official_economic_fixture_preserves_point_in_time_evidence() -> None:
    payload = json.loads((FIXTURES / "economic_bls_valid.json").read_text())

    evidence = validate_official_economic_evidence(payload)

    assert evidence["official_source"] is True
    assert evidence["source_url"] == "https://api.bls.gov/publicAPI/v2/timeseries/data"
    assert evidence["available_at"] == "2026-07-14T12:30:03+00:00"


def test_economic_evidence_rejects_nonofficial_and_missing_publication_time() -> None:
    base = {
        "source_url": "https://example.com/cpi",
        "published_at": "2026-07-14T12:30:00Z",
        "available_at": "2026-07-14T12:30:03Z",
        "ingested_at": "2026-07-14T12:30:05Z",
    }
    with pytest.raises(ValueError, match="not official"):
        validate_official_economic_evidence(base)

    base["source_url"] = "https://api.bls.gov/publicAPI/v2/timeseries/data/"
    base.pop("published_at")
    base["event_time"] = "2026-07-01T00:00:00Z"
    with pytest.raises(ValueError, match="published_at is required"):
        validate_official_economic_evidence(base)


def test_economic_fixture_rejects_evidence_unavailable_at_decision() -> None:
    payload = json.loads((FIXTURES / "economic_future_available.json").read_text())

    with pytest.raises(ValueError, match="not available at the decision"):
        validate_official_economic_evidence(payload)


def test_rss_fixture_is_canonicalized_and_timestamped() -> None:
    xml = (FIXTURES / "news_rss_valid.xml").read_text()
    fetched_at = datetime(2026, 7, 22, 18, 0, 3, tzinfo=UTC)

    items = parse_rss_feed(
        xml,
        source_name="federal_reserve",
        source_url="https://www.federalreserve.gov/feeds/press_all.xml",
        fetched_at=fetched_at,
    )

    assert len(items) == 1
    assert items[0]["source_url"].endswith("monetary20260722a.htm")
    assert items[0]["feed_url"] == "https://www.federalreserve.gov/feeds/press_all.xml"
    assert items[0]["available_at"] == "2026-07-22T18:00:03+00:00"
    assert len(items[0]["source_identity"]) == 64


def test_rss_fixture_rejects_future_and_insecure_feed_evidence() -> None:
    future_xml = (FIXTURES / "news_rss_future.xml").read_text()
    fetched_at = datetime(2026, 7, 23, 12, 30, tzinfo=UTC)

    with pytest.raises(ValueError, match="published_at is after available_at"):
        parse_rss_feed(
            future_xml,
            source_name="fixture",
            source_url="https://www.bls.gov/feed.xml",
            fetched_at=fetched_at,
        )

    valid_xml = (FIXTURES / "news_rss_valid.xml").read_text()
    with pytest.raises(ValueError, match="must use HTTPS"):
        parse_rss_feed(
            valid_xml,
            source_name="fixture",
            source_url="http://www.federalreserve.gov/feed.xml",
            fetched_at=datetime(2026, 7, 22, 18, 0, 3, tzinfo=UTC),
        )


def test_repository_deduplicates_canonical_url_variants(tmp_path: Path) -> None:
    session_factory = get_session_factory(init_db(f"sqlite:///{tmp_path / 'news.db'}"))
    base = {
        "source": "federal_reserve",
        "title": "Federal Reserve publishes a policy statement",
        "published_at": "2026-07-22T18:00:00Z",
        "available_at": "2026-07-22T18:00:03Z",
        "ingested_at": "2026-07-22T18:00:05Z",
    }

    with session_factory() as session:
        _, first_created = upsert_news_item(
            session,
            {
                **base,
                "source_url": "https://www.federalreserve.gov/statement?utm_source=rss",
            },
        )
        _, second_created = upsert_news_item(
            session,
            {
                **base,
                "source_url": "https://www.federalreserve.gov/statement#latest",
            },
        )
        session.commit()
        count = session.scalar(select(func.count(NewsItem.id)))

    assert first_created is True
    assert second_created is False
    assert count == 1


def test_invalid_future_news_is_reported_without_a_database_write(tmp_path: Path) -> None:
    session_factory = get_session_factory(init_db(f"sqlite:///{tmp_path / 'news.db'}"))
    item = {
        "source": "bls",
        "source_url": "https://www.bls.gov/news.release/future.htm",
        "title": "Future release",
        "published_at": "2026-07-24T12:30:00Z",
        "available_at": "2026-07-23T12:30:00Z",
        "ingested_at": "2026-07-23T12:30:01Z",
    }

    with session_factory() as session:
        summary = ingest_news_items(session, [item], source="rss")
        session.commit()
        count = session.scalar(select(func.count(NewsItem.id)))

    assert summary.items_inserted == 0
    assert summary.errors == ["Future release: published_at is after available_at"]
    assert count == 0
