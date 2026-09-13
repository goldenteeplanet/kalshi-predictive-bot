import json
from datetime import UTC, datetime, timedelta

import pytest

from kalshi_predictor.crypto.cost_evidence import OriginalBook
from kalshi_predictor.crypto.current_market_scan import (
    API,
    discovery_rows,
    discovery_url,
    evaluate_current_preflight,
)

NOW = datetime(2026, 9, 13, tzinfo=UTC)


def discovery(series='KXSOLE', *, cursor='', count=1, age=0):
    payload = {'markets': [dict(ticker=f'{series}-EVENT-T{i}', status='active',
                               close_time=(NOW+timedelta(hours=1)).isoformat(),
                               expiration_time=(NOW+timedelta(hours=2)).isoformat())
                           for i in range(count)], 'cursor': cursor}
    return OriginalBook(discovery_url(series), json.dumps(payload).encode(),
                        NOW-timedelta(seconds=age))


def book(series='KXSOLE', *, age=0):
    ticker = f'{series}-EVENT-T0'
    return ticker, OriginalBook(
        f'{API}/markets/{ticker}/orderbook?depth=10',
        b'{"orderbook_fp":{"yes_dollars":[],"no_dollars":[["0.50","2.0"]]}}',
        NOW-timedelta(seconds=age))


def test_market_book_costs_never_manufacture_forecasts_or_admission():
    ticker, original = book()
    report = evaluate_current_preflight(discoveries={'KXSOLE': discovery()},
                                       books={ticker: original}, fee_originals={}, assessed_at=NOW)
    assert report['funnel']['markets_scanned'] == 1
    assert report['funnel']['book_valid_independent_of_forecast'] == 1
    assert report['funnel']['forecastable'] == report['funnel']['paper_eligible'] == 0
    yes, no = report['rows']
    assert yes['snapshot_impact']['value'] == '0.00'
    assert no['executable_price'] is None
    assert 'NO_EXECUTABLE_ASK' in no['blockers']
    assert all(r['full_net_ev'] is None and r['forecast_probability'] is None
               and not r['paper_eligible'] for r in report['rows'])
    assert report['full_net_scanner_operational'] is False


def test_discovery_empty_unsupported_fee_and_pagination_are_distinct():
    report = evaluate_current_preflight(
        discoveries={'KXSOLE': discovery(count=0), 'KXXRP': discovery('KXXRP', cursor='more')},
        books={}, fee_originals={}, assessed_at=NOW)
    sol, eth, btc, xrp, doge = report['families']
    assert sol['discovery_status'] == 'EMPTY_PAGE'
    assert xrp['discovery_status'] == 'MARKETS_RETURNED'
    assert xrp['pagination_incomplete'] is True
    assert xrp['fee_family_reviewed'] is False
    assert eth['discovery_status'] == doge['discovery_status'] == 'NOT_ACQUIRED'


@pytest.mark.parametrize('age', [-1, 301])
def test_discovery_future_and_stale_receipts_rejected(age):
    with pytest.raises(ValueError, match='INVALID_OR_STALE'):
        discovery_rows(discovery(age=age), series='KXSOLE', assessed_at=NOW)


def test_discovery_bound_and_wrong_family_rejected():
    with pytest.raises(ValueError, match='BOUND_EXCEEDED'):
        discovery_rows(discovery(count=3), series='KXSOLE', assessed_at=NOW)
    with pytest.raises(ValueError, match='INVALID_OR_STALE'):
        discovery_rows(discovery(), series='KXBTC', assessed_at=NOW)


def test_stale_book_never_gets_known_snapshot_cost():
    ticker, original = book(age=61)
    report = evaluate_current_preflight(discoveries={'KXSOLE': discovery()},
                                       books={ticker: original}, fee_originals={}, assessed_at=NOW)
    assert report['rows'][0]['snapshot_impact']['value'] is None
    assert report['funnel']['book_valid_independent_of_forecast'] == 0


def test_latest_expiration_block_does_not_erase_valid_book_evidence():
    original = discovery()
    body = json.loads(original.payload)
    body['markets'][0]['expiration_time'] = (NOW+timedelta(days=7)).isoformat()
    original = OriginalBook(original.url, json.dumps(body).encode(), NOW)
    ticker, depth = book()
    report = evaluate_current_preflight(discoveries={'KXSOLE': original},
                                       books={ticker: depth}, fee_originals={}, assessed_at=NOW)
    assert report['funnel']['book_valid_independent_of_forecast'] == 1
    assert report['funnel']['book_valid_and_latest_expiration_within_72h'] == 0
    assert 'HORIZON_NOT_VERIFIED_WITHIN_72H' in report['rows'][0]['blockers']
