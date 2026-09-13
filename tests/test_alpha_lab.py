import copy
from datetime import timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_alpha_analysis import alpha_row
from test_current_assessment_view import NOW, envelopes
from test_current_research_dashboard import database

from kalshi_predictor.overnight_paper.alpha_analysis import captured_alpha_analysis
from kalshi_predictor.overnight_paper.current_research_store import append_current_record
from kalshi_predictor.ui.alpha_lab import create_router, lab_snapshot, leaderboard, render_lab


def analyzed(*rows):
    return captured_alpha_analysis(envelopes(*rows), now=NOW)


@pytest.mark.parametrize('price,band', [('.01', '1-10c'), ('.099', '1-10c'),
    ('.1', '10-25c'), ('.25', '25-40c'), ('.4', '40-60c'), ('.6', '60-75c'),
    ('.75', '75-90c'), ('.9', '90-99c'), ('.99', '90-99c'),
    ('0', 'OUTSIDE_DECLARED_BANDS'), ('1', 'OUTSIDE_DECLARED_BANDS')])
def test_side_executable_price_bands_not_probability(price, band):
    alpha = analyzed(alpha_row('.8', price))
    assert alpha['rows'][0]['price_band'] == band
    assert alpha['rows'][0]['probability_band'] == '75-90%'
    assert alpha['by_price'][band]['forecast_ready_sides'] == 1


def test_complete_strict_after_fee_funnel():
    # After-fee values exactly 0, 2, 5, 7, 10 and 11 cents.
    rows = [alpha_row(p=p, ask='.5', ticker=str(i)) for i, p in
            enumerate(('.51', '.53', '.56', '.58', '.61', '.62'))]
    funnel = analyzed(*rows)['funnel']
    assert funnel['after_fee_thresholds_cents'] == {'0': 5, '2': 4, '5': 3, '7': 2, '10': 1}
    assert funnel['after_fee_gt_5c'] == 3


def test_two_sides_one_forecast_missing_fee_denominator_and_no_certification():
    yes = alpha_row('.6', '.5', side='YES')
    no = alpha_row('.4', '.5', side='NO')
    no.update(fee=None, after_fee=None, after_execution=None)
    alpha = analyzed(yes, no)
    before = copy.deepcopy(alpha)
    row, = leaderboard(alpha)
    assert row['forecast_n'] == 1 and row['executable_side_n'] == 2
    assert row['fee_supported_side_n'] == 1
    assert row['gross_positive_pct'] == '50' and row['after_fee_positive_pct'] == '100'
    assert row['status'] == 'INSUFFICIENT_DATA' and row['paper_eligible'] is False
    assert row['event_n'] is row['dependency_group_n'] is row['brier'] is None
    assert alpha == before


def test_stale_economics_no_current_opportunity_or_performance():
    alpha = captured_alpha_analysis(envelopes(alpha_row()), now=NOW+timedelta(hours=1))
    current = {'latest_assessment_batch': {'alpha_analysis': alpha}}
    result = lab_snapshot(current)
    assert result['freshness'] == 'STALE' and result['full_net_ev'] is None
    assert result['predictive_quality'] == 'OUTCOMES_NOT_JOINED'
    assert result['leaderboard'][0]['hypothetical_executable_return'] is None
    html = render_lab(current)
    assert 'No segment is holdout validated' in html and 'STALE' in html


def test_empty_page_and_html_escaping():
    assert 'No checked forecast assessments available' in render_lab({})
    alpha = analyzed(alpha_row())
    alpha['rows'][0]['model'] = '<script>alert(1)</script>'
    html = render_lab({'latest_assessment_batch': {'alpha_analysis': alpha}})
    assert '<script>' not in html and '&lt;script&gt;' in html


def test_real_snapshot_routes_do_not_mutate(tmp_path, monkeypatch):
    path = tmp_path/'mission.db'
    with database(path) as db:
        db.execute('BEGIN')
        append_current_record(db, kind='ASSESSMENT', identity='alpha',
                              payload=alpha_row('.994', '.99'), recorded_at=NOW)
    before = path.read_bytes()
    monkeypatch.setenv('OVERNIGHT_PAPER_DB', str(path))
    app = FastAPI(); app.include_router(create_router())
    with TestClient(app) as client:
        response = client.get('/api/alpha-lab')
        assert response.status_code == 200
        report = response.json()
        assert report['alpha_funnel']['after_fee_thresholds_cents']['0'] == 0
        assert report['leaderboard'][0]['price_band'] == '90-99c'
        assert report['paper_eligible'] is False
        response = client.get('/alpha-lab')
        assert response.status_code == 200 and 'Alpha Lab' in response.text
        assert 'Predictive quality' in response.text and 'Trading value' in response.text
        assert '-0.6000c' in response.text
    assert path.read_bytes() == before
