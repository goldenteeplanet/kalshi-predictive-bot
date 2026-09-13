"""Real conditional replay remains historical; view projection cannot certify it."""

import hashlib
import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import text
from test_current_calibration_evidence import fixture, request
from test_overnight_activation import baseline_template  # noqa: F401
from test_paper_release_dataset_store import factory  # noqa: F401

from kalshi_predictor.crypto import calibration_cost_evidence as calibration
from kalshi_predictor.crypto.cost_record import build_cost_record, cost_decision_from_qualification
from kalshi_predictor.overnight_paper.coordinator import _checkpoint
from kalshi_predictor.overnight_paper.dashboard import render, snapshot
from kalshi_predictor.overnight_paper.qualification import GATE_NAMES, decision_fingerprint
from kalshi_predictor.overnight_paper.research_record import PREFIX, research_record


def seed(factory, monkeypatch):  # noqa: F811
    _, args = request()
    policy, originals = fixture(args['decision_at'], 'a' * 64)
    # Synthetic independent labels exercise the real conditional estimator only.
    # They are not original-source or candidate-applicability certification.
    templates = json.loads(originals.dataset)
    dataset = json.dumps([
        templates[i % 2] | {'event': f'synthetic-{i}', 'cluster_id': f'cluster-{i}'}
        for i in range(1000)
    ]).encode()
    policy = replace(policy, dataset_sha256=hashlib.sha256(dataset).hexdigest(),
                     minimum_independent_events=1000)
    originals = replace(originals, dataset=dataset, policy_version=policy.version)
    monkeypatch.setattr(calibration, 'REVIEWED_CALIBRATION_POLICIES', (policy,))
    inputs = dict(ticker=args['ticker'], event_id=args['event_id'], series=args['series'],
                  side='BUY_YES', forecast_probability=str(args['selected_probability']),
                  executable_price=str(args['executable_price']), model_artifact_sha256='a' * 64,
                  calibration_segment=originals.segment,
                  decision_at=args['decision_at'].isoformat())
    scope = cost_decision_from_qualification(inputs)
    costs = build_cost_record(
        decision=scope, side='YES', selected_probability=args['selected_probability'],
        executable_price=args['executable_price'], books=(args['book'],),
        public_paper_fee_originals=args['fee_originals'],
        public_paper_assessed_at=args['decision_at'],
        calibration_policy_version=originals.policy_version,
        calibration_dataset=originals.dataset, calibration_protocol=originals.protocol,
        independence_review=originals.independence_review)
    assert costs['assessment']['uncertainty']['paper_support'] is True
    assert Decimal(costs['assessment']['full_net_ev']) > Decimal('.05')
    identity = decision_fingerprint(inputs)
    checkpoint = dict(kind='PAPER_RELEASE_QUALIFICATION', decision_inputs=inputs,
                      qualification=dict(decision_id=identity,
                                         gates=[[g, True] for g in GATE_NAMES],
                                         blockers=[], net_ev=None, status='PAPER_NOT_READY'),
                      shadow_payload={'cost_record': costs})
    legacy = research_record(checkpoint)
    assert legacy['full_net_ev_status'] == 'FULL_NET_EV_KNOWN'
    with factory() as session:
        _checkpoint(session, 'release-qualification:' + identity, args['decision_at'], checkpoint)
        _checkpoint(session, PREFIX + identity, args['decision_at'], legacy)
        session.commit()
    return Path(factory.kw['bind'].url.database), checkpoint, legacy


def test_supported_conditional_original_preserved_but_view_unknown(factory, monkeypatch):  # noqa: F811
    path, checkpoint, legacy = seed(factory, monkeypatch)
    original = path.read_bytes()
    result = snapshot(path)
    assert path.read_bytes() == original
    assert result['research_assessment_count'] == 1
    view = result['latest_research_assessment']
    assert view['historical_conditional_diagnostic'] == legacy == research_record(checkpoint)
    assert view['full_net_ev'] is None and view['full_net_ev_status'] == 'FULL_NET_EV_UNKNOWN'
    assert view['status'] == 'CANDIDATE_APPLICABILITY_UNKNOWN'
    assert view['candidate_applicability'] is view['paper_eligible'] is False
    assert view['execution_authority'] is False
    assert 'cost_assessment' not in view
    assert result['qualification_current'] is False
    page = render(result)
    assert 'Canonical candidate full net EV remains unknown' in page
    assert 'nested historical conditional diagnostic' in page
    with factory() as session:
        saved = json.loads(session.execute(text(
            "SELECT payload FROM overnight_sprint_cycles WHERE id LIKE 'release-research-v1:%'"
        )).scalar_one())
    assert saved == legacy
    assert 'historical_conditional_diagnostic' not in saved


@pytest.mark.parametrize('tamper', ['net', 'missing_checkpoint'])
def test_projection_does_not_hide_legacy_lineage_failure(factory, monkeypatch, tamper):  # noqa: F811
    path, _, _ = seed(factory, monkeypatch)
    with factory() as session:
        if tamper == 'missing_checkpoint':
            session.execute(text(
                "DELETE FROM overnight_sprint_cycles WHERE id LIKE 'release-qualification:%'"))
        else:
            key, raw = session.execute(text(
                "SELECT id,payload FROM overnight_sprint_cycles "
                "WHERE id LIKE 'release-research-v1:%'"
            )).one()
            changed = json.loads(raw)
            changed['full_net_ev'] = '0.99'
            session.execute(text('UPDATE overnight_sprint_cycles SET payload=:p WHERE id=:id'),
                            {'p': json.dumps(changed), 'id': key})
        session.commit()
    result = snapshot(path)
    assert result['paper_mode'] == 'UNVERIFIED'
    assert result['latest_research_assessment'] is None
    assert result['research_assessment_count'] is None
