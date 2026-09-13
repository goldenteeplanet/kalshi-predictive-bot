import copy
import hashlib
import json
import math

import pytest

from kalshi_predictor.overnight_paper.alpha_tournament import read_tournament, score_tournament
from kalshi_predictor.ui.alpha_lab import render_tournament


def inputs():
    scores = []
    rows = []
    for decision, probability, outcome in [('a', '.3', 0), ('b', '.7', 1)]:
        p = float(probability)
        scores.append(dict(decision_id=decision, model='model', ticker=decision, asset='BTC',
                           event='event', rule_version='endpoint', score=dict(
                           probability=probability, outcome=outcome, brier='.09',
                           log_loss=-math.log(p if outcome else 1-p), probability_clipped=False)))
        from decimal import Decimal
        for side in ('YES', 'NO'):
            side_p = Decimal(probability) if side == 'YES' else Decimal(str(1-p))
            price = Decimal('.2') if side == 'YES' else Decimal('.8')
            gross = side_p-price
            rows.append(dict(decision_id=decision, model='model', ticker=decision,
                             side=side, rule_version='endpoint', gross=str(gross),
                             fee=dict(price=str(price), value='.02'),
                             gross_minus_supported_fee=str(gross-Decimal('.02')),
                             paper_eligible=False, full_net_ev=None))
    return dict(rows=rows), dict(rows=scores, execution_authority=False)


def test_join_price_segments_and_count_forecasts_once():
    analysis, evaluation = inputs()
    before = copy.deepcopy((analysis, evaluation))
    result = score_tournament(analysis, evaluation)
    assert result['model_forecasts'] == 2 and result['event_n'] == 1
    assert result['scored_side_rows'] == 4
    assert len(result['leaderboard']) == 2
    yes, = [r for r in result['leaderboard'] if r['price_band'] == '10-25c']
    assert yes['forecast_n'] == 2 and yes['event_n'] == 1
    assert yes['brier'] == '0.09' and yes['accuracy'] == 1
    assert yes['after_fee_thresholds_cents']['5'] == 2
    assert yes['mean_hypothetical_return'] is not None
    assert yes['dependency_group_n'] is None and result['holdout_validated'] is False
    assert (analysis, evaluation) == before


@pytest.mark.parametrize('mutation', ['duplicate_score','duplicate_side','wrong_model',
                                    'wrong_ticker','wrong_endpoint','wrong_brier','wrong_loss',
                                    'wrong_edge','wrong_fee','paper','clipped'])
def test_contradictory_evidence_refuses(mutation):
    a, e = inputs()
    if mutation == 'duplicate_score': e['rows'].append(copy.deepcopy(e['rows'][0]))
    if mutation == 'duplicate_side': a['rows'].append(copy.deepcopy(a['rows'][0]))
    if mutation == 'wrong_model': a['rows'][0]['model'] = 'other'
    if mutation == 'wrong_ticker': a['rows'][0]['ticker'] = 'other'
    if mutation == 'wrong_endpoint': a['rows'][0]['rule_version'] = 'other'
    if mutation == 'wrong_brier': e['rows'][0]['score']['brier'] = '.08'
    if mutation == 'wrong_loss': e['rows'][0]['score']['log_loss'] = .1
    if mutation == 'wrong_edge': a['rows'][0]['gross'] = '.11'
    if mutation == 'wrong_fee': a['rows'][0]['fee']['value'] = '.01'
    if mutation == 'paper': a['rows'][0]['paper_eligible'] = True
    if mutation == 'clipped': e['rows'][0]['score']['probability_clipped'] = True
    with pytest.raises(ValueError): score_tournament(a, e)


def test_no_fee_does_not_turn_missing_economics_into_zero():
    a, e = inputs()
    for row in a['rows']:
        row['gross_minus_supported_fee'] = None
        row['fee'] = None
    result = score_tournament(a, e)
    segment, = result['leaderboard']
    assert segment['price_band'] == 'UNKNOWN'
    assert segment['fee_supported_side_n'] == 0 and segment['best_after_fee_edge'] is None
    assert segment['forecast_n'] == 2


def test_reader_requires_exact_pins_and_escapes_rendered_metadata(tmp_path):
    a, e = inputs()
    manifest = dict(schema='ALPHA_TOURNAMENT_INPUTS_V1', files={},
                    horizon='5-minute lead', cohort='<script>unsafe</script>')
    for name, data in [('analysis.json', a), ('evaluation.json', e)]:
        raw = json.dumps(data).encode(); (tmp_path/name).write_bytes(raw)
        manifest['files'][name] = hashlib.sha256(raw).hexdigest()
    raw = json.dumps(manifest).encode();(tmp_path/'manifest.json').write_bytes(raw)
    pin = hashlib.sha256(raw).hexdigest()
    report = read_tournament(tmp_path, pin)
    assert report['status'] == 'RETAINED_ARITHMETIC_CHECKED'
    html = render_tournament(report)
    assert '<script>' not in html and '&lt;script&gt;' in html
    assert 'Hypothetical return' in html and 'INSUFFICIENT_DATA' in html
    assert read_tournament(tmp_path, '0'*64)['status'] == 'UNAVAILABLE_OR_INVALID'
    (tmp_path/'evaluation.json').write_text('{}')
    assert read_tournament(tmp_path, pin)['status'] == 'UNAVAILABLE_OR_INVALID'
