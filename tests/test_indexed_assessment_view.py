import json
from datetime import timedelta, timezone
from decimal import Inexact, localcontext

import pytest
from test_current_assessment_view import NOW, record
from test_current_research_dashboard import database

from kalshi_predictor.overnight_paper.current_assessment_view import latest_assessment_batch
from kalshi_predictor.overnight_paper.current_research_index import research_validation_session
from kalshi_predictor.overnight_paper.current_research_store import (
    append_current_record,
    read_current_records,
)
from kalshi_predictor.overnight_paper.indexed_assessment_view import indexed_latest_assessment_batch


def make(tmp_path, rows):
    path = tmp_path / 'mission.sqlite'
    with database(path) as db:
        db.execute('BEGIN')
        for i, row in enumerate(rows):
            append_current_record(db, kind='ASSESSMENT', identity=str(i), payload=row,
                                  recorded_at=NOW + timedelta(seconds=i))
        expected = read_current_records(db)
    return path, expected


@pytest.mark.parametrize('case', ['empty', 'arithmetic', 'missing', 'unsupported', 'duplicate',
                                 'conflict', 'hash', 'future', 'stale', 'invalid_clock'])
def test_real_journal_oracle_parity(tmp_path, case):
    rows = [record()]
    now = NOW
    if case == 'empty':
        rows = []
    elif case == 'arithmetic':
        rows[0]['gross_edge'] = '0.9'
    elif case == 'missing':
        rows[0]['forecast_probability'] = None
    elif case == 'unsupported':
        rows[0]['fee']['status'] = 'UNKNOWN'
    elif case == 'duplicate':
        rows *= 2
    elif case == 'conflict':
        rows.append(record(executable_price='0.65'))
    elif case == 'hash':
        rows.append(record(scan_sha256='c' * 64))
    elif case == 'future':
        now -= timedelta(seconds=1)
    elif case == 'stale':
        now += timedelta(hours=1)
    elif case == 'invalid_clock':
        rows[0]['decision_time'] = NOW.isoformat()
        del rows[0]['assessed_at']
    path, originals = make(tmp_path, rows)
    before = path.read_bytes()
    with research_validation_session(path, tmp_path / 'index') as session:
        result = indexed_latest_assessment_batch(session, now=now)
        assert result == latest_assessment_batch(originals, now=now)
        assert result['original_replay'] is False
        assert result['batch_completeness'] == 'UNKNOWN'
        assert result['full_net_ev'] is None and result['paper_eligible'] is False
    assert path.read_bytes() == before


@pytest.mark.parametrize('count', [600, 601])
def test_exact_batch_bound_without_loading_oversized_batch(tmp_path, count, monkeypatch):
    path, originals = make(tmp_path, [record(ticker=f'BTC-{i}') for i in range(count)])
    with research_validation_session(path, tmp_path / 'index') as session:
        reads = []
        original_read = session.read_original

        def read(key):
            reads.append(key)
            return original_read(key)

        monkeypatch.setattr(session, 'read_original', read)
        assert indexed_latest_assessment_batch(session, now=NOW) == latest_assessment_batch(
            originals, now=NOW,
        )
        assert len(reads) == (600 if count == 600 else 0)


def test_physical_clock_tie_preserves_first_original_offset_and_no_history_fetch(tmp_path):
    first_clock = NOW.astimezone(timezone(timedelta(hours=-5))).isoformat()
    rows = [record(assessed_at=(NOW - timedelta(hours=1)).isoformat(), ticker='OLD'),
            record(assessed_at=first_clock, ticker='FIRST'), record(ticker='SECOND')]
    path, originals = make(tmp_path, rows)
    with research_validation_session(path, tmp_path / 'index') as session:
        ids = session.latest_assessment_ids()
        assert len(ids) == 2
        assert [json.loads(session.read_original(key))['record']['ticker'] for key in ids] == [
            'FIRST', 'SECOND',
        ]
        result = indexed_latest_assessment_batch(session, now=NOW)
        assert result == latest_assessment_batch(originals, now=NOW)
        assert result['assessed_at'] == NOW.isoformat()  # aware() canonicalizes to UTC.


def test_hostile_decimal_context_and_lifetime_refusal(tmp_path):
    path, originals = make(tmp_path, [record()])
    with research_validation_session(path, tmp_path / 'index') as session:
        with localcontext() as context:
            context.prec = 3
            context.traps[Inexact] = True
            assert indexed_latest_assessment_batch(session, now=NOW) == latest_assessment_batch(
                originals, now=NOW,
            )
    with pytest.raises(ValueError, match='INDEX_SESSION_CLOSED'):
        indexed_latest_assessment_batch(session, now=NOW)


def test_original_failure_propagates_instead_of_unavailable(tmp_path, monkeypatch):
    path, _ = make(tmp_path, [record()])
    with research_validation_session(path, tmp_path / 'index') as session:
        def refuse(key):
            raise ValueError('INDEX_ORIGINAL_HASH_MISMATCH')

        monkeypatch.setattr(session, 'read_original', refuse)
        with pytest.raises(ValueError, match='INDEX_ORIGINAL_HASH_MISMATCH'):
            indexed_latest_assessment_batch(session, now=NOW)


def test_expired_session_refuses_even_an_empty_projection(tmp_path, monkeypatch):
    path, _ = make(tmp_path, [])
    with research_validation_session(path, tmp_path / 'index') as session:
        def expired():
            raise ValueError('INDEX_TIME_BOUND')

        monkeypatch.setattr(session, '_deadline', expired)
        with pytest.raises(ValueError, match='INDEX_TIME_BOUND'):
            indexed_latest_assessment_batch(session, now=NOW)
