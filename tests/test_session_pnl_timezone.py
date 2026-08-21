from datetime import UTC, datetime
from decimal import Decimal

from kalshi_predictor.advanced_risk.service import _session_pnl, _session_start
from kalshi_predictor.autopilot.repository import current_daily_pnl
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.schema import PaperPnl


def test_session_pnl_uses_timezone_boundary_and_latest_per_ticker_delta(tmp_path) -> None:
    engine = init_db(f"sqlite:///{tmp_path / 'session-pnl.db'}")
    with get_session_factory(engine)() as session:
        _pnl(session, "OLD", datetime(2026, 8, 20, 4, 59, tzinfo=UTC), "1", "0")
        _pnl(session, "OLD", datetime(2026, 8, 20, 5, 1, tzinfo=UTC), "1", "0")
        _pnl(session, "OLD", datetime(2026, 8, 20, 5, 2, tzinfo=UTC), "1", "0")
        _pnl(session, "NEW", datetime(2026, 8, 20, 5, 3, tzinfo=UTC), "0", "-0.5")
        _pnl(session, "NEW", datetime(2026, 8, 20, 5, 4, tzinfo=UTC), "0", "-0.5")
        session.commit()

        now = datetime(2026, 8, 20, 6, 0, tzinfo=UTC)
        start = _session_start(
            now,
            session_timezone="America/Chicago",
            session_reset_time="00:00",
        )
        realized, unrealized = _session_pnl(
            session,
            decision_timestamp=now,
            session_start=start,
        )
        daily = current_daily_pnl(
            session,
            now=now,
            session_timezone="America/Chicago",
            session_reset_time="00:00",
        )

    assert start == datetime(2026, 8, 20, 5, 0, tzinfo=UTC)
    assert realized == Decimal("0")
    assert unrealized == Decimal("-0.5")
    assert daily == Decimal("-0.5")


def _pnl(session, ticker: str, at: datetime, realized: str, unrealized: str) -> None:
    session.add(
        PaperPnl(
            ticker=ticker,
            calculated_at=at,
            yes_contracts=1,
            no_contracts=0,
            avg_yes_price="0.5",
            avg_no_price=None,
            settlement_result=None,
            realized_pnl=realized,
            unrealized_pnl=unrealized,
            total_pnl=str(Decimal(realized) + Decimal(unrealized)),
            notes="fixture",
        )
    )
