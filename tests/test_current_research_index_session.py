"""Owned snapshot lifetime and per-ID original replay; no runtime consumers."""

import hashlib
import json
import sqlite3

import pytest
from test_current_research_index import mission
from test_current_research_store import NOW, payload

from kalshi_predictor.overnight_paper import current_research_index as module
from kalshi_predictor.overnight_paper.current_research_store import append_current_record


def originals(path):
    with sqlite3.connect(path) as db:
        return dict(db.execute('SELECT id,payload FROM overnight_sprint_cycles'))


def test_complete_context_reads_exact_originals_and_closes(tmp_path):
    path = mission(tmp_path, 3, lifecycle=True)
    expected = originals(path)
    with module.research_validation_session(path, tmp_path / 'index') as session:
        manifest = session.manifest
        assert manifest.status == 'COMPLETE_VALIDATED_SNAPSHOT'
        assert manifest.record_count == len(expected)
        assert not hasattr(session, 'connection') and not hasattr(session, 'execute')
        for key, raw in expected.items():
            assert session.read_original(key) == raw.encode()
    assert manifest.index_path.exists()  # Diagnostic artifact, not a resumable session.
    for action in (lambda: session.manifest, lambda: session.read_original(next(iter(expected)))):
        with pytest.raises(ValueError, match='SESSION_CLOSED'):
            action()


def test_consumer_exception_closes_but_keeps_complete_owned_artifact(tmp_path):
    path = mission(tmp_path)
    with pytest.raises(RuntimeError, match='consumer'):
        with module.research_validation_session(path, tmp_path / 'index') as session:
            assert session.manifest.record_count == 3
            raise RuntimeError('consumer')
    with pytest.raises(ValueError, match='SESSION_CLOSED'):
        session.read_original(next(iter(originals(path))))
    assert (tmp_path / 'index').exists()


def test_idle_consumption_deadline_expires_and_closes(tmp_path, monkeypatch):
    path = mission(tmp_path)
    clock = [0.0]
    monkeypatch.setattr(module.time, 'monotonic', lambda: clock[0])
    with module.research_validation_session(path, tmp_path / 'index', timeout_seconds=1) as session:
        key = next(iter(originals(path)))
        clock[0] = 1.0
        with pytest.raises(ValueError, match='TIME_BOUND'):
            session.read_original(key)
        with pytest.raises(ValueError, match='SESSION_CLOSED'):
            session.read_original(key)


def test_append_and_update_after_complete_cannot_change_held_snapshot(tmp_path):
    path = mission(tmp_path, 2)
    prior = originals(path)
    with module.research_validation_session(path, tmp_path / 'index') as session:
        with sqlite3.connect(path) as writer:
            writer.execute('BEGIN')
            later = append_current_record(writer, kind='ASSESSMENT', identity='later',
                                          payload=payload(), recorded_at=NOW)
            writer.execute('UPDATE overnight_sprint_cycles SET payload=? WHERE id=?',
                           ('{', next(iter(prior))))
        assert session.manifest.record_count == 2
        for key, raw in prior.items():
            assert session.read_original(key) == raw.encode()
        with pytest.raises(ValueError, match='NOT_IN_VALIDATED_SNAPSHOT'):
            session.read_original(later)
    with pytest.raises(ValueError):
        module.validate_research_index(path, tmp_path / 'next')
    assert not (tmp_path / 'next').exists()


def test_replaced_scratch_file_is_not_read_or_deleted(tmp_path):
    path = mission(tmp_path)
    index = tmp_path / 'index'
    with module.research_validation_session(path, index) as session:
        replacement = tmp_path / 'foreign'
        replacement.write_bytes(b'foreign artifact')
        replacement.replace(index)
        with pytest.raises(ValueError, match='OWNERSHIP_CHANGED'):
            session.read_original(next(iter(originals(path))))
    assert index.read_bytes() == b'foreign artifact'


