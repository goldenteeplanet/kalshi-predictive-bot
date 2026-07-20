from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from kalshi_predictor.active_universe import latest_links_for_table
from kalshi_predictor.data.schema import CryptoMarketLink


def _engine():
    engine = create_engine("sqlite:///:memory:")
    CryptoMarketLink.__table__.create(engine)
    return engine


def _link(ticker: str, detected_at: datetime, symbol: str = "BTC") -> CryptoMarketLink:
    return CryptoMarketLink(
        ticker=ticker,
        symbol=symbol,
        detected_at=detected_at,
        confidence="1",
        reason="exact-scope regression fixture",
        raw_json="{}",
    )


def test_latest_links_pushes_exact_ticker_scope_into_sql() -> None:
    engine = _engine()
    statements: list[str] = []
    event.listen(
        engine,
        "before_cursor_execute",
        lambda _conn, _cursor, statement, _parameters, _context, _many: statements.append(
            statement
        ),
    )
    with Session(engine) as session:
        session.add_all(
            [
                _link("KXBTC-A", datetime(2026, 7, 1, tzinfo=UTC)),
                _link("KXBTC-A", datetime(2026, 7, 2, tzinfo=UTC)),
                _link("KXBTC-B", datetime(2026, 7, 3, tzinfo=UTC)),
                _link("KXETH-A", datetime(2026, 7, 4, tzinfo=UTC), "ETH"),
            ]
        )
        session.commit()

        rows = latest_links_for_table(
            session,
            CryptoMarketLink,
            ticker_scope={"KXBTC-A", "KXBTC-B"},
        )

    assert {row.ticker for row in rows} == {"KXBTC-A", "KXBTC-B"}
    assert next(row for row in rows if row.ticker == "KXBTC-A").detected_at == datetime(2026, 7, 2)
    select_sql = next(statement for statement in statements if "row_number()" in statement.lower())
    assert "crypto_market_links.ticker IN" in select_sql


def test_empty_exact_scope_never_falls_back_to_full_table() -> None:
    engine = _engine()
    with Session(engine) as session:
        session.add(_link("KXBTC-A", datetime(2026, 7, 1, tzinfo=UTC)))
        session.commit()
        rows = latest_links_for_table(session, CryptoMarketLink, ticker_scope=[])

    assert rows == []


def test_scope_is_exact_not_prefix_or_fuzzy() -> None:
    engine = _engine()
    with Session(engine) as session:
        session.add_all(
            [
                _link("KXBTC-A", datetime(2026, 7, 1, tzinfo=UTC)),
                _link("KXBTC-A-LOOKALIKE", datetime(2026, 7, 2, tzinfo=UTC)),
            ]
        )
        session.commit()
        rows = latest_links_for_table(
            session,
            CryptoMarketLink,
            ticker_scope=["KXBTC-A"],
        )

    assert [row.ticker for row in rows] == ["KXBTC-A"]
