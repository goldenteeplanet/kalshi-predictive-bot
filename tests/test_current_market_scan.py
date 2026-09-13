import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from kalshi_predictor.crypto.account_fee_evidence import FeeAuthorityOriginal
from kalshi_predictor.crypto.cost_evidence import OriginalBook
from kalshi_predictor.crypto.current_market_scan import (
    API,
    CurrentResearchInputs,
    discovery_page_url,
    discovery_rows,
    discovery_url,
    evaluate_current_preflight,
    evaluate_paginated_current_research,
    paginated_discovery_rows,
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


def test_discovery_empty_reviewed_family_and_pagination_are_distinct():
    report = evaluate_current_preflight(
        discoveries={'KXSOLE': discovery(count=0), 'KXXRP': discovery('KXXRP', cursor='more')},
        books={}, fee_originals={}, assessed_at=NOW)
    sol, eth, btc, xrp, doge = report['families']
    assert sol['discovery_status'] == 'EMPTY_PAGE'
    assert xrp['discovery_status'] == 'MARKETS_RETURNED'
    assert xrp['pagination_incomplete'] is True
    assert xrp['fee_family_reviewed'] is True
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


def page(*, cursor='', next_cursor='', start=0, count=20, at=NOW):
    body = json.loads(discovery(count=count).payload)
    for i, market in enumerate(body['markets'], start=start):
        market['ticker'] = f'KXSOLE-EVENT-T{i}'
    body['cursor'] = next_cursor
    return OriginalBook(discovery_page_url('KXSOLE', cursor=cursor),
                        json.dumps(body).encode(), at)


def test_three_pages_keep_exact_lineage_and_stop_at_sixty_markets():
    pages = (page(next_cursor='next one'), page(cursor='next one', next_cursor='third', start=20),
             page(cursor='third', next_cursor='fourth', start=40))
    result = paginated_discovery_rows(pages, series='KXSOLE', assessed_at=NOW)
    assert result['unique_markets'] == result['raw_rows_returned'] == 60
    assert result['pagination_incomplete'] and result['stop_reason'] == 'PAGE_BUDGET_REACHED'
    assert result['rows'][21]['sources'][0]['sha256'] == pages[1].sha256
    assert 'cursor=next+one' in result['pages'][1]['url']
    with pytest.raises(ValueError, match='PAGE_COUNT'):
        paginated_discovery_rows(pages+(page(cursor='fourth'),),
                                 series='KXSOLE', assessed_at=NOW)


@pytest.mark.parametrize('change', ['wrong_cursor', 'stale', 'future', 'loop', 'over_limit'])
def test_page_chain_rejects_invalid_originals(change):
    first = page(next_cursor='second')
    second = page(cursor='second', start=20)
    if change == 'wrong_cursor':
        second = page(cursor='wrong', start=20)
    elif change == 'stale':
        first = replace(first, received_at=NOW-timedelta(seconds=301))
    elif change == 'future':
        second = replace(second, received_at=NOW+timedelta(seconds=1))
    elif change == 'loop':
        second = page(cursor='second', next_cursor='second', start=20)
    else:
        second = page(cursor='second', start=20, count=21)
    with pytest.raises(ValueError):
        paginated_discovery_rows((first, second), series='KXSOLE', assessed_at=NOW)


def test_pagination_deduplicates_without_losing_conflicting_sightings():
    first = page(next_cursor='second', count=1)
    second = page(cursor='second', count=1)
    body = json.loads(second.payload)
    body['markets'][0]['close_time'] = (NOW+timedelta(hours=3)).isoformat()
    second = replace(second, payload=json.dumps(body).encode())
    result = paginated_discovery_rows((first, second), series='KXSOLE', assessed_at=NOW)
    assert result['unique_markets'] == 1 and result['raw_rows_returned'] == 2
    assert result['rows'][0]['conflicting_sightings']
    assert len(result['rows'][0]['sources']) == 2


def test_every_discovered_market_has_explicit_missing_inputs_even_without_books():
    report = evaluate_paginated_current_research(
        discovery_pages={'KXSOLE': (page(count=20),)}, books={},
        fee_originals={}, assessed_at=NOW)
    assert report['funnel']['markets_scanned'] == 20
    assert len(report['rows']) == 40
    assert report['first_blocker_counts'] == {'NO_CURRENT_PREPARED_FORECAST': 20}
    assert all('BOOK_NOT_ACQUIRED' in row['blockers'] for row in report['rows'])
    assert all(row['full_net_ev'] is None and not row['paper_eligible'] for row in report['rows'])
    assert report['funnel']['forecastable'] == 0


def intake_scan_request():
    from test_current_research_intake import request
    original = request()
    target = original['target']
    at = original.pop('as_of')
    market = json.loads(target.market_original)['market']
    ticker = market['ticker']
    discovery = OriginalBook(discovery_page_url('KXBTC'),
                             json.dumps({'markets': [market], 'cursor': ''}).encode(), at)
    depth = OriginalBook(f'{API}/markets/{ticker}/orderbook?depth=10',
                         b'{"orderbook_fp":{"yes_dollars":[["0.40","2"]],'
                         b'"no_dollars":[["0.50","2"]]}}', at)
    return dict(discovery_pages={'KXBTC': (discovery,)}, books={ticker: depth},
                fee_originals={}, assessed_at=at,
                research_inputs={ticker: CurrentResearchInputs(**original)})


def test_current_intake_is_recomputed_but_unknown_fee_never_becomes_zero():
    request = intake_scan_request()
    result = evaluate_paginated_current_research(**request)
    assert result['funnel']['forecastable'] == result['funnel']['book_valid'] == 1
    assert result['rows'][0]['forecast']['research_forecast_id']
    assert result['rows'][0]['gross_edge'] is not None
    assert result['rows'][0]['after_fee'] is None
    assert result['funnel']['conservative_bounds_known'] == 0
    assert result['funnel']['uncertainty_known'] == result['funnel']['full_net_positive'] == 0
    assert all(not row['paper_horizon_verified'] for row in result['rows'])
    assert result['funnel']['observation_horizon_eligible'] == 1
    assert result['funnel']['horizon_eligible'] == 0
    assert result['funnel']['phase_3m_pass'] == result['funnel']['phase_3n_allow'] == 0
    json.dumps(result)


def test_forged_forecast_dictionary_and_modified_original_fail_closed():
    request = intake_scan_request()
    ticker = next(iter(request['research_inputs']))
    original = request['research_inputs'][ticker]
    for invalid in ({'probability_yes': '.99', 'calibrated': True},
                    replace(original, cf_original=original.cf_original+b' ')):
        request['research_inputs'][ticker] = invalid
        result = evaluate_paginated_current_research(**request)
        assert result['funnel']['forecastable'] == 0
        assert all(row['forecast_probability'] is None for row in result['rows'])


def test_bound_inputs_reject_extra_books_outside_current_discovery():
    with pytest.raises(ValueError, match='OUTSIDE_VERIFIED_DISCOVERY'):
        evaluate_paginated_current_research(discovery_pages={}, books=dict([book()]),
                                            fee_originals={}, assessed_at=NOW)


def test_original_fee_and_forecast_yield_support_bounds_not_calibrated_full_net():
    from test_public_paper_costs import authority
    request = intake_scan_request()
    now = request['assessed_at']
    originals = authority()
    root = Path(__file__).parent/'fixtures/public_paper_fees'
    request['fee_originals'] = {'KXBTC': (
        FeeAuthorityOriginal(originals[0].url, originals[0].payload, now),
        FeeAuthorityOriginal(f'{API}/series/KXBTC', (root/'KXBTC-series.json').read_bytes(), now),
        FeeAuthorityOriginal(f'{API}/series/fee_changes?series_ticker=KXBTC&show_historical=true',
                             originals[2].payload, now),
    )}
    result = evaluate_paginated_current_research(**request)
    assert result['funnel']['conservative_bounds_known'] == 1
    assert result['funnel']['uncertainty_known'] == 0
    row = result['rows'][0]
    assert row['fee']['value'] == '0.02'
    assert row['net_lower_bound'] == '-0.52'
    assert row['conservative_bound']['status'] == 'CONSERVATIVE_BOUND'
    assert row['conservative_bound']['paper_support'] is False
    assert row['uncertainty'] is row['full_net_ev'] is None
    assert all(not r['paper_eligible'] for r in result['rows'])
    json.dumps(result)
