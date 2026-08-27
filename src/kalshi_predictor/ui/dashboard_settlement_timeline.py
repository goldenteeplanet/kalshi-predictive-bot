from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from itertools import pairwise
from typing import Any, Literal

TIMELINE_SCHEMA_VERSION = "phase4gt-dashboard-settlement-timeline-v1"
TimelineStatus = Literal["PENDING", "SETTLED", "INCOMPLETE", "STALE"]
SettlementStage = Literal[
    "ORDER_FILLED",
    "AWAITING_SETTLEMENT",
    "SETTLEMENT_OBSERVED",
    "RECONCILED",
    "REALIZED",
]
STAGE_ORDER = (
    "ORDER_FILLED",
    "AWAITING_SETTLEMENT",
    "SETTLEMENT_OBSERVED",
    "RECONCILED",
    "REALIZED",
)


class DashboardSettlementTimelineError(ValueError):
    """Stable fail-closed settlement-timeline error."""


@dataclass(frozen=True)
class DashboardSettlementEvent:
    event_id: str
    stage: SettlementStage
    occurred_at_ms: int
    paper_order_id: int
    ticker: str
    forecast_id: int
    source_identity_hash: str
    source_watermark: str
    lineage_hash: str
    evidence_age_seconds: int
    complete: bool
    event_hash: str


@dataclass(frozen=True)
class DashboardSettlementTimelineItem:
    event_id: str
    stage: SettlementStage
    occurred_at_ms: int
    dwell_since_previous_ms: int | None
    complete: bool


@dataclass(frozen=True)
class DashboardSettlementTimeline:
    status: TimelineStatus
    reasons: tuple[str, ...]
    paper_order_id: int
    ticker: str
    forecast_id: int
    source_identity_hash: str
    source_watermark: str
    event_count: int
    last_stage: SettlementStage
    total_elapsed_ms: int
    observed_max_age_seconds: int
    max_evidence_age_seconds: int
    items: tuple[DashboardSettlementTimelineItem, ...]
    events_hash: str
    timeline_hash: str
    read_only: bool = True
    execution_authorized: bool = False


def make_dashboard_settlement_event(
    *,
    event_id: str,
    stage: SettlementStage,
    occurred_at_ms: int,
    paper_order_id: int,
    ticker: str,
    forecast_id: int,
    source_identity_hash: str,
    source_watermark: str,
    lineage_hash: str,
    evidence_age_seconds: int,
    complete: bool,
) -> DashboardSettlementEvent:
    unsigned = {
        "event_id": event_id,
        "stage": stage,
        "occurred_at_ms": occurred_at_ms,
        "paper_order_id": paper_order_id,
        "ticker": ticker,
        "forecast_id": forecast_id,
        "source_identity_hash": source_identity_hash,
        "source_watermark": source_watermark,
        "lineage_hash": lineage_hash,
        "evidence_age_seconds": evidence_age_seconds,
        "complete": complete,
    }
    _validate_event_fields(unsigned)
    return DashboardSettlementEvent(**unsigned, event_hash=_hash(unsigned))


