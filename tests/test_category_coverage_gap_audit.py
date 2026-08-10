from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from sqlalchemy import select

from kalshi_predictor.category_coverage_gap_audit import build_category_coverage_gap_audit
from kalshi_predictor.crypto.repository import insert_crypto_market_link
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.repositories import insert_market_snapshot, upsert_market
from kalshi_predictor.data.schema import Market
from kalshi_predictor.market_legs import parse_and_store_market_legs
from kalshi_predictor.utils.time import utc_now


def test_category_gap_funnel_is_cumulative_and_reports_profitability(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    now = utc_now()
    with session_factory() as session:
        upsert_market(
            session,
            {
                "ticker": "KXBTC-AUDIT",
                "title": "yes Target Price: $100,000",
                "status": "active",
                "close_time": now + timedelta(days=1),
            },
        )
        parse_and_store_market_legs(session, refresh=True)
        insert_crypto_market_link(
            session,
            ticker="KXBTC-AUDIT",
            symbol="BTC",
            confidence=1,
            reason="exact test evidence",
        )
        insert_market_snapshot(
            session,
            {"ticker": "KXBTC-AUDIT", "status": "active"},
            None,
            now,
        )
        session.commit()
        payload = build_category_coverage_gap_audit(
            session,
            manifest={},
            gh2={},
            freshness_minutes=15,
        )

    crypto = payload["categories"]["crypto"]
    assert crypto["counts"]["active_catalog"] == 1
    assert crypto["counts"]["authoritative_link"] == 1
    assert crypto["counts"]["forecast"] == 0
    assert crypto["first_blocker"] == "MISSING_MODEL_FORECAST"
    assert payload["safety"]["database_writes"] == 0


def test_empty_category_reports_absent_active_opportunity(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        payload = build_category_coverage_gap_audit(
            session,
            manifest={},
            gh2={},
            freshness_minutes=15,
        )
    assert payload["categories"]["economic"]["first_blocker"] == "NO_ACTIVE_OPPORTUNITY"


def test_dashboard_exposes_linkable_denominator(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    from kalshi_predictor.market_legs import link_coverage_dashboard

    with session_factory() as session:
        upsert_market(
            session,
            {"ticker": "KXBTC-LINKED", "title": "yes Target Price: $100,000"},
        )
        parse_and_store_market_legs(session, refresh=True)
        insert_crypto_market_link(
            session,
            ticker="KXBTC-LINKED",
            symbol="BTC",
            confidence=1,
            reason="test",
        )
        coverage = link_coverage_dashboard(session)

    crypto = next(row for row in coverage["category_rows"] if row["category"] == "crypto")
    assert crypto["current_coverage_display"] == "100.0% (1/1 eligible)"


def test_read_only_engine_rejects_writes(tmp_path: Path) -> None:
    from sqlalchemy import create_engine

    from kalshi_predictor.candidate_funnel_audit import make_candidate_funnel_read_only_engine
    from kalshi_predictor.data.schema import Base

    database = tmp_path / "audit.db"
    writable = create_engine(f"sqlite:///{database}")
    Base.metadata.create_all(writable)
    read_only = make_candidate_funnel_read_only_engine(f"sqlite:///{database}")
    with read_only.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA query_only").scalar_one() == 1
        try:
            connection.execute(Market.__table__.insert().values(ticker="NO-WRITE"))
        except Exception as exc:
            assert "readonly" in str(exc).lower() or "query only" in str(exc).lower()
        else:
            raise AssertionError("read-only category audit accepted a write")
        assert connection.execute(select(Market.ticker)).all() == []


def _session_factory(tmp_path: Path):
    engine = init_db(f"sqlite:///{tmp_path / 'category_gap.db'}")
    return get_session_factory(engine)
