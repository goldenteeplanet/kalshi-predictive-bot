import json
from dataclasses import replace
from datetime import timedelta

import pytest
from test_cf_process_inputs import encode, fixture
from test_settlement_target_research_router import NOW, target

from kalshi_predictor.crypto.cf_process_inputs import digest
from kalshi_predictor.crypto.current_research_intake import (
    MODEL,
    VERSION,
    prepare_current_research_forecast,
    target_binding,
)


def request():
    t = target()
    market = json.loads(t.market_original)
    market['market'].update(status='active', expiration_time=(NOW+timedelta(hours=2)).isoformat())
    t = replace(t, market_original=encode(market), market_received_at=NOW-timedelta(seconds=10))
    body, receipt = fixture()
    raw = encode(body)
    receipt['source_sha256'] = digest(raw)
    protocol = dict(
        version=VERSION, model=MODEL, target_sha256=target_binding(t, as_of=NOW),
        declared_at=(NOW-timedelta(seconds=10)).isoformat(),
        not_before=(NOW-timedelta(seconds=5)).isoformat(),
        not_after=(NOW+timedelta(seconds=30)).isoformat(),
        max_cf_gets=1, retries=0, scope='UNCALIBRATED_CURRENT_RESEARCH_ONLY',
    )
    pr = encode(protocol)
    return dict(target=t, cf_original=raw, cf_receipt=encode(receipt),
                protocol_original=pr, protocol_sha256=digest(pr), as_of=NOW)


def test_current_originals_produce_repeatable_research_only_forecast():
    r = request()
    a = prepare_current_research_forecast(**r)
    assert a == prepare_current_research_forecast(**r)
    assert 0 <= float(a['probability_yes']) <= 1
    assert not a['calibrated'] and not a['rule_certified'] and not a['paper_eligible']
    assert not a['execution_authority'] and a['database_writes'] == 0


@pytest.mark.parametrize('change', ['late_protocol', 'stale_source', 'wrong_target', 'tamper'])
def test_original_intake_rejects_unusable_provenance(change):
    r = request()
    if change == 'late_protocol':
        p = json.loads(r['protocol_original'])
        p['declared_at'] = NOW.isoformat()
        r.update(protocol_original=encode(p), protocol_sha256=digest(encode(p)))
    elif change == 'stale_source':
        r['as_of'] = NOW+timedelta(minutes=2)
    elif change == 'wrong_target':
        r['target'] = replace(r['target'], comparator='AT_OR_ABOVE')
    else:
        r['cf_original'] += b' '
    with pytest.raises(ValueError):
        prepare_current_research_forecast(**r)


def test_late_finality_allows_prospective_research_but_never_paper_horizon():
    r = request()
    market = json.loads(r['target'].market_original)
    market['market'].update(
        expiration_time=(NOW+timedelta(days=7)).isoformat(),
        expected_expiration_time=(NOW+timedelta(hours=2)).isoformat(),
    )
    r['target'] = replace(r['target'], market_original=encode(market))
    p = json.loads(r['protocol_original'])
    p['target_sha256'] = target_binding(r['target'], as_of=NOW)
    r.update(protocol_original=encode(p), protocol_sha256=digest(encode(p)))
    result = prepare_current_research_forecast(**r)
    assert result['research_horizon_basis'] == 'ORIGINAL_BOUND_OBSERVATION_CLOSE'
    assert result['paper_horizon_verified'] is False
    assert result['paper_eligible'] is False
    assert result['paper_horizon_blocker'] == 'CERTIFIED_FINAL_SETTLEMENT_BOUND_REQUIRED'