def build_dashboard_settlement_timeline(
    events: Sequence[Any],
    *,
    max_events: int = 32,
    max_evidence_age_seconds: int = 300,
) -> DashboardSettlementTimeline:
    if (
        isinstance(max_events, bool)
        or not isinstance(max_events, int)
        or max_events <= 0
        or isinstance(max_evidence_age_seconds, bool)
        or not isinstance(max_evidence_age_seconds, int)
        or max_evidence_age_seconds < 0
    ):
        raise DashboardSettlementTimelineError("TIMELINE_BOUND_INVALID")
    if not events:
        raise DashboardSettlementTimelineError("EVENTS_EMPTY")
    if len(events) > max_events:
        raise DashboardSettlementTimelineError("EVENT_BOUND_EXCEEDED")

    validated = [_validated_event(item) for item in events]
    event_ids = [item.event_id for item in validated]
    if len(set(event_ids)) != len(event_ids):
        raise DashboardSettlementTimelineError("EVENT_ID_DUPLICATE")
    identities = {
        (
            item.paper_order_id,
            item.ticker,
            item.forecast_id,
            item.source_identity_hash,
            item.source_watermark,
            item.lineage_hash,
        )
        for item in validated
    }
    if len(identities) != 1:
        raise DashboardSettlementTimelineError("EVENT_LINEAGE_MIXED")

    ordered = sorted(validated, key=lambda item: (item.occurred_at_ms, item.event_id))
    if [item.occurred_at_ms for item in ordered] != sorted(
        item.occurred_at_ms for item in ordered
    ) or len({item.occurred_at_ms for item in ordered}) != len(ordered):
        raise DashboardSettlementTimelineError("EVENT_TIMESTAMP_DUPLICATE")
    stage_indexes = [STAGE_ORDER.index(item.stage) for item in ordered]
    if any(current <= previous for previous, current in pairwise(stage_indexes)):
        raise DashboardSettlementTimelineError("EVENT_STAGE_ORDER_INVALID")

    gaps = [
        STAGE_ORDER[index]
        for index in range(stage_indexes[0], stage_indexes[-1] + 1)
        if index not in stage_indexes
    ]
    items = tuple(
        DashboardSettlementTimelineItem(
            event_id=item.event_id,
            stage=item.stage,
            occurred_at_ms=item.occurred_at_ms,
            dwell_since_previous_ms=(
                None if index == 0 else item.occurred_at_ms - ordered[index - 1].occurred_at_ms
            ),
            complete=item.complete,
        )
        for index, item in enumerate(ordered)
    )
    observed_age = max(item.evidence_age_seconds for item in ordered)
    reasons: list[str] = []
    if observed_age > max_evidence_age_seconds:
        status: TimelineStatus = "STALE"
        reasons.append("SETTLEMENT_TIMELINE_EVIDENCE_STALE")
    else:
        reasons.extend(f"STAGE_MISSING:{stage}" for stage in gaps)
        reasons.extend(f"EVENT_INCOMPLETE:{item.event_id}" for item in ordered if not item.complete)
        if reasons:
            status = "INCOMPLETE"
        elif stage_indexes[-1] >= STAGE_ORDER.index("SETTLEMENT_OBSERVED"):
            status = "SETTLED"
        else:
            status = "PENDING"

    first = ordered[0]
    events_hash = _hash([asdict(item) for item in ordered])
    unsigned = {
        "schema_version": TIMELINE_SCHEMA_VERSION,
        "status": status,
        "reasons": sorted(reasons),
        "paper_order_id": first.paper_order_id,
        "ticker": first.ticker,
        "forecast_id": first.forecast_id,
        "source_identity_hash": first.source_identity_hash,
        "source_watermark": first.source_watermark,
        "event_count": len(items),
        "last_stage": ordered[-1].stage,
        "total_elapsed_ms": ordered[-1].occurred_at_ms - ordered[0].occurred_at_ms,
        "observed_max_age_seconds": observed_age,
        "max_evidence_age_seconds": max_evidence_age_seconds,
        "items": [asdict(item) for item in items],
        "events_hash": events_hash,
        "read_only": True,
        "execution_authorized": False,
    }
    return DashboardSettlementTimeline(
        status=status,
        reasons=tuple(unsigned["reasons"]),
        paper_order_id=first.paper_order_id,
        ticker=first.ticker,
        forecast_id=first.forecast_id,
        source_identity_hash=first.source_identity_hash,
        source_watermark=first.source_watermark,
        event_count=len(items),
        last_stage=ordered[-1].stage,
        total_elapsed_ms=unsigned["total_elapsed_ms"],
        observed_max_age_seconds=observed_age,
        max_evidence_age_seconds=max_evidence_age_seconds,
        items=items,
        events_hash=events_hash,
        timeline_hash=_hash(unsigned),
    )


def validate_dashboard_settlement_timeline(timeline: Any) -> None:
    if not isinstance(timeline, DashboardSettlementTimeline):
        raise DashboardSettlementTimelineError("TIMELINE_RESULT_TYPE_INVALID")
    if timeline.read_only is not True or timeline.execution_authorized is not False:
        raise DashboardSettlementTimelineError("TIMELINE_SAFETY_BOUNDARY_INVALID")
    if timeline.status in {"PENDING", "SETTLED"} and timeline.reasons:
        raise DashboardSettlementTimelineError("TIMELINE_STATE_INVALID")
    if timeline.status in {"INCOMPLETE", "STALE"} and not timeline.reasons:
        raise DashboardSettlementTimelineError("TIMELINE_REASONS_MISSING")
    unsigned = asdict(timeline)
    unsigned.pop("timeline_hash")
    unsigned["schema_version"] = TIMELINE_SCHEMA_VERSION
    unsigned["reasons"] = sorted(unsigned["reasons"])
    unsigned["items"] = [asdict(item) for item in timeline.items]
    if timeline.timeline_hash != _hash(unsigned):
        raise DashboardSettlementTimelineError("TIMELINE_HASH_MISMATCH")


def _validated_event(value: Any) -> DashboardSettlementEvent:
    if not isinstance(value, DashboardSettlementEvent):
        raise DashboardSettlementTimelineError("EVENT_TYPE_INVALID")
    unsigned = asdict(value)
    supplied_hash = unsigned.pop("event_hash")
    _validate_event_fields(unsigned)
    if supplied_hash != _hash(unsigned):
        raise DashboardSettlementTimelineError("EVENT_HASH_MISMATCH")
    return value


def _validate_event_fields(payload: dict[str, Any]) -> None:
    for key in (
        "event_id",
        "ticker",
        "source_identity_hash",
        "source_watermark",
        "lineage_hash",
    ):
        if not isinstance(payload[key], str) or not payload[key]:
            raise DashboardSettlementTimelineError("EVENT_FIELD_INVALID")
    if payload["stage"] not in STAGE_ORDER:
        raise DashboardSettlementTimelineError("EVENT_STAGE_INVALID")
    for key in ("occurred_at_ms", "paper_order_id", "forecast_id", "evidence_age_seconds"):
        value = payload[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise DashboardSettlementTimelineError("EVENT_FIELD_INVALID")
    if not isinstance(payload["complete"], bool):
        raise DashboardSettlementTimelineError("EVENT_FIELD_INVALID")


def _hash(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
