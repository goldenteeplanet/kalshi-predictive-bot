import json
import time
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError

from kalshi_predictor.candidate_funnel_audit import make_candidate_funnel_read_only_engine
from kalshi_predictor.config import Settings
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.repositories import (
    decode_json,
    insert_forecast,
    insert_market_snapshot,
    upsert_market,
)
from kalshi_predictor.data.schema import Market, MarketRanking
from kalshi_predictor.forecasting.base import ForecastOutput
from kalshi_predictor.opportunities.repository import insert_market_ranking
from kalshi_predictor.opportunities.scanner import scan_opportunities
from kalshi_predictor.phase_gh2 import select_actionable_ranked_markets
from kalshi_predictor.series_lineage_repair import (
    apply_lineage_repair,
    build_lineage_repair_plan,
    fetch_exact_catalog_lineage,
    normalize_tickers,
    write_lineage_repair_artifacts,
)
from kalshi_predictor.utils.time import utc_now


def test_partial_snapshot_preserves_lineage_and_propagates_to_ranking(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    now = utc_now()
    with session_factory() as session:
        upsert_market(
            session,
            {
                "ticker": "WEATHER",
                "event_ticker": "WEATHER-EVENT",
                "series_ticker": "WEATHER-SERIES",
                "status": "open",
                "close_time": (now + timedelta(hours=4)).isoformat(),
            },
        )
        snapshot = insert_market_snapshot(
            session,
            {"ticker": "WEATHER", "status": "open", "liquidity_dollars": "100"},
            {
                "orderbook_fp": {
                    "yes_dollars": [["0.40", "10"]],
                    "no_dollars": [["0.50", "10"]],
                }
            },
            now,
        )
        insert_forecast(
            session,
            ForecastOutput(
                ticker="WEATHER",
                forecasted_at=now,
                model_name="weather_v2",
                yes_probability=Decimal("0.55"),
                market_mid_probability=None,
                best_yes_bid=Decimal("0.40"),
                best_yes_ask=Decimal(snapshot.best_yes_ask),
                feature_json={"source": "test"},
            ),
        )
        summary = scan_opportunities(
            session,
            model_name="weather_v2",
            settings=_settings(),
            min_edge=Decimal("0"),
            min_score=Decimal("0"),
        )
        session.commit()

        raw = decode_json(snapshot.raw_market_json)
        market = session.get(Market, "WEATHER")
        ranking = session.scalar(select(MarketRanking).where(MarketRanking.ticker == "WEATHER"))
        assert raw["event_ticker"] == "WEATHER-EVENT"
        assert raw["series_ticker"] == "WEATHER-SERIES"
        assert market is not None and market.series_ticker == "WEATHER-SERIES"
        assert ranking is not None and ranking.series_ticker == "WEATHER-SERIES"
        assert summary.rankings[0]["series_ticker"] == "WEATHER-SERIES"


def test_explicit_snapshot_lineage_remains_authoritative(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        upsert_market(
            session,
            {"ticker": "M", "event_ticker": "OLD-E", "series_ticker": "OLD-S"},
        )
        snapshot = insert_market_snapshot(
            session,
            {"ticker": "M", "event_ticker": "NEW-E", "series_ticker": "NEW-S"},
            None,
            utc_now(),
        )
        assert decode_json(snapshot.raw_market_json)["series_ticker"] == "NEW-S"
        assert session.get(Market, "M").series_ticker == "NEW-S"


def test_selector_diversity_uses_preserved_series(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    now = utc_now()
    with session_factory() as session:
        for ticker, series, score in (
            ("C1", "CRYPTO", "30"),
            ("C2", "CRYPTO", "29"),
            ("W1", "WEATHER", "28"),
        ):
            snapshot = insert_market_snapshot(
                session,
                {
                    "ticker": ticker,
                    "series_ticker": series,
                    "status": "open",
                    "close_time": (now + timedelta(hours=2)).isoformat(),
                },
                None,
                now,
            )
            insert_market_ranking(
                session,
                {
                    "ticker": ticker,
                    "ranked_at": now,
                    "series_ticker": series,
                    "forecast_model": "crypto_v2" if series == "CRYPTO" else "weather_v2",
                    "best_side": "BUY_YES",
                    "best_price": "0.4",
                    "estimated_edge": "0.01",
                    "opportunity_score": score,
                },
                attribution_enabled=False,
            )
            assert snapshot.id is not None
        selected = select_actionable_ranked_markets(
            session,
            limit=2,
            max_per_series=1,
            now=now,
        )
        assert [row["ticker"] for row in selected] == ["C1", "W1"]


def test_dry_run_plan_is_read_only_and_apply_is_idempotent(tmp_path) -> None:
    database = tmp_path / "repair.db"
    writable = init_db(f"sqlite:///{database.as_posix()}")
    writable_factory = get_session_factory(writable)
    with writable_factory.begin() as session:
        upsert_market(session, {"ticker": "M", "status": "open"})

    read_only = make_candidate_funnel_read_only_engine(f"sqlite:///{database.as_posix()}")
    read_only_factory = get_session_factory(read_only)
    evidence = {"M": [{"ticker": "M", "event_ticker": "E", "series_ticker": "S"}]}
    with read_only_factory() as session:
        plan = build_lineage_repair_plan(session, tickers=["M"], catalog_evidence=evidence)
        assert plan["summary"]["applicable"] == 1
        with pytest.raises(OperationalError, match="readonly database"):
            session.execute(text("UPDATE markets SET series_ticker='BAD' WHERE ticker='M'"))

    with writable_factory.begin() as session:
        result = apply_lineage_repair(session, plan=plan, writer_monitor=_clear_writer)
        assert result["applied"] == 1
    with writable_factory.begin() as session:
        second = build_lineage_repair_plan(session, tickers=["M"], catalog_evidence=evidence)
        result = apply_lineage_repair(session, plan=second, writer_monitor=_clear_writer)
        assert second["summary"]["unchanged"] == 1
        assert result["applied"] == 0


def test_conflicts_fail_closed_and_writer_gate_blocks_apply(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory.begin() as session:
        upsert_market(session, {"ticker": "M", "series_ticker": "KNOWN"})
    evidence = {"M": [{"ticker": "M", "series_ticker": "DIFFERENT"}]}
    with session_factory() as session:
        plan = build_lineage_repair_plan(session, tickers=["M"], catalog_evidence=evidence)
        assert plan["rows"][0]["reason"] == "SERIES_CONFLICT"

    repair = {"rows": [{"ticker": "M", "action": "APPLY", "before": {}, "source": {}}]}
    with session_factory() as session:
        with pytest.raises(RuntimeError, match="Writer gate"):
            apply_lineage_repair(
                session,
                plan=repair,
                writer_monitor=lambda: {"writer_count": 1, "safe_to_start_write": False},
            )


def test_inactive_market_repair_is_blocked(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory.begin() as session:
        upsert_market(session, {"ticker": "M", "status": "closed"})
    evidence = {"M": [{"ticker": "M", "status": "open", "series_ticker": "S"}]}
    with session_factory() as session:
        plan = build_lineage_repair_plan(session, tickers=["M"], catalog_evidence=evidence)
        assert plan["rows"][0]["reason"] == "MARKET_NOT_ACTIVE"


def test_bounded_fetch_and_rollback_artifact(tmp_path) -> None:
    tickers = normalize_tickers("m,m", limit=1)
    evidence = fetch_exact_catalog_lineage(
        _FakeClient(),
        tickers=tickers,
        deadline_monotonic=time.monotonic() + 5,
    )
    assert list(evidence) == ["M"]
    plan = {
        "summary": {"requested": 1, "applicable": 1, "blocked": 0},
        "rows": [
            {
                "ticker": "M",
                "action": "APPLY",
                "reason": "SOURCE_BACKED_NULL_REPAIR",
                "before": {"event_ticker": None, "series_ticker": None, "raw_json": "{}"},
                "source_sha256": "abc",
            }
        ],
    }
    artifacts = write_lineage_repair_artifacts(
        plan=plan,
        output_dir=tmp_path / "reports",
        dry_run=True,
    )
    rollback = json.loads(artifacts.rollback_path.read_text())
    assert rollback["rows"][0]["restore"]["series_ticker"] is None
    with pytest.raises(ValueError, match="exceeds limit"):
        normalize_tickers("A,B", limit=1)


class _FakeClient:
    def get_markets(self, **_kwargs):
        return {"markets": [{"ticker": "M", "event_ticker": "E", "series_ticker": "S"}]}


def _session_factory(tmp_path):
    engine = init_db(f"sqlite:///{tmp_path / 'lineage.db'}")
    return get_session_factory(engine)


def _settings() -> Settings:
    return Settings(
        opportunity_min_edge=Decimal("0"),
        opportunity_min_score=Decimal("0"),
        opportunity_max_spread=Decimal("1"),
        opportunity_min_liquidity=Decimal("0"),
        opportunity_min_time_to_close_minutes=Decimal("0"),
    )


def _clear_writer() -> dict:
    return {"writer_count": 0, "safe_to_start_write": True}
