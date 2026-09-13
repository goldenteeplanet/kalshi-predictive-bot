"""Offline latest-batch adapter; no dashboard or runtime consumer calls this API."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from .current_assessment_view import empty_assessment_batch, latest_assessment_batch
from .current_research_index import ResearchValidationSession


def indexed_latest_assessment_batch(
    session: ResearchValidationSession, *, now: datetime,
) -> dict[str, Any]:
    """Reuse the deployed arithmetic oracle on at most 600 verified originals.

    Lifecycle/deadline/original validation failures propagate. The result remains
    journal arithmetic only, never provider-original replay or admission.
    """
    try:
        ids = session.latest_assessment_ids()
    except ValueError as exc:
        if str(exc) != 'AMBIGUOUS_OR_OVERSIZED_LATEST_ASSESSMENT_BATCH':
            raise
        result = empty_assessment_batch()
        result.update(status='INVALID_OR_AMBIGUOUS', blocker=str(exc))
        return result
    records = [json.loads(session.read_original(key)) for key in ids]
    result = latest_assessment_batch(records, now=now)
    # Enforce the shared lifetime after projection computation as well.
    _ = session.manifest
    return result