def test_replacement_during_failed_validation_preserved(tmp_path, monkeypatch):
    path = mission(tmp_path)
    index = tmp_path / 'index'

    def fail(*args):
        replacement = tmp_path / 'foreign'
        replacement.write_bytes(b'foreign artifact')
        replacement.replace(index)
        raise ValueError('injected validation failure')

    monkeypatch.setattr(module, '_read_envelope', fail)
    with pytest.raises(ValueError, match='OWNERSHIP_CHANGED'):
        with module.research_validation_session(path, index):
            pytest.fail('partial session exposed')
    assert index.read_bytes() == b'foreign artifact'


def test_external_index_update_is_locked_and_manifest_stays_exact(tmp_path):
    path = mission(tmp_path)
    target = tmp_path / 'index'
    with module.research_validation_session(path, target) as session:
        expected_hash = session.manifest.index_sha256
        other = sqlite3.connect(target, timeout=0.01)
        try:
            other.execute("UPDATE records SET raw_sha='foreign'")
            with pytest.raises(sqlite3.OperationalError, match='locked'):
                other.commit()
            other.rollback()
        finally:
            other.close()
        assert hashlib.sha256(target.read_bytes()).hexdigest() == expected_hash
        for key, raw in originals(path).items():
            assert session.read_original(key) == raw.encode()


def test_deadline_applies_at_context_exit_without_another_read(tmp_path, monkeypatch):
    path = mission(tmp_path)
    clock = [0.0]
    monkeypatch.setattr(module.time, 'monotonic', lambda: clock[0])
    with pytest.raises(ValueError, match='TIME_BOUND'):
        with module.research_validation_session(path, tmp_path / 'index', timeout_seconds=1):
            clock[0] = 1.0


@pytest.mark.parametrize('field,value,error', [
    ('raw_sha', 'f' * 64, 'HASH_MISMATCH'),
    ('payload_sha', 'f' * 64, 'IDENTITY_MISMATCH'),
    ('at', '2030-01-01T00:00:00+00:00', 'CLOCK_MISMATCH'),
    ('bytes', 8000001, 'LENGTH_MISMATCH'),
])
def test_internal_index_damage_never_returns_unverified_original(tmp_path, field, value, error):
    path = mission(tmp_path)
    key = next(iter(originals(path)))
    with module.research_validation_session(path, tmp_path / 'index') as session:
        # Fault injection via private internals; no SQL connection is a public API.
        session._index.execute('PRAGMA query_only=OFF')
        session._index.execute(f'UPDATE records SET {field}=? WHERE id=?', (value, key))
        session._index.execute('PRAGMA query_only=ON')
        with pytest.raises(ValueError, match=error):
            session.read_original(key)


def test_linked_parent_compared_against_its_own_index_hash(tmp_path):
    path = mission(tmp_path, 0, lifecycle=True)
    expected = originals(path)
    child = next(k for k in expected if ':evaluation:' in k)
    parent = next(k for k in expected if ':prospective_shadow:' in k)
    with module.research_validation_session(path, tmp_path / 'index') as session:
        session._index.execute('PRAGMA query_only=OFF')
        session._index.execute('UPDATE records SET raw_sha=? WHERE id=?', ('f' * 64, parent))
        session._index.execute('PRAGMA query_only=ON')
        with pytest.raises(ValueError, match='HASH_MISMATCH'):
            session.read_original(child)


def test_oversized_linked_parent_refused_before_payload_fetch(tmp_path):
    path = mission(tmp_path, 0, lifecycle=True)
    expected = originals(path)
    child = next(k for k in expected if ':evaluation:' in k)
    parent = next(k for k in expected if ':prospective_shadow:' in k)
    with module.research_validation_session(path, tmp_path / 'index') as session:
        real = session._source

        class OversizedParent:
            def execute(self, sql, params):
                if params == (parent,):
                    assert not sql.startswith('SELECT payload'), 'oversized parent fetched'

                    class Row:
                        def fetchone(self):
                            return (json.loads(expected[parent])['recorded_at'], 8000001)
                    return Row()
                return real.execute(sql, params)

            def close(self):
                real.close()

        session._source = OversizedParent()
        with pytest.raises(ValueError, match='LENGTH_MISMATCH'):
            session.read_original(child)
