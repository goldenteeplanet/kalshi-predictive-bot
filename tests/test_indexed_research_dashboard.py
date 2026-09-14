import json
import sqlite3
from datetime import timedelta, timezone

import pytest
from test_current_assessment_view import record
from test_current_research_dashboard import NOW, database, scan
from test_current_research_store import evaluation_payload

from kalshi_predictor.overnight_paper.current_research_dashboard import current_research_snapshot
from kalshi_predictor.overnight_paper.current_research_index import research_validation_session
from kalshi_predictor.overnight_paper.current_research_store import append_current_record
from kalshi_predictor.overnight_paper.indexed_research_dashboard import (
    indexed_current_research_snapshot,
)


def append(db, kind, identity, payload, at):
    return append_current_record(db, kind=kind, identity=identity, payload=payload, recorded_at=at)


def parity(path, index, now):
    with sqlite3.connect(path) as db:
        expected = current_research_snapshot(db, now=now)
    with research_validation_session(path, index) as session:
        observed = indexed_current_research_snapshot(session, now=now)
        assert observed == expected
        assert observed['runtime_verified'] is False and observed['paper_eligible'] is False
        assert observed['full_net_point_estimate_status'] == 'UNKNOWN'
        return observed


@pytest.mark.parametrize('case', ['empty', 'no_scan', 'latest_assessment_invalid', 'scan_tie'])
def test_complete_projection_parity(tmp_path, case):
    path = tmp_path / 'db'
    with database(path) as db:
        db.execute('BEGIN')
        if case in ('no_scan', 'latest_assessment_invalid'):
            payload = record()
            if case == 'latest_assessment_invalid':
                payload['gross_edge'] = '0.8'
            append(db, 'ASSESSMENT', 'a', payload, NOW)
        if case == 'scan_tie':
            append(db, 'SCAN', 'first', scan(NOW, 3), NOW)
            append(db, 'SCAN', 'second', scan(NOW, 9), NOW)
    result = parity(path, tmp_path / 'index', NOW + timedelta(hours=1))
    if case == 'latest_assessment_invalid':
        assert result['latest_assessment_batch']['status'] == 'INVALID_OR_AMBIGUOUS'
    if case == 'scan_tie':
        assert result['latest_scan_freshness'] == 'STALE'
        with sqlite3.connect(path) as db:
            latest = json.loads(db.execute(
                'SELECT payload FROM overnight_sprint_cycles ORDER BY id DESC LIMIT 1'
            ).fetchone()[0])
        assert result['latest_scan_funnel'] == latest['record']['funnel']


def lifecycle(path, states, evaluated=False):
    payload = evaluation_payload()
    decision, evaluation = payload['decision'], payload['evaluation']
    with database(path) as db:
        db.execute('BEGIN')
        append(db, 'PROSPECTIVE_SHADOW', decision['decision_id'],
               {**decision, 'execution_authority': False}, NOW)
        if evaluated:
            append(db, 'EVALUATION', decision['decision_id'], payload, NOW + timedelta(hours=3))
        for state, at in states:
            observation = dict(
                decision_id=decision['decision_id'], state=state,
                official_original_json=evaluation['official_original_json'],
                official_receipt_json=evaluation['official_receipt_json'],
                observed_at=evaluation['evaluated_at'], paper_eligible=False,
                execution_authority=False,
            )
            append(db, 'SHADOW_OBSERVATION', decision['decision_id'] + ':' + state,
                   observation, at)


@pytest.mark.parametrize('state', ['OPEN', 'CLOSED', 'AWAITING_FINAL', 'FINAL', 'EVALUATED'])
def test_lifecycle_state_counts_match_originals(tmp_path, state):
    path = tmp_path / 'db'
    observations = [] if state in ('OPEN', 'EVALUATED') else [(state, NOW + timedelta(hours=4))]
    lifecycle(path, observations, evaluated=state == 'EVALUATED')
    result = parity(path, tmp_path / 'index', NOW + timedelta(hours=8))
    assert result['shadow_state_counts'][state] == 1
    assert sum(result['shadow_state_counts'].values()) == 1


@pytest.mark.parametrize('evaluated', [False, True])
def test_raw_text_order_not_physical_time_and_evaluation_is_absorbing(tmp_path, evaluated):
    path = tmp_path / 'db'
    later_physical = (NOW + timedelta(hours=5)).astimezone(timezone(timedelta(hours=-2)))
    earlier_physical = NOW + timedelta(hours=4)
    lifecycle(path, [('AWAITING_FINAL', later_physical), ('CLOSED', earlier_physical)], evaluated)
    result = parity(path, tmp_path / 'index', NOW + timedelta(hours=8))
    assert result['shadow_state_counts']['EVALUATED' if evaluated else 'CLOSED'] == 1


