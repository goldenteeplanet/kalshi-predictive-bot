from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.repositories import upsert_market
from kalshi_predictor.data.schema import PaperOrder, Settlement
from kalshi_predictor.ingest.markets import sync_settlements
from kalshi_predictor.learning.targets import SLOW_COMPOSITE_PREFIXES
from kalshi_predictor.paper.ledger import create_paper_order
from kalshi_predictor.paper.models import BUY_YES, PaperDecision


def test_paper_order_rejects_closed_or_near_close_market(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    now = datetime.now(UTC)
    with session_factory() as session:
        upsert_market(
            session,
            {
                "ticker": "CLOSING-SOON",
                "status": "open",
                "close_time": (now + timedelta(minutes=4)).isoformat(),
            },
        )

        assert create_paper_order(session, _decision("CLOSING-SOON")) is None


def test_paper_order_rejects_market_with_exact_settlement(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    now = datetime.now(UTC)
    with session_factory() as session:
        upsert_market(
            session,
            {
                "ticker": "ALREADY-SETTLED",
                "status": "open",
                "close_time": (now + timedelta(hours=1)).isoformat(),
            },
        )
        session.add(
            Settlement(
                ticker="ALREADY-SETTLED",
                settled_at=now,
                result="yes",
                yes_settlement_value="1",
                raw_json="{}",
                updated_at=now,
            )
        )
        session.flush()

        assert create_paper_order(session, _decision("ALREADY-SETTLED")) is None


def test_settlement_sync_recovers_missing_paper_ticker_exactly(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    now = datetime.now(UTC)
    client = _ExactRecoveryClient(now)
    with session_factory() as session:
        session.add(
            PaperOrder(
                ticker="RECOVER-ME",
                forecast_id=None,
                created_at=now - timedelta(hours=1),
                model_name="test",
                side="BUY_YES",
                probability="0.6",
                market_price="0.5",
                limit_price="0.5",
                edge="0.1",
                quantity=1,
                status="FILLED",
                reason="test",
                raw_decision_json="{}",
            )
        )
        session.flush()

        count = sync_settlements(session=session, client=client, max_pages=1)

        assert count == 1
        assert client.requested == ["RECOVER-ME"]
        assert session.get(Settlement, "RECOVER-ME") is not None


def test_settlement_sync_recovers_recent_past_close_market_without_paper_order(
    tmp_path,
) -> None:
    session_factory = _session_factory(tmp_path)
    now = datetime.now(UTC)
    client = _ExactRecoveryClient(now)
    with session_factory() as session:
        upsert_market(
            session,
            {
                "ticker": "PAST-CLOSE-MISSING",
                "event_ticker": "KXBTC-PAST-CLOSE",
                "status": "active",
                "close_time": (now - timedelta(minutes=5)).isoformat(),
            },
        )
        upsert_market(
            session,
            {
                "ticker": "UNRELATED-PAST-CLOSE",
                "event_ticker": "UNRELATED-PAST-CLOSE",
                "status": "active",
                "close_time": (now - timedelta(minutes=5)).isoformat(),
            },
        )
        session.flush()

        telemetry = []
        count = sync_settlements(
            session=session,
            client=client,
            max_pages=1,
            telemetry_callback=telemetry.append,
        )

        assert count == 1
        assert client.requested == ["PAST-CLOSE-MISSING"]
        settlement = session.get(Settlement, "PAST-CLOSE-MISSING")
        assert settlement is not None
        assert settlement.result == "yes"
        assert telemetry == [
            {
                "global_page_seen": 0,
                "global_page_canonical": 0,
                "global_page_unresolved": 0,
                "global_page_outside_window": 0,
                "paper_exact_candidates": 0,
                "due_local_exact_candidates": 1,
                "explicit_exact_candidates": 0,
                "exact_unique_candidates": 1,
                "exact_canonical": 1,
                "exact_unresolved": 0,
                "exact_api_errors": 0,
                "persisted": 1,
                "commits": 0,
                "exact_recovery_budget": 100,
                "explicit_exact_admitted": 0,
                "paper_exact_admitted": 0,
                "series_candidates": {
                    "KXBTC": 1,
                    "KXETH": 0,
                    "KXSOLE": 0,
                    "KXXRP": 0,
                    "KXDOGE": 0,
                },
            }
        ]


def test_settlement_due_local_recovery_is_fair_across_authorized_series(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    now = datetime.now(UTC)
    client = _ExactRecoveryClient(now)
    roots = ("KXBTC", "KXETH", "KXSOLE", "KXXRP", "KXDOGE")
    with session_factory() as session:
        for root in roots:
            upsert_market(
                session,
                {
                    "ticker": f"{root}-DUE-TICKER",
                    "event_ticker": f"{root}-DUE-EVENT",
                    "status": "active",
                    "close_time": (now - timedelta(minutes=5)).isoformat(),
                },
            )
        upsert_market(
            session,
            {
                "ticker": "KXBTC-NEWER-DUE-TICKER",
                "event_ticker": "KXBTC-NEWER-DUE-EVENT",
                "status": "active",
                "close_time": (now - timedelta(minutes=1)).isoformat(),
            },
        )
        session.flush()
        telemetry = []

        count = sync_settlements(
            session=session,
            client=client,
            max_pages=0,
            exact_recovery_limit=5,
            recover_paper_tickers=False,
            telemetry_callback=telemetry.append,
        )

        assert count == 5
        assert set(client.requested) == {f"{root}-DUE-TICKER" for root in roots}
        assert "KXBTC-NEWER-DUE-TICKER" not in client.requested
        assert telemetry[0]["series_candidates"] == {root: 1 for root in roots}
        assert telemetry[0]["exact_unique_candidates"] == 5


def test_explicit_hints_reserve_but_do_not_increase_fair_budget(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    now = datetime.now(UTC)
    client = _ExactRecoveryClient(now)
    roots = ("KXBTC", "KXETH", "KXSOLE", "KXXRP", "KXDOGE")
    with session_factory() as session:
        for root in roots:
            for suffix in ("OLD", "HINT"):
                upsert_market(
                    session,
                    {
                        "ticker": f"{root}-{suffix}",
                        "event_ticker": f"{root}-EVENT",
                        "status": "active",
                        "close_time": (now - timedelta(minutes=5)).isoformat(),
                    },
                )
        session.flush()
        telemetry = []
        count = sync_settlements(
            session=session,
            client=client,
            max_pages=0,
            exact_recovery_limit=5,
            recover_paper_tickers=False,
            exact_recovery_tickers=[f"{root}-HINT" for root in roots],
            telemetry_callback=telemetry.append,
        )
        assert count == 5
        assert set(client.requested) == {f"{root}-HINT" for root in roots}
        assert telemetry[0]["exact_recovery_budget"] == 5
        assert telemetry[0]["explicit_exact_admitted"] == 5
        assert telemetry[0]["due_local_exact_candidates"] == 0


def test_runtime_launcher_runs_settlement_and_pnl_cycle() -> None:
    root = Path(__file__).parents[1]
    launcher = (root / "scripts/local/kalshi-fixed-rate-refresh.sh").read_text(encoding="utf-8")

    assert "sync-settlements" in launcher
    assert "--lookback-days 90 --limit 200 --max-pages 10" in launcher
    assert "paper-pnl --skip-signal-refresh" in launcher
    assert "phase3aa-realize" in launcher
    assert "--no-dry-run" in launcher


def test_slow_composite_families_are_excluded_from_learning_targets() -> None:
    assert "KXMVECROSSCATEGORY-" in SLOW_COMPOSITE_PREFIXES
    assert "KXMVESPORTSMULTIGAMEEXTENDED-" in SLOW_COMPOSITE_PREFIXES


class _ExactRecoveryClient:
    def __init__(self, now: datetime) -> None:
        self.now = now
        self.requested: list[str] = []

    def iter_markets(self, **_kwargs):
        return iter(())

    def get_market(self, ticker: str):
        self.requested.append(ticker)
        return {
            "ticker": ticker,
            "status": "settled",
            "result": "yes",
            "settlement_value_dollars": "1.00",
            "settlement_ts": self.now.isoformat(),
        }


def _decision(ticker: str) -> PaperDecision:
    return PaperDecision(
        ticker=ticker,
        forecast_id=None,
        model_name="test",
        side=BUY_YES,
        probability=Decimal("0.6"),
        market_price=Decimal("0.5"),
        limit_price=Decimal("0.5"),
        edge=Decimal("0.1"),
        quantity=1,
        reason="test",
        raw_decision_json={},
    )


def _session_factory(tmp_path):
    engine = init_db(f"sqlite:///{tmp_path / 'settlement_reliability.db'}")
    return get_session_factory(engine)
