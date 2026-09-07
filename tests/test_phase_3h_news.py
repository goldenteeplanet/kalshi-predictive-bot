from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from kalshi_predictor.config import Settings
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.repositories import insert_market_snapshot
from kalshi_predictor.data.schema import (
    NewsFeature,
    NewsItem,
    NewsMarketLink,
    NewsSignal,
    SignalEvent,
)
from kalshi_predictor.forecasting.news_v1 import NewsV1Forecaster
from kalshi_predictor.news.classifier import classify_news_item
from kalshi_predictor.news.features import build_news_features
from kalshi_predictor.news.ingestion import ingest_news_file
from kalshi_predictor.news.linker import link_news_markets
from kalshi_predictor.news.providers import parse_rss_feed
from kalshi_predictor.news.reports import generate_news_report
from kalshi_predictor.news.signals import generate_news_signals
from kalshi_predictor.signals.attribution import extract_active_signals
from kalshi_predictor.signals.signal_types import NEWS_SIGNAL
from kalshi_predictor.ui.app import create_app
from kalshi_predictor.utils.time import utc_now


def test_manual_json_and_csv_news_ingestion(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    now = utc_now()
    json_path = Path(tmp_path) / "news.json"
    json_path.write_text(
        """
[
  {
    "source": "manual",
    "published_at": "%s",
    "title": "Fed official rate decision holds steady",
    "summary": "The Federal Reserve held interest rates steady after the FOMC meeting.",
    "category": "economic"
  },
  {
    "source": "manual",
    "published_at": "%s",
    "title": "Bitcoin surges as BTC gains after bullish rally",
    "summary": "Crypto traders saw Bitcoin gain after a strong rally.",
    "category": "crypto"
  }
]
        """.strip()
        % (now.isoformat(), now.isoformat()),
        encoding="utf-8",
    )
    csv_path = Path(tmp_path) / "news.csv"
    csv_path.write_text(
        "source,published_at,title,summary,category\n"
        f"manual,{now.isoformat()},Hurricane emergency warning issued,"
        "NOAA warns of storm risk,weather\n",
        encoding="utf-8",
    )

    with session_factory() as session:
        json_summary = ingest_news_file(session, json_path)
        csv_summary = ingest_news_file(session, csv_path)
        session.commit()
        count = session.scalar(select(func.count(NewsItem.id)))
        categories = {row.category for row in session.scalars(select(NewsItem))}

    assert json_summary.items_inserted == 2
    assert csv_summary.items_inserted == 1
    assert count == 3
    assert {"economic", "crypto", "weather"}.issubset(categories)


def test_rss_parser_with_fixture() -> None:
    xml = """
<rss><channel>
  <item>
    <title>Fed holds rates steady</title>
    <link>https://example.test/fed</link>
    <description>Federal Reserve statement.</description>
    <pubDate>Wed, 17 Jun 2026 12:00:00 GMT</pubDate>
  </item>
  <item>
    <title>Bitcoin gains after strong session</title>
    <link>https://example.test/btc</link>
    <description>BTC market news.</description>
    <pubDate>Wed, 17 Jun 2026 13:00:00 GMT</pubDate>
  </item>
</channel></rss>
    """

    items = parse_rss_feed(xml, source_name="fixture", category="economic", limit=5)

    assert len(items) == 2
    assert items[0]["source"] == "fixture"
    assert items[0]["source_url"] == "https://example.test/fed"
    assert items[0]["published_at"].startswith("2026-06-17T12:00:00")


def test_classifier_detects_categories_sentiment_and_importance() -> None:
    crypto = classify_news_item(
        {
            "title": "Bitcoin surges as BTC gains in bullish record rally",
            "published_at": utc_now().isoformat(),
        }
    )
    economic = classify_news_item(
        {
            "title": "Fed official rate decision after FOMC and CPI data",
            "published_at": utc_now().isoformat(),
        }
    )
    weather = classify_news_item(
        {
            "title": "Hurricane emergency warning from NOAA as storm strengthens",
            "published_at": utc_now().isoformat(),
        }
    )

    assert crypto["category"] == "crypto"
    assert "BTC" in crypto["entities"]
    assert crypto["sentiment_score"] > 0
    assert economic["category"] == "economic"
    assert "Fed" in economic["entities"]
    assert weather["category"] == "weather"
    assert weather["importance_score"] >= Decimal("0.70")


def test_news_market_linker_links_btc_and_fed_markets(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed_btc_market(session)
        _seed_fed_market(session)
        _ingest_btc_and_fed_news(session)
        summary = link_news_markets(
            session,
            settings=Settings(
                overnight_require_market_data=False,
                news_min_link_confidence=Decimal("0.40"),
            ),
        )
        session.commit()
        links = list(session.scalars(select(NewsMarketLink)))

    assert summary.links_created >= 2
    assert any(link.ticker == "NEWS-BTC" for link in links)
    assert any(link.ticker == "NEWS-FED" for link in links)


def test_news_features_signals_and_signal_attribution(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed_btc_market(session)
        _ingest_btc_and_fed_news(session)
        link_news_markets(
            session,
            settings=Settings(
                overnight_require_market_data=False,
                news_min_link_confidence=Decimal("0.40"),
            ),
        )
        feature_summary = build_news_features(
            session,
            window_minutes=360,
            settings=Settings(overnight_require_market_data=False),
        )
        signal_summary = generate_news_signals(session)
        active = extract_active_signals(session, ticker="NEWS-BTC", model_name="news_v1")
        session.commit()
        features = session.scalar(select(func.count(NewsFeature.id)))
        signals = session.scalar(select(func.count(NewsSignal.id)))
        events = session.scalar(select(func.count(SignalEvent.id)))

    assert feature_summary.features_inserted >= 1
    assert signal_summary.signals_created >= 1
    assert features and features >= 1
    assert signals and signals >= 1
    assert events and events >= 1
    assert any(signal.signal_name == NEWS_SIGNAL for signal in active)


def test_news_v1_skips_without_features_and_adjusts_with_news(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    forecaster = NewsV1Forecaster(settings=Settings(overnight_require_market_data=False))
    with session_factory() as session:
        no_news_snapshot = _seed_no_news_market(session)
        assert forecaster.forecast(session, no_news_snapshot) is None

        snapshot = _seed_btc_market(session)
        _ingest_btc_and_fed_news(session)
        link_news_markets(
            session,
            settings=Settings(
                overnight_require_market_data=False,
                news_min_link_confidence=Decimal("0.40"),
            ),
        )
        build_news_features(
            session,
            window_minutes=360,
            settings=Settings(overnight_require_market_data=False),
        )
        forecast = forecaster.forecast(session, snapshot)

    assert forecast is not None
    assert forecast.market_mid_probability == Decimal("0.50")
    assert forecast.yes_probability > Decimal("0.50")
    assert forecast.feature_json["linked_news"]


def test_news_report_generation_and_ui_pages(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    report_path = Path(tmp_path) / "news_report.md"
    with session_factory() as session:
        _seed_btc_market(session)
        _ingest_btc_and_fed_news(session)
        link_news_markets(
            session,
            settings=Settings(
                overnight_require_market_data=False,
                news_min_link_confidence=Decimal("0.40"),
            ),
        )
        build_news_features(
            session,
            window_minutes=360,
            settings=Settings(overnight_require_market_data=False),
        )
        generate_news_signals(session)
        path = generate_news_report(
            session,
            output_path=report_path,
            settings=Settings(overnight_require_market_data=False),
        )
        first_item = session.scalar(select(NewsItem).order_by(NewsItem.id))
        session.commit()

    client = TestClient(
        create_app(
            session_factory=session_factory,
            settings=Settings(overnight_require_market_data=False),
        )
    )
    dashboard = client.get("/news")
    detail = client.get(f"/news/{first_item.id}")

    assert path.exists()
    assert "News Intelligence Report" in path.read_text(encoding="utf-8")
    assert dashboard.status_code == 200
    assert "News Intelligence" in dashboard.text
    assert detail.status_code == 200
    assert first_item.title in detail.text


def _session_factory(tmp_path):
    engine = init_db(f"sqlite:///{Path(tmp_path) / 'phase3h.db'}")
    return get_session_factory(engine)


def _seed_btc_market(session):
    now = utc_now()
    return insert_market_snapshot(
        session,
        {
            "ticker": "NEWS-BTC",
            "status": "open",
            "title": "Will Bitcoin be above $100,000 on June 30?",
            "series_ticker": "KXBTC",
            "event_ticker": "KXBTC-NEWS",
            "close_time": (now + timedelta(hours=12)).isoformat(),
            "yes_ask_dollars": "0.52",
            "yes_bid_dollars": "0.48",
            "liquidity_dollars": "12000",
            "volume_fp": "1000",
            "open_interest_fp": "500",
        },
        {
            "orderbook_fp": {
                "yes_dollars": [["0.48", "20"]],
                "no_dollars": [["0.48", "20"]],
            }
        },
        now,
    )


def _seed_fed_market(session):
    now = utc_now()
    return insert_market_snapshot(
        session,
        {
            "ticker": "NEWS-FED",
            "status": "open",
            "title": "Will the Fed hold interest rates steady after the FOMC meeting?",
            "series_ticker": "KXFED",
            "event_ticker": "KXFED-NEWS",
            "close_time": (now + timedelta(hours=12)).isoformat(),
            "yes_ask_dollars": "0.55",
            "yes_bid_dollars": "0.51",
            "liquidity_dollars": "8000",
            "volume_fp": "1000",
            "open_interest_fp": "500",
        },
        {
            "orderbook_fp": {
                "yes_dollars": [["0.51", "20"]],
                "no_dollars": [["0.45", "20"]],
            }
        },
        now,
    )


def _seed_no_news_market(session):
    now = utc_now()
    return insert_market_snapshot(
        session,
        {
            "ticker": "NEWS-NONE",
            "status": "open",
            "title": "Will a generic event happen?",
            "series_ticker": "KXGEN",
            "event_ticker": "KXGEN-NEWS",
            "close_time": (now + timedelta(hours=12)).isoformat(),
            "yes_ask_dollars": "0.52",
            "yes_bid_dollars": "0.48",
        },
        {
            "orderbook_fp": {
                "yes_dollars": [["0.48", "20"]],
                "no_dollars": [["0.48", "20"]],
            }
        },
        now,
    )


def _ingest_btc_and_fed_news(session) -> None:
    now = utc_now()
    ingest_news_file(
        session,
        _write_news_file(
            session,
            [
                {
                    "source": "manual",
                    "published_at": now.isoformat(),
                    "title": "Bitcoin surges as BTC gains after bullish record rally",
                    "summary": "Strong crypto momentum is lifting Bitcoin.",
                    "category": "crypto",
                },
                {
                    "source": "manual",
                    "published_at": now.isoformat(),
                    "title": "Fed official rate decision holds steady after FOMC",
                    "summary": "The Federal Reserve held interest rates steady.",
                    "category": "economic",
                },
            ],
        ),
    )


def _write_news_file(session, items) -> Path:
    bind = session.get_bind()
    database = getattr(bind.url, "database", None)
    path = Path(database).with_name("seed_news.json") if database else Path("seed_news.json")
    import json

    path.write_text(json.dumps(items), encoding="utf-8")
    return path
