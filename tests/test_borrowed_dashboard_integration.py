import sqlite3
import subprocess
import sys
from contextlib import contextmanager
from datetime import timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_current_assessment_view import envelopes, record
from test_current_research_dashboard import NOW, add_scan, database

from kalshi_predictor.overnight_paper import dashboard, dashboard_scratch
from kalshi_predictor.overnight_paper.alpha_analysis import captured_alpha_analysis
from kalshi_predictor.overnight_paper.current_research_dashboard import current_research_snapshot
from kalshi_predictor.overnight_paper.current_research_index import (
    borrowed_research_validation_session,
)
from kalshi_predictor.overnight_paper.current_research_store import append_current_record


def mission(tmp_path):
    path = tmp_path / 'db'
    with database(path) as db:
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('BEGIN')
        add_scan(db, NOW, 2, 'scan')
        append_current_record(
            db, kind='ASSESSMENT', identity='a', payload=record(), recorded_at=NOW,
        )
    return path


@pytest.fixture(autouse=True)
def private_scratch(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard_scratch.tempfile, 'gettempdir', lambda: str(tmp_path))


@pytest.mark.parametrize('outcome', ['success', 'consumer_error', 'deadline', 'invalid_original'])
def test_borrowed_owner_keeps_connection_transaction_row_factory_and_callbacks(tmp_path, outcome):
    path = mission(tmp_path)
    if outcome == 'invalid_original':
        with sqlite3.connect(path) as edit:
            edit.execute("UPDATE overnight_sprint_cycles SET payload='{}'")
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        authorizer_calls, progress_calls = [], []
        db.set_authorizer(lambda *args: authorizer_calls.append(args[0]) or sqlite3.SQLITE_OK)
        db.set_progress_handler(lambda: progress_calls.append(1) or 0, 1)

        def deadline():
            if outcome == 'deadline':
                raise ValueError('CALLER_DEADLINE')

        try:
            with borrowed_research_validation_session(
                db, tmp_path / 'index', caller_deadline=deadline,
            ) as session:
                assert session.manifest.record_count == 2
                if outcome == 'consumer_error':
                    raise RuntimeError('CONSUMER_ERROR')
        except (ValueError, RuntimeError):
            assert outcome != 'success'
        else:
            assert outcome == 'success'
        assert db.in_transaction and db.row_factory is sqlite3.Row
        before = (len(authorizer_calls), len(progress_calls))
        assert db.execute('SELECT count(*) FROM overnight_sprint_cycles').fetchone()[0] == 2
        assert len(authorizer_calls) > before[0] and len(progress_calls) > before[1]
        assert db.execute('PRAGMA query_only').fetchone()[0] == 1
        db.rollback()


def test_borrow_requires_existing_query_only_transaction(tmp_path):
    path = mission(tmp_path)
    with sqlite3.connect(path) as db:
        with pytest.raises(ValueError, match='BORROWED_READ_TRANSACTION_REQUIRED'):
            with borrowed_research_validation_session(
                db, tmp_path / 'index', caller_deadline=lambda: None,
            ):
                pytest.fail('must not yield')
        assert not db.in_transaction


