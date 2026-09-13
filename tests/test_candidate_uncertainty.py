"""Negative applicability proofs; synthetic conditional fixtures grant no scope."""

import copy
import hashlib

import pytest
from test_current_calibration_evidence import request

from kalshi_predictor.crypto import calibration_cost_evidence as calibration
from kalshi_predictor.crypto.candidate_uncertainty import BLOCKER, verify_candidate_uncertainty
from kalshi_predictor.crypto.cost_record import (
    build_cost_record,
    replay_candidate_cost_record,
    replay_cost_record,
)


def inputs():
    policy, args = request()
    original = args['originals']
    decision = {k: args[k] for k in ('ticker', 'event_id', 'series', 'model_version')}
    decision.update(segment=original.segment, side='BUY_YES',
                    selected_probability=str(args['selected_probability']),
                    executable_price=str(args['executable_price']),
                    decision_at=args['decision_at'].isoformat())
    return policy, dict(decision=decision, policy_version=original.policy_version,
                        dataset=original.dataset, protocol=original.protocol,
                        independence_review=original.independence_review), args


def test_real_conditional_support_cannot_grant_candidate_allowance(monkeypatch):
    policy, data, _ = inputs()
    monkeypatch.setattr(calibration, 'REVIEWED_CALIBRATION_POLICIES', (policy,))
    result = verify_candidate_uncertainty(**data)
    assert result['conditional_evidence_supported'] is True
    assert result['value'] is None and result['status'] == 'UNKNOWN'
    assert result['candidate_applicability'] is result['paper_support'] is False
    assert BLOCKER in result['blockers']
    assert result['original_receipts_replayed'] is False


@pytest.mark.parametrize('field,value', [
    ('ticker', 'OTHER'), ('event_id', 'OTHER'), ('side', 'BUY_NO'),
    ('model_version', 'OTHER'), ('segment', 'OTHER'), ('selected_probability', '0.1'),
    ('rule_version', 'FORGED'), ('selection_policy', 'FORGED'),
    ('paper_support', True), ('independent_n', 999999), ('value', '0'),
])
def test_scope_changes_and_claimed_labels_never_pass(monkeypatch, field, value):
    policy, data, _ = inputs()
    monkeypatch.setattr(calibration, 'REVIEWED_CALIBRATION_POLICIES', (policy,))
    before = verify_candidate_uncertainty(**data)
    data['decision'][field] = value
    after = verify_candidate_uncertainty(**data)
    assert before['candidate_sha256'] != after['candidate_sha256']
    assert after['value'] is None and after['candidate_applicability'] is False


@pytest.mark.parametrize('change', ['missing', 'tamper', 'malformed', 'clock', 'nonfinite'])
def test_missing_altered_malformed_evidence_and_inputs_remain_unknown(monkeypatch, change):
    policy, data, _ = inputs()
    if change == 'missing':
        data['policy_version'] = None
    elif change == 'tamper':
        data['dataset'] += b' '
    elif change == 'malformed':
        from dataclasses import replace
        data['dataset'] = b'{'
        policy = replace(policy, dataset_sha256=hashlib.sha256(b'{').hexdigest())
        data['policy_version'] = policy.version
    elif change == 'clock':
        data['decision']['decision_at'] = '2026-09-13T00:00:00'
    else:
        data['decision']['selected_probability'] = 'NaN'
    monkeypatch.setattr(calibration, 'REVIEWED_CALIBRATION_POLICIES', (policy,))
    result = verify_candidate_uncertainty(**data)
    assert result['conditional_evidence_supported'] is False
    assert result['value'] is None and not result['paper_support']
    assert result['original_sha256']['dataset'] == hashlib.sha256(data['dataset']).hexdigest()


def test_guarded_cost_replay_preserves_conditional_record_but_blocks_applicability(monkeypatch):
    policy, data, args = inputs()
    monkeypatch.setattr(calibration, 'REVIEWED_CALIBRATION_POLICIES', (policy,))
    record = build_cost_record(
        decision=data['decision'], side='YES', selected_probability=args['selected_probability'],
        executable_price=args['executable_price'], books=(args['book'],),
        public_paper_fee_originals=args['fee_originals'],
        public_paper_assessed_at=args['decision_at'],
        calibration_policy_version=data['policy_version'], calibration_dataset=data['dataset'],
        calibration_protocol=data['protocol'], independence_review=data['independence_review'])
    before = copy.deepcopy(record)
    conditional = replay_cost_record(record, expected_decision=data['decision'])
    assert conditional['uncertainty']['paper_support'] is True
    assert conditional['full_net_ev'] is not None
    result = replay_candidate_cost_record(record, expected_decision=data['decision'])
    assert result['conditional_assessment'] == conditional
    assert result['full_net_ev'] is result['uncertainty']['value'] is None
    assert result['clears_net_gate'] is result['uncertainty']['paper_support'] is False
    assert record == before
    record['assessment']['candidate_applicability'] = True
    with pytest.raises(ValueError, match='RECOMPUTATION_MISMATCH'):
        replay_candidate_cost_record(record, expected_decision=data['decision'])


def test_scanner_and_assembler_use_candidate_boundary(monkeypatch):
    from test_cf_candidate_assembly import preparation
    from test_current_calibration_evidence import fixture
    from test_current_market_scan import intake_scan_request

    from kalshi_predictor.crypto.current_market_scan import evaluate_paginated_current_research
    from kalshi_predictor.crypto.current_research_intake import MODEL
    from kalshi_predictor.overnight_paper.cf_candidate_assembly import (
        assemble_cf_research_candidate,
    )

    args = intake_scan_request()
    ticker = next(iter(args['research_inputs']))
    policy, bundle = fixture(args['assessed_at'], MODEL)
    monkeypatch.setattr(calibration, 'REVIEWED_CALIBRATION_POLICIES', (policy,))
    args['calibration_originals'] = {ticker: bundle}
    rows = evaluate_paginated_current_research(**args)['rows']
    for row in rows:
        if row['candidate_uncertainty_applicability']:
            assert not row['candidate_uncertainty_applicability']['candidate_applicability']
            assert BLOCKER in row['blockers']
            assert row['uncertainty'] is row['full_net_ev'] is None
    paper, provenance = preparation()
    result = assemble_cf_research_candidate(paper_decision=paper, provenance_args=provenance)
    applicability = result.shadow_payload['candidate_uncertainty_applicability']
    assert not applicability['candidate_applicability']
    assert result.qualification_args['ev'] is None
