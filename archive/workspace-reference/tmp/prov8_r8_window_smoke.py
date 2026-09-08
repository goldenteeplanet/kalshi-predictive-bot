from datetime import timedelta
from pathlib import Path

from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.repositories import upsert_market
from kalshi_predictor.data.schema import MarketSnapshot
from kalshi_predictor.phase3bb_r8_unified_paper_gate import _latest_by_ticker
from kalshi_predictor.utils.time import utc_now

path = Path('/tmp/prov8_r8_window.db')
path.unlink(missing_ok=True)
factory = get_session_factory(init_db(f'sqlite:///{path}'))
now = utc_now()
tickers = ['PROV8-A', 'PROV8-B']
with factory() as session:
    for ticker in tickers:
        upsert_market(session, {
            'ticker': ticker, 'title': ticker, 'event_ticker': ticker,
            'series_ticker': ticker, 'status': 'open',
            'close_time': '2030-01-01T00:00:00Z', 'market_type': 'binary',
            'rules_primary': 'test',
        })
        for offset in range(500):
            session.add(MarketSnapshot(
                ticker=ticker, captured_at=now + timedelta(seconds=offset),
                status='open', raw_market_json='{}',
            ))
    session.flush()
    expected = {
        ticker: session.query(MarketSnapshot.id)
        .filter(MarketSnapshot.ticker == ticker)
        .order_by(MarketSnapshot.captured_at.desc(), MarketSnapshot.id.desc())
        .first()[0]
        for ticker in tickers
    }
    session.expunge_all()
    latest = _latest_by_ticker(session, MarketSnapshot, tickers, MarketSnapshot.captured_at)
    assert {ticker: row.id for ticker, row in latest.items()} == expected
    assert len(session.identity_map) == 2
path.unlink()
print('PROV8_R8_WINDOW_SMOKE=PASS rows_seeded=1000 rows_materialized=2')