def test_actual_snapshot_routes_use_index_without_legacy_fallback(tmp_path, monkeypatch):
    path = mission(tmp_path)
    with sqlite3.connect(path) as db:
        expected = current_research_snapshot(db, now=NOW + timedelta(hours=1))
    from datetime import datetime

    class FixedClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW + timedelta(hours=1)

    monkeypatch.setattr(dashboard, 'datetime', FixedClock)
    import kalshi_predictor.overnight_paper.current_research_dashboard as legacy

    def forbidden(*args, **kwargs):
        raise AssertionError('legacy whole-list fallback invoked')

    monkeypatch.setattr(legacy, 'read_current_records', forbidden)
    before = path.read_bytes()
    value = dashboard.snapshot(path)
    expected_alpha = captured_alpha_analysis(envelopes(record()), now=NOW + timedelta(hours=1))

    def assert_projection(actual):
        # Preserve exact parity for every pre-existing field, while validating
        # the intentional additive projection against the original fixture.
        batch = dict(actual['latest_assessment_batch'])
        alpha = batch.pop('alpha_analysis')
        assert {**actual, 'latest_assessment_batch': batch} == expected
        assert alpha == expected_alpha
        assert alpha['funnel']['gross_positive'] == 1
        assert alpha['funnel']['after_fee_gt_5c'] == 1
        assert alpha['freshness'] == 'STALE' and alpha['current_best_after_fee'] is None
        assert alpha['full_net_ev'] is None and alpha['paper_eligible'] is False

    assert_projection(value['current_research'])
    assert value['paper_mode'] == 'NOT_ACTIVE' and value['live_exchange'] == 'DISABLED'
    app = FastAPI()
    app.include_router(dashboard.create_router())
    monkeypatch.setenv('OVERNIGHT_PAPER_DB', str(path))
    with TestClient(app) as client:
        assert_projection(client.get('/api/paper-live').json()['current_research'])
        assert 'Latest recorded assessment batch' in client.get('/paper-live').text
    assert path.read_bytes() == before
    assert not (tmp_path / 'kalshi-paper-research-view' / 'active').exists()


def test_snapshot_begins_before_schema_and_preserves_one_generation(tmp_path, monkeypatch):
    path = mission(tmp_path)
    original = dashboard.borrowed_research_validation_session

    @contextmanager
    def append_after_schema(db, index_path, **kwargs):
        assert db.in_transaction and db.row_factory is sqlite3.Row
        with sqlite3.connect(path) as writer:
            writer.execute('BEGIN')
            append_current_record(writer, kind='ASSESSMENT', identity='late', payload=record(),
                                  recorded_at=NOW)
            writer.execute("INSERT INTO overnight_history VALUES('later-event')")
        with original(db, index_path, **kwargs) as session:
            yield session

    monkeypatch.setattr(dashboard, 'borrowed_research_validation_session', append_after_schema)
    value = dashboard.snapshot(path)
    assert value['current_research']['assessment_count'] == 1
    assert value['historical_evaluated_events'] == 0


def test_busy_resets_without_recursive_lease_or_stale_counts(tmp_path):
    path = mission(tmp_path)
    with dashboard_scratch.research_request_scratch():
        assert dashboard.snapshot(None)['paper_mode'] == 'NOT_ACTIVE'
        value = dashboard.snapshot(path)
        assert value['paper_mode'] == 'UNVERIFIED'
        assert value['blockers'] == ['RESEARCH_VIEW_BUSY']
        assert value['current_research']['journal_records'] is None


def test_scratch_foreign_or_orphan_preserved_and_lock_inode_never_deleted(tmp_path):
    root = tmp_path / 'kalshi-paper-research-view'
    with pytest.raises(ValueError, match='FOREIGN_CONTENT'):
        with dashboard_scratch.research_request_scratch() as lease:
            (lease.index_path.parent / 'foreign.txt').write_text('preserve')
    lock_identity = (root / 'request.lock').stat().st_ino
    with pytest.raises(dashboard_scratch.ResearchScratchUnavailable, match='ORPHAN_REFUSED'):
        with dashboard_scratch.research_request_scratch():
            pytest.fail('orphan must refuse')
    assert (root / 'active' / 'foreign.txt').read_text() == 'preserve'
    assert (root / 'request.lock').stat().st_ino == lock_identity
    value = dashboard.snapshot(mission(tmp_path))
    assert value['blockers'] == ['RESEARCH_VIEW_SCRATCH_UNAVAILABLE']
    assert value['current_research']['journal_records'] is None