@pytest.mark.parametrize('case', ['old_scan_counts', 'scan_clock', 'scan_parse',
                                 'earlier_scan_error', 'earlier_future', 'naive_now'])
def test_dashboard_only_failure_order_matches_oracle_without_invalidating_session(tmp_path, case):
    path = tmp_path / 'db'
    bad = scan(NOW, 1)
    bad['funnel']['markets_scanned'] = True
    now = NOW + timedelta(hours=1)
    with database(path) as db:
        db.execute('BEGIN')
        if case in ('old_scan_counts', 'earlier_scan_error'):
            append(db, 'SCAN', 'bad', bad, NOW)
            append(db, 'SCAN', 'valid-later', scan(NOW, 1), NOW + timedelta(minutes=1))
        elif case == 'earlier_future':
            append(db, 'ASSESSMENT', 'future', record(), NOW + timedelta(hours=2))
            append(db, 'SCAN', 'bad', bad, NOW + timedelta(hours=3))
        elif case in ('scan_clock', 'scan_parse'):
            bad = scan(NOW, 1)
            bad['decision_time'] = NOW.isoformat()
            bad['assessed_at'] = (
                (NOW + timedelta(hours=1)).isoformat() if case == 'scan_clock' else 'invalid'
            )
            append(db, 'SCAN', 'bad', bad, NOW)
        if case == 'earlier_scan_error':
            append(db, 'ASSESSMENT', 'future', record(), NOW + timedelta(hours=2))
    if case == 'naive_now':
        now = NOW.replace(tzinfo=None)
    with sqlite3.connect(path) as db:
        with pytest.raises((ValueError, KeyError, AttributeError)) as expected:
            current_research_snapshot(db, now=now)
    with research_validation_session(path, tmp_path / 'index') as session:
        assert session.manifest.status == 'COMPLETE_VALIDATED_SNAPSHOT'
        with pytest.raises(type(expected.value)) as actual:
            indexed_current_research_snapshot(session, now=now)
        assert str(actual.value) == str(expected.value)


def test_latest_scan_only_fetch_and_closed_expired_refusals(tmp_path, monkeypatch):
    path = tmp_path / 'db'
    with database(path) as db:
        db.execute('BEGIN')
        for i in range(10):
            at = NOW + timedelta(seconds=i)
            append(db, 'SCAN', str(i), scan(at, i), at)
    with research_validation_session(path, tmp_path / 'index') as session:
        calls = []
        original = session.read_original

        def read(key):
            calls.append(key)
            return original(key)

        monkeypatch.setattr(session, 'read_original', read)
        indexed_current_research_snapshot(session, now=NOW + timedelta(hours=1))
        assert len(calls) == 1

        def expired():
            raise ValueError('INDEX_TIME_BOUND')

        monkeypatch.setattr(session, '_deadline', expired)
        with pytest.raises(ValueError, match='INDEX_TIME_BOUND'):
            session.dashboard_summary(now=NOW)
    with pytest.raises(ValueError, match='INDEX_SESSION_CLOSED'):
        indexed_current_research_snapshot(session, now=NOW)


def test_latest_scan_original_hash_and_compact_metadata_must_match(tmp_path):
    path = tmp_path / 'db'
    with database(path) as db:
        db.execute('BEGIN')
        key = append(db, 'SCAN', 'scan', scan(NOW, 1), NOW)
    with research_validation_session(path, tmp_path / 'index') as session:
        # Private fault injection; the public API offers no SQL connection.
        session._index.execute('PRAGMA query_only=OFF')
        session._index.execute('UPDATE records SET raw_sha=? WHERE id=?', ('f' * 64, key))
        with pytest.raises(ValueError, match='INDEX_ORIGINAL_HASH_MISMATCH'):
            indexed_current_research_snapshot(session, now=NOW)


def test_scan_metadata_mismatch_refuses_projection(tmp_path):
    path = tmp_path / 'db'
    with database(path) as db:
        db.execute('BEGIN')
        key = append(db, 'SCAN', 'scan', scan(NOW, 1), NOW)
    with research_validation_session(path, tmp_path / 'index') as session:
        session._index.execute('PRAGMA query_only=OFF')
        session._index.execute('UPDATE dashboard_metadata SET scan_clock=? WHERE id=?',
                               ((NOW - timedelta(seconds=1)).isoformat(), key))
        with pytest.raises(ValueError, match='INDEX_SCAN_METADATA_MISMATCH'):
            indexed_current_research_snapshot(session, now=NOW)
