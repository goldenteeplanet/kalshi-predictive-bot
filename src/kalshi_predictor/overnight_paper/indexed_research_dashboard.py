"""Offline complete-journal projection; production consumers remain unchanged."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from .current_research_dashboard import empty_current_research
from .current_research_index import ResearchValidationSession
from .indexed_assessment_view import indexed_latest_assessment_batch
from .store import aware


def indexed_current_research_snapshot(
    session: ResearchValidationSession, *, now: datetime,
) -> dict[str, Any]:
    summary = session.dashboard_summary(now=now)
    at = aware(now.isoformat())
    result = empty_current_research()
    result.update(
        evidence_status='VERIFIED_JOURNAL_NOT_RUNTIME_CERTIFICATION',
        journal_records=summary.journal_records, scan_count=summary.scan_count,
        assessment_count=summary.assessment_count,
        prospective_shadow_count=summary.prospective_shadow_count,
        evaluated_shadow_count=summary.evaluated_shadow_count,
        shadow_state_counts=dict(summary.shadow_state_counts),
        latest_assessment_batch=indexed_latest_assessment_batch(session, now=at),
    )
    if summary.latest_scan_id is not None:
        envelope = json.loads(session.read_original(summary.latest_scan_id))
        record = envelope['record']
        clock = aware(record['assessed_at'])
        if envelope['record_kind'] != 'SCAN' or clock.isoformat() != summary.latest_scan_at:
            raise ValueError('INDEX_LATEST_SCAN_ORIGINAL_MISMATCH')
        age = (at - clock).total_seconds()
        result.update(
            latest_scan_at=clock.isoformat(), latest_scan_age_seconds=age,
            latest_scan_freshness='RECENT_RECORDED_SCAN' if age <= 300 else 'STALE',
            latest_scan_funnel=record['funnel'],
            latest_scan_first_blocker_counts=record['first_blocker_counts'],
            latest_scan_scope=record.get('scope'),
        )
    _ = session.manifest
    return result
