"""Complete-only disk indexed validation; existing aggregate limits still apply.

This new API has no callers in production. It writes only an exclusively created
scratch index, never the mission. The index contains references, not originals.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .current_research_store import (
    MAX_READ_BYTES,
    MAX_RECORD_BYTES,
    PREFIX,
    _linked_shadow,
    _read_envelope,
)
from .store import aware, digest


def _validate_index_links(index: sqlite3.Connection) -> None:
    bad_link = index.execute(
        "SELECT c.id FROM records c LEFT JOIN records p "
        "ON p.kind='PROSPECTIVE_SHADOW' AND p.semantic_id=c.parent "
        "WHERE c.parent IS NOT NULL AND (p.id IS NULL OR p.at>=c.at) LIMIT 1"
    ).fetchone()
    if bad_link:
        raise ValueError("INDEX_STRICT_PRIOR_SHADOW_REQUIRED")


@dataclass(frozen=True)
class ValidatedResearchIndex:
    index_path: Path
    index_sha256: str
    original_manifest_sha256: str
    record_count: int
    payload_bytes: int
    status: str = "COMPLETE_VALIDATED_SNAPSHOT"
    validation_version: str = "CURRENT_RESEARCH_INDEX_V1"


_SESSION_OWNER = object()


def _dashboard_scan_clock(record: dict[str, Any], recorded: str) -> str:
    clock = aware(record['assessed_at'])
    if clock > aware(recorded):
        raise ValueError('CURRENT_RESEARCH_SCAN_CLOCK_INVALID')
    for field in ('funnel', 'first_blocker_counts'):
        counts = record[field]
        if not isinstance(counts, dict) or any(
            not isinstance(k, str) or type(v) is not int or v < 0
            for k, v in counts.items()
        ):
            raise ValueError('CURRENT_RESEARCH_SCAN_COUNTS_INVALID')
    return clock.isoformat()


def _dashboard_scan_metadata(
    record: dict[str, Any], recorded: str,
) -> tuple[str | None, str | None, str | None]:
    try:
        return _dashboard_scan_clock(record, recorded), None, None
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        # Do not store potentially huge parser exception text; replay the first
        # offending original only when dashboard projection requests this error.
        return None, type(exc).__name__, None


@dataclass(frozen=True)
class ResearchDashboardSummary:
    journal_records: int
    scan_count: int
    assessment_count: int
    prospective_shadow_count: int
    evaluated_shadow_count: int
    shadow_state_counts: tuple[tuple[str, int], ...]
    latest_scan_id: str | None
    latest_scan_at: str | None


class ResearchValidationSession:
    """Complete validated snapshot access, valid only inside its owning context.

    No connection or arbitrary SQL interface is exposed. Returned bytes are one
    original journal envelope, never a currentness or source-authentication claim.
    """

    def __init__(
        self, owner: object, source: sqlite3.Connection, index: sqlite3.Connection,
        manifest: ValidatedResearchIndex, deadline: Callable[[], None],
        file_identity: tuple[int, int],
    ) -> None:
        if owner is not _SESSION_OWNER:
            raise ValueError("INDEX_OWNED_CONTEXT_REQUIRED")
        self._source = source
        self._index = index
        self._manifest = manifest
        self._deadline = deadline
        self._file_identity = file_identity
        self._closed = False

    def _check(self) -> None:
        if self._closed:
            raise ValueError("INDEX_SESSION_CLOSED")
        try:
            self._deadline()
            stat = self._manifest.index_path.lstat()
            if self._manifest.index_path.is_symlink() or (stat.st_dev, stat.st_ino) != (
                self._file_identity
            ):
                raise ValueError("INDEX_OWNERSHIP_CHANGED")
        except BaseException:
            self._close()
            raise

    def _close(self) -> None:
        if not self._closed:
            self._closed = True
            try:
                self._source.close()
            finally:
                self._index.close()

    @property
    def manifest(self) -> ValidatedResearchIndex:
        self._check()
        return self._manifest

    def read_original(self, record_id: str) -> bytes:
        """Read by validated ID from the same transaction; later appends are absent."""
        self._check()
        if type(record_id) is not str or not 0 < len(record_id) <= 160:
            raise ValueError("INDEX_KEY_BOUND")
        try:
            raw, _ = self._read(record_id, linked=False)
            self._check()
            return raw.encode()
        except BaseException:
            self._close()
            raise

    def latest_assessment_ids(self) -> tuple[str, ...]:
        """Compact latest physical-clock selection; never load historical payloads.

        Return original journal order for deterministic legacy tie handling;
        the deployed aware() helper normalizes offsets to UTC. A malformed clock
        anywhere takes precedence,
        as in the legacy projection's complete candidate comprehension.
        """
        self._check()
        invalid = self._index.execute(
            'SELECT id FROM assessments WHERE invalid_clock=1 ORDER BY ordinal LIMIT 1'
        ).fetchone()
        if invalid is not None:
            self._check()
            return (invalid[0],)
        latest = self._index.execute('SELECT max(assessed_utc) FROM assessments').fetchone()[0]
        if latest is None:
            self._check()
            return ()
        count, hashes = self._index.execute(
            'SELECT count(*),count(DISTINCT scan_sha) FROM assessments WHERE assessed_utc=?',
            (latest,),
        ).fetchone()
        self._check()
        if count > 600 or hashes != 1:
            raise ValueError('AMBIGUOUS_OR_OVERSIZED_LATEST_ASSESSMENT_BATCH')
        rows = self._index.execute(
            'SELECT id FROM assessments WHERE assessed_utc=? ORDER BY ordinal LIMIT 600',
            (latest,),
        ).fetchall()
        self._check()
        return tuple(row[0] for row in rows)

    def dashboard_summary(self, *, now: datetime) -> ResearchDashboardSummary:
        """Complete compact projection; dashboard errors stay outside foundation validation."""
        self._check()
        if now.utcoffset() is None:
            raise ValueError('CURRENT_RESEARCH_DASHBOARD_AWARE_CLOCK_REQUIRED')
        at = aware(now.isoformat()).isoformat()
        first_error = self._index.execute(
            'SELECT r.id,r.at FROM records r '
            'JOIN dashboard_metadata m ON m.id=r.id '
            'WHERE r.at>? OR m.error_type IS NOT NULL ORDER BY m.ordinal LIMIT 1', (at,),
        ).fetchone()
        self._check()
        if first_error is not None:
            key, recorded = first_error
            if recorded > at:
                raise ValueError('CURRENT_RESEARCH_DASHBOARD_FUTURE_CLOCK')
            raw, envelope = self._read(key, linked=False)
            del raw
            _dashboard_scan_clock(envelope['record'], envelope['recorded_at'])
            raise ValueError('INDEX_DASHBOARD_ERROR_METADATA_MISMATCH')
        counts = dict(self._index.execute('SELECT kind,count(*) FROM records GROUP BY kind'))
        states = dict(self._index.execute(
            "SELECT CASE WHEN EXISTS(SELECT 1 FROM records e WHERE e.kind='EVALUATION' "
            'AND e.parent=s.semantic_id) THEN \'EVALUATED\' ELSE COALESCE('
            "(SELECT o.state FROM records o JOIN dashboard_metadata m ON m.id=o.id "
            "WHERE o.kind='SHADOW_OBSERVATION' AND o.parent=s.semantic_id "
            "ORDER BY m.ordinal DESC LIMIT 1),'OPEN') END AS final_state,count(*) "
            "FROM records s WHERE s.kind='PROSPECTIVE_SHADOW' GROUP BY final_state"
        ))
        latest = self._index.execute(
            'SELECT id,scan_clock FROM dashboard_metadata WHERE scan_clock IS NOT NULL '
            'ORDER BY scan_clock DESC,id DESC LIMIT 1'
        ).fetchone()
        self._check()
        return ResearchDashboardSummary(
            sum(counts.values()), counts.get('SCAN', 0), counts.get('ASSESSMENT', 0),
            counts.get('PROSPECTIVE_SHADOW', 0), counts.get('EVALUATION', 0),
            tuple((state, states.get(state, 0)) for state in (
                'OPEN', 'CLOSED', 'AWAITING_FINAL', 'FINAL', 'EVALUATED')),
            None if latest is None else latest[0], None if latest is None else latest[1],
        )

    def _read(self, key: str, *, linked: bool) -> tuple[str, dict[str, Any]]:
        self._check()
        expected = self._index.execute(
            "SELECT at,kind,raw_sha,payload_sha,bytes,parent FROM records WHERE id=?", (key,)
        ).fetchone()
        if expected is None:
            raise ValueError("INDEX_ID_NOT_IN_VALIDATED_SNAPSHOT")
        at, kind, raw_sha, payload_sha, size, parent = expected
        observed = self._source.execute(
            "SELECT captured_at,length(CAST(payload AS BLOB)) "
            "FROM overnight_sprint_cycles WHERE id=?", (key,)
        ).fetchone()
        if (observed is None or type(observed[1]) is not int
                or not 0 < observed[1] <= MAX_RECORD_BYTES or observed[1] != size):
            raise ValueError("INDEX_ORIGINAL_LENGTH_MISMATCH")
        if aware(observed[0]).astimezone(UTC).isoformat() != at:
            raise ValueError("INDEX_ORIGINAL_CLOCK_MISMATCH")
        raw = self._source.execute(
            "SELECT payload FROM overnight_sprint_cycles WHERE id=?", (key,)
        ).fetchone()[0]
        if not isinstance(raw, str) or hashlib.sha256(raw.encode()).hexdigest() != raw_sha:
            raise ValueError("INDEX_ORIGINAL_HASH_MISMATCH")
        envelope = _read_envelope(key, observed[0], raw)
        if envelope['record_kind'] != kind or envelope['payload_sha256'] != payload_sha:
            raise ValueError("INDEX_ORIGINAL_IDENTITY_MISMATCH")
        if kind == 'SCAN':
            metadata = self._index.execute(
                'SELECT scan_clock,error_type,error_message FROM dashboard_metadata WHERE id=?',
                (key,),
            ).fetchone()
            if metadata != _dashboard_scan_metadata(envelope['record'], observed[0]):
                raise ValueError('INDEX_SCAN_METADATA_MISMATCH')
        if kind == 'ASSESSMENT':
            batch_clock: str | None
            try:
                batch_clock = aware(envelope['record']['assessed_at']).astimezone(UTC).isoformat()
                invalid_clock = 0
            except (ValueError, TypeError, KeyError):
                batch_clock, invalid_clock = None, 1
            metadata = self._index.execute(
                'SELECT assessed_utc,scan_sha,invalid_clock FROM assessments WHERE id=?', (key,),
            ).fetchone()
            if metadata != (batch_clock, envelope['record']['scan_sha256'], invalid_clock):
                raise ValueError('INDEX_ASSESSMENT_METADATA_MISMATCH')
        if linked and (kind != 'PROSPECTIVE_SHADOW' or parent is not None):
            raise ValueError("INDEX_PARENT_KIND_MISMATCH")
        if parent is not None:
            parent_key = PREFIX + 'prospective_shadow:' + digest({'identity': parent})
            parent_raw, parent_envelope = self._read(parent_key, linked=True)
            if (parent_envelope['record']['decision_id'] != parent
                    or aware(parent_envelope['recorded_at']) >= aware(envelope['recorded_at'])):
                raise ValueError("INDEX_STRICT_PRIOR_SHADOW_REQUIRED")
            del parent_raw, parent_envelope
        _linked_shadow(self._source, kind, envelope['record'])
        self._check()
        return raw, envelope


@contextmanager
def research_validation_session(
    mission_path: Path,
    index_path: Path,
    *,
    max_records: int = 10000,
    max_bytes: int = MAX_READ_BYTES,
    timeout_seconds: int = 30,
) -> Iterator[ResearchValidationSession]:
    """Replay one payload at a time in one read transaction, then publish index.

    Extra read invariants: shadow/evaluation semantic identities are unique and
    match envelope identity; observation (decision,state) pairs are unique;
    parent journal time strictly precedes child time. Invalid legacy evidence
    fails explicitly, never gets rewritten. No source authentication is implied.
    """
    if (
        type(max_records) is not int
        or not 1 <= max_records <= 10000
        or type(max_bytes) is not int
        or not 1 <= max_bytes <= MAX_READ_BYTES
        or type(timeout_seconds) is not int
        or not 1 <= timeout_seconds <= 60
    ):
        raise ValueError("INDEX_BOUNDS_INVALID")
    mission_path = mission_path.resolve(strict=True)
    index_path = index_path.absolute()
    if index_path.exists() or index_path.is_symlink() or not index_path.parent.is_dir():
        raise ValueError("INDEX_EXCLUSIVE_DESTINATION_REQUIRED")
    if index_path.parent.resolve() != index_path.parent:
        raise ValueError("INDEX_REAL_PARENT_REQUIRED")
    started = time.monotonic()

    def deadline() -> None:
        if time.monotonic() - started >= timeout_seconds:
            raise ValueError("INDEX_TIME_BOUND")

    # Exclusive file creation defines cleanup ownership. Never remove a prior file.
    with index_path.open("xb"):
        pass
    stat = index_path.lstat()
    file_identity = (stat.st_dev, stat.st_ino)
    index: sqlite3.Connection | None = None
    source: sqlite3.Connection | None = None
    session: ResearchValidationSession | None = None
    complete = False
    try:
        index = sqlite3.connect(index_path, timeout=1)
        index.execute("PRAGMA page_size=4096")
        index.execute("PRAGMA journal_mode=DELETE")
        index.execute("PRAGMA cache_size=-2048")
        index.execute("PRAGMA temp_store=FILE")
        index.execute("PRAGMA max_page_count=8192")
        index.set_progress_handler(lambda: int(time.monotonic() - started >= timeout_seconds), 1000)
        index.execute(
            "CREATE TABLE records (id TEXT PRIMARY KEY, at TEXT NOT NULL, "
            "kind TEXT NOT NULL, raw_sha TEXT NOT NULL, payload_sha TEXT NOT NULL, "
            "bytes INTEGER NOT NULL, semantic_id TEXT, state TEXT, parent TEXT)"
        )
        index.execute("CREATE UNIQUE INDEX semantic ON records(kind,semantic_id,state)")
        index.execute('CREATE INDEX lifecycle_parent ON records(kind,parent)')
        index.execute(
            'CREATE TABLE assessments (id TEXT PRIMARY KEY, assessed_utc TEXT, '
            'scan_sha TEXT NOT NULL, ordinal INTEGER NOT NULL, invalid_clock INTEGER NOT NULL)'
        )
        index.execute('CREATE INDEX assessment_clock ON assessments(assessed_utc,ordinal)')
        index.execute(
            'CREATE TABLE dashboard_metadata (id TEXT PRIMARY KEY,ordinal INTEGER NOT NULL, '
            'scan_clock TEXT,error_type TEXT,error_message TEXT)'
        )
        index.execute('CREATE INDEX dashboard_order ON dashboard_metadata(ordinal)')
        index.execute('CREATE INDEX dashboard_scan ON dashboard_metadata(scan_clock,id)')
        source = sqlite3.connect(mission_path.as_uri() + "?mode=ro", uri=True, timeout=1)
        source.execute("PRAGMA query_only=ON")
        source.execute("PRAGMA cache_size=-2048")
        source.set_progress_handler(
            lambda: int(time.monotonic() - started >= timeout_seconds), 1000
        )
        source.execute("BEGIN")
        count, total = source.execute(
            "SELECT count(*),coalesce(sum(length(CAST(payload AS BLOB))),0) "
            "FROM overnight_sprint_cycles WHERE id LIKE ?",
            (PREFIX + "%",),
        ).fetchone()
        if count > max_records or total > max_bytes:
            raise ValueError("INDEX_AGGREGATE_BOUND_EXCEEDED")
        manifest = hashlib.sha256()
        observed_count = observed_bytes = 0
        # Length-only cursor avoids fetching oversized payloads before rejecting.
        cursor = source.execute(
            "SELECT id,captured_at,length(CAST(payload AS BLOB)) "
            "FROM overnight_sprint_cycles WHERE id LIKE ? ORDER BY captured_at,id",
            (PREFIX + "%",),
        )
        for key, at, size in cursor:
            deadline()
            if not isinstance(size, int) or not 0 < size <= MAX_RECORD_BYTES:
                raise ValueError("INDEX_RECORD_BOUND_EXCEEDED")
            if not isinstance(key, str) or len(key) > 160:
                raise ValueError("INDEX_KEY_BOUND")
            raw = source.execute(
                "SELECT payload FROM overnight_sprint_cycles WHERE id=?", (key,)
            ).fetchone()[0]
            envelope = _read_envelope(key, at, raw)
            record, kind = envelope["record"], envelope["record_kind"]
            dashboard_metadata = (
                _dashboard_scan_metadata(record, at) if kind == 'SCAN' else (None, None, None)
            )
            index.execute('INSERT INTO dashboard_metadata VALUES(?,?,?,?,?)',
                          (key, observed_count, *dashboard_metadata))
            if kind == 'ASSESSMENT':
                assessed_utc: str | None
                try:
                    assessed_utc = aware(record['assessed_at']).astimezone(UTC).isoformat()
                    invalid_clock = 0
                except (ValueError, TypeError, KeyError):
                    assessed_utc, invalid_clock = None, 1
                index.execute(
                    'INSERT INTO assessments VALUES(?,?,?,?,?)',
                    (key, assessed_utc, record['scan_sha256'], observed_count, invalid_clock),
                )
            if kind in ("SHADOW_OBSERVATION", "EVALUATION"):
                identity = (
                    record["decision_id"]
                    if kind == "SHADOW_OBSERVATION"
                    else record["decision"]["decision_id"]
                )
                parent_key = PREFIX + "prospective_shadow:" + digest({"identity": identity})
                parent_size = source.execute(
                    "SELECT length(CAST(payload AS BLOB)) FROM overnight_sprint_cycles WHERE id=?",
                    (parent_key,),
                ).fetchone()
                if parent_size is None:
                    raise ValueError("CURRENT_RESEARCH_PRIOR_SHADOW_REQUIRED")
                if type(parent_size[0]) is not int or not 0 < parent_size[0] <= MAX_RECORD_BYTES:
                    raise ValueError("INDEX_LINKED_RECORD_BOUND_EXCEEDED")
            _linked_shadow(source, kind, record)
            semantic, state, parent = None, None, None
            if kind in ("PROSPECTIVE_SHADOW", "EVALUATION"):
                semantic = (record if kind == "PROSPECTIVE_SHADOW" else record["decision"])[
                    "decision_id"
                ]
                state = "IDENTITY"
                if envelope["identity"] != semantic:
                    raise ValueError("INDEX_SEMANTIC_IDENTITY_MISMATCH")
            elif kind == "SHADOW_OBSERVATION":
                semantic, state = record["decision_id"], record["state"]
            if kind in ("SHADOW_OBSERVATION", "EVALUATION"):
                parent = semantic
            raw_sha = hashlib.sha256(raw.encode()).hexdigest()
            index.execute(
                "INSERT INTO records VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    key,
                    aware(at).astimezone(UTC).isoformat(),
                    kind,
                    raw_sha,
                    envelope["payload_sha256"],
                    size,
                    semantic,
                    state,
                    parent,
                ),
            )
            manifest.update(json.dumps([key, at, raw_sha], separators=(",", ":")).encode() + b"\n")
            observed_count += 1
            observed_bytes += size
            # No decoded payload list, shadow dictionary or original copy survives.
            del raw, envelope, record
        if (observed_count, observed_bytes) != (count, total):
            raise ValueError("INDEX_SNAPSHOT_COVERAGE_MISMATCH")
        _validate_index_links(index)
        deadline()
        index.execute(
            "CREATE TABLE completion(status TEXT,records INTEGER,bytes INTEGER, "
            "original_manifest_sha256 TEXT)"
        )
        index.execute(
            "INSERT INTO completion VALUES(?,?,?,?)",
            (
                "COMPLETE_VALIDATED_SNAPSHOT",
                count,
                total,
                manifest.hexdigest(),
            ),
        )
        index.commit()
        index.execute('PRAGMA query_only=ON')
        index.execute('BEGIN')
        index.execute('SELECT count(*) FROM records').fetchone()
        deadline()
        file_hash = hashlib.sha256()
        with index_path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(65536), b""):
                deadline()
                file_hash.update(chunk)
        manifest_result = ValidatedResearchIndex(
            index_path,
            file_hash.hexdigest(),
            manifest.hexdigest(),
            count,
            total,
        )
        session = ResearchValidationSession(
            _SESSION_OWNER, source, index, manifest_result, deadline, file_identity,
        )
        session._check()
        complete = True
        yield session
        if not session._closed:
            session._check()
    except BaseException:
        if source is not None:
            source.close()
        if index is not None:
            index.close()
        if not complete:
            stat = index_path.lstat()
            if index_path.is_symlink() or (stat.st_dev, stat.st_ino) != file_identity:
                raise ValueError('INDEX_OWNERSHIP_CHANGED') from None
            index_path.unlink()
        raise
    finally:
        if session is not None:
            session._close()
        elif source is not None:
            source.close()
        if index is not None:
            index.close()


def validate_research_index(
    mission_path: Path, index_path: Path, *, max_records: int = 10000,
    max_bytes: int = MAX_READ_BYTES, timeout_seconds: int = 30,
) -> ValidatedResearchIndex:
    """Return the original closed-snapshot manifest API; no resumable session."""
    with research_validation_session(
        mission_path, index_path, max_records=max_records, max_bytes=max_bytes,
        timeout_seconds=timeout_seconds,
    ) as session:
        return session.manifest