def test_cross_process_lock_refuses_without_new_index(tmp_path):
    code = (
        'import sys; from kalshi_predictor.overnight_paper import dashboard_scratch as m; '
        'm.tempfile.gettempdir=lambda:sys.argv[1]\n'
        'try:\n'
        ' with m.research_request_scratch(): raise AssertionError("lock bypass")\n'
        'except m.ResearchViewBusy: sys.exit(7)\n'
    )
    with dashboard_scratch.research_request_scratch():
        child = subprocess.run([sys.executable, '-B', '-c', code, str(tmp_path)],
                               capture_output=True, text=True, timeout=10)
        assert child.returncode == 7, child.stderr
    with dashboard_scratch.research_request_scratch() as lease:
        assert not lease.index_path.exists()


def test_replaced_active_directory_is_preserved(tmp_path):
    original = tmp_path / 'retained'
    with pytest.raises(ValueError, match='DIRECTORY_CHANGED'):
        with dashboard_scratch.research_request_scratch() as lease:
            lease.index_path.parent.rename(original)
            lease.index_path.parent.mkdir()
            (lease.index_path.parent / 'foreign').write_text('keep')
    assert original.is_dir()
    assert (tmp_path / 'kalshi-paper-research-view' / 'active' / 'foreign').read_text() == 'keep'


def test_symlink_scratch_root_never_touched(tmp_path):
    foreign = tmp_path / 'foreign'
    foreign.mkdir()
    root = tmp_path / 'kalshi-paper-research-view'
    try:
        root.symlink_to(foreign, target_is_directory=True)
    except OSError:
        pytest.skip('symlink creation unavailable on this platform')
    with pytest.raises(ValueError, match='ROOT_INVALID'):
        with dashboard_scratch.research_request_scratch():
            pytest.fail('symlink must refuse')
    assert list(foreign.iterdir()) == []


def test_same_name_replaced_index_preserved_by_both_cleanup_layers(tmp_path):
    path = mission(tmp_path)
    root = tmp_path / 'kalshi-paper-research-view'
    with sqlite3.connect(path) as db:
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        with pytest.raises(ValueError, match='FOREIGN_CONTENT'):
            with dashboard_scratch.research_request_scratch() as lease:
                with pytest.raises(ValueError, match='INDEX_OWNERSHIP_CHANGED'):
                    with borrowed_research_validation_session(
                        db, lease.index_path, caller_deadline=lease.deadline,
                        cleanup_index=True,
                    ) as session:
                        session._close()
                        lease.index_path.rename(tmp_path / 'retained-index')
                        lease.index_path.write_bytes(b'foreign original')
        assert db.in_transaction
    assert (root / 'active' / 'index.sqlite').read_bytes() == b'foreign original'
    assert (tmp_path / 'retained-index').is_file()


def test_replaced_root_preserves_owned_and_foreign_directories(tmp_path):
    root = tmp_path / 'kalshi-paper-research-view'
    retained = tmp_path / 'retained-root'
    with pytest.raises(ValueError, match='ROOT_CHANGED'):
        with dashboard_scratch.research_request_scratch():
            root.rename(retained)
            (root / 'active').mkdir(parents=True)
            (root / 'active' / 'index.sqlite').write_bytes(b'foreign root')
    assert (root / 'active' / 'index.sqlite').read_bytes() == b'foreign root'
    assert (retained / 'active').is_dir()


def test_request_clock_captured_after_schema_snapshot(tmp_path, monkeypatch):
    from datetime import datetime

    path = mission(tmp_path)
    real_connect = sqlite3.connect
    queries = []

    def connect(*args, **kwargs):
        db = real_connect(*args, **kwargs)
        db.set_trace_callback(queries.append)
        return db

    class FixedClock(datetime):
        @classmethod
        def now(cls, tz=None):
            assert "SELECT name FROM sqlite_master WHERE type='table'" in queries
            return NOW + timedelta(hours=1)

    monkeypatch.setattr(dashboard.sqlite3, 'connect', connect)
    monkeypatch.setattr(dashboard, 'datetime', FixedClock)
    assert dashboard.snapshot(path)['current_research']['assessment_count'] == 1
