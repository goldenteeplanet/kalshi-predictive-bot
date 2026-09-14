import copy
import json
from datetime import timedelta
from decimal import Decimal, Inexact, localcontext

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_current_assessment_view import NOW, envelopes, record
from test_current_research_dashboard import database

from kalshi_predictor.overnight_paper.alpha_analysis import captured_alpha_analysis
from kalshi_predictor.overnight_paper.current_research_store import append_current_record
from kalshi_predictor.overnight_paper.dashboard import create_router as paper_router
from kalshi_predictor.ui.positive_ev import create_router


def alpha_row(p='0.8', ask='0.7', fee='0.01', **changes):
    with localcontext() as ctx:
        ctx.prec = 28
        gross = Decimal(p)-Decimal(ask)
        after = gross-Decimal(fee)
    item = record(forecast_probability=p, executable_price=ask, gross_edge=str(gross),
                  after_fee=str(after), after_execution=str(after), asset='BTC',
                  observation_close_hours=0.25)
    item['fee']['value'] = fee
    item.update(changes)
    return item


def test_strict_gross_and_after_fee_thresholds():
    rows = [alpha_row(p=str(Decimal('.5') + Decimal(c)/100), ask='.5',
                      ticker=f'BTC-{c}') for c in (0, 1, 3, 5, 7, 10, 11)]
    result = captured_alpha_analysis(envelopes(*rows), now=NOW)
    funnel = result['funnel']
    assert funnel['gross_positive'] == 6
    assert funnel['gross_thresholds_cents'] == {'1': 5, '3': 4, '5': 3, '7': 2, '10': 1}
    assert funnel['after_fee_positive'] == 5 and funnel['after_fee_gt_5c'] == 3
    assert result['paper_eligible'] is False and result['full_net_ev'] is None


def test_accurate_extreme_does_not_create_trading_value():
    result = captured_alpha_analysis(envelopes(alpha_row('.994', '.99')), now=NOW)
    row = result['rows'][0]
    assert row['gross_edge'] == '0.004' and row['after_fee'] == '-0.006'
    assert row['trading_value'] == 'NONPOSITIVE_AFTER_RECORDED_FEE'
    assert result['calibration_value'] == 'NOT_EVALUATED_BY_ALPHA_VIEW'
    assert result['model_vs_market_performance'] is None


def test_stale_has_captured_evidence_but_no_current_winners():
    result = captured_alpha_analysis(envelopes(alpha_row()), now=NOW+timedelta(minutes=6))
    assert result['freshness'] == 'STALE'
    assert result['best_captured_after_fee'] is not None
    assert result['current_best_after_fee'] is None


def test_latest_selected_before_economics_and_inputs_unchanged():
    old = alpha_row('.99', '.01', assessed_at=(NOW-timedelta(minutes=1)).isoformat())
    rows = envelopes(old, alpha_row('.4', '.5'))
    before = copy.deepcopy(rows)
    result = captured_alpha_analysis(rows, now=NOW)
    assert result['funnel']['gross_positive'] == 0 and rows == before


def test_missing_ask_or_supported_fee_remains_unknown():
    no_ask = alpha_row(executable_price=None, ticker='NOASK', gross_edge=None,
                       after_fee=None, after_execution=None)
    no_fee = alpha_row(ticker='NOFEE')
    no_fee.update(fee=None, after_fee=None, after_execution=None)
    rows = [no_ask, no_fee]
    result = captured_alpha_analysis(envelopes(*rows), now=NOW)
    assert result['funnel']['after_fee_known'] == 0
    assert result['funnel']['missing_executable_ask'] == 1
    assert result['funnel']['missing_supported_fee'] == 1
    assert all(row['market_midpoint'] is None for row in result['rows'])


def test_midpoint_requires_same_book_and_both_asks():
    book = {'sha256': 'a'*64, 'received_at': NOW.isoformat(), 'url': 'captured'}
    yes = alpha_row('.6', '.55', side='YES', book_source=book)
    no = alpha_row('.4', '.5', side='NO', book_source=book)
    result = captured_alpha_analysis(envelopes(yes, no), now=NOW)
    rows = {r['side']: r for r in result['rows']}
    assert rows['YES']['market_midpoint'] == '0.525'
    assert rows['YES']['model_market_disagreement'] == '0.075'
    no['book_source'] = {**book, 'sha256': 'b'*64}
    result = captured_alpha_analysis(envelopes(yes, no), now=NOW)
    assert all(r['market_midpoint'] is None for r in result['rows'])


@pytest.mark.parametrize('p,band', [('0', '0-10%'), ('.1', '10-25%'), ('.25', '25-40%'),
                                 ('.4', '40-60%'), ('.6', '60-75%'),
                                 ('.75', '75-90%'), ('.9', '90-100%'), ('1', '90-100%')])
def test_probability_boundaries(p, band):
    result = captured_alpha_analysis(envelopes(alpha_row(p, '.5')), now=NOW)
    assert result['rows'][0]['probability_band'] == band


def test_asset_and_observation_horizon_not_inferred_market_family():
    result = captured_alpha_analysis(envelopes(alpha_row(asset='SOL')), now=NOW)
    assert result['by_asset']['SOL']['forecast_ready_sides'] == 1
    assert result['by_asset']['BTC']['forecast_ready_sides'] == 0
    assert result['rows'][0]['horizon'] == 'GT_5_LE_15_MIN'
    assert result['horizon_semantics'] == 'REMAINING_TIME_TO_OBSERVATION_NOT_MARKET_FAMILY'


def test_bounds_and_invalid_arithmetic_fail_closed():
    with pytest.raises(ValueError, match='BOUNDED'):
        captured_alpha_analysis(envelopes(*[alpha_row()] * 601), now=NOW)
    result = captured_alpha_analysis(envelopes(alpha_row(gross_edge='.99')), now=NOW)
    assert result['status'] == 'INVALID_OR_AMBIGUOUS' and result['funnel'] is None


def test_caller_decimal_traps_do_not_change_alpha():
    rows = envelopes(alpha_row('.9938197717037843704', '.99'))
    expected = captured_alpha_analysis(rows, now=NOW)
    with localcontext() as ctx:
        ctx.prec = 2
        ctx.traps[Inexact] = True
        actual = captured_alpha_analysis(rows, now=NOW)
    assert actual == expected


def test_actual_snapshot_api_and_positive_ev_wiring_readonly(tmp_path, monkeypatch):
    path = tmp_path / 'mission.db'
    with database(path) as db:
        db.execute('BEGIN')
        append_current_record(db, kind='ASSESSMENT', identity='alpha',
                              payload=alpha_row('.994', '.99'), recorded_at=NOW)
    before = path.read_bytes()
    monkeypatch.setenv('OVERNIGHT_PAPER_DB', str(path))
    app = FastAPI()
    app.include_router(paper_router())
    app.include_router(create_router())
    with TestClient(app) as client:
        body = client.get('/api/paper-live').json()
        alpha = body['current_research']['latest_assessment_batch']['alpha_analysis']
        assert alpha['funnel']['after_fee_positive'] == 0
        assert alpha['full_net_ev'] is None
        html = client.get('/positive-ev').text
        assert 'Gross edge funnel' in html and 'Alpha by asset' in html
        assert 'After-fee edge funnel' in html and 'Paper eligibility: No' in html
        assert html.index('alpha-discovery') < html.index('research-snapshot')
        json.dumps(body, allow_nan=False)
    assert path.read_bytes() == before
