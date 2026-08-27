from __future__ import annotations

from dataclasses import replace

import pytest

from kalshi_predictor.ui.dashboard_settlement_timeline import (
    DashboardSettlementTimelineError,
    build_dashboard_settlement_timeline,
    make_dashboard_settlement_event,
    validate_dashboard_settlement_timeline,
)


def test_pending_timeline_is_deterministic_and_read_only() -> None:
    first = build_dashboard_settlement_timeline(list(reversed(_pending_events())))
    second = build_dashboard_settlement_timeline(_pending_events())
    validate_dashboard_settlement_timeline(first)
    assert first.status == "PENDING"
    assert first.total_elapsed_ms == 100
    assert first.timeline_hash == second.timeline_hash
    assert first.execution_authorized is False


def test_empty_and_event_bound_fail_closed() -> None:
    with pytest.raises(DashboardSettlementTimelineError, match="EVENTS_EMPTY"):
        build_dashboard_settlement_timeline([])
    with pytest.raises(DashboardSettlementTimelineError, match="EVENT_BOUND_EXCEEDED"):
        build_dashboard_settlement_timeline(_pending_events(), max_events=1)


def test_exact_freshness_boundary_is_pending_and_one_second_over_is_stale() -> None:
    assert build_dashboard_settlement_timeline(_pending_events(age=300)).status == "PENDING"
    stale = build_dashboard_settlement_timeline(_pending_events(age=301))
    assert stale.status == "STALE"
    assert stale.reasons == ("SETTLEMENT_TIMELINE_EVIDENCE_STALE",)


def test_settled_partial_and_incomplete_states_are_honest() -> None:
    settled = build_dashboard_settlement_timeline(
        _pending_events() + [_event("observed", "SETTLEMENT_OBSERVED", 1200)]
    )
    assert settled.status == "SETTLED"
    missing = build_dashboard_settlement_timeline(
        [_event("filled", "ORDER_FILLED", 1000), _event("observed", "SETTLEMENT_OBSERVED", 1200)]
    )
    assert missing.status == "INCOMPLETE"
    assert missing.reasons == ("STAGE_MISSING:AWAITING_SETTLEMENT",)
    incomplete = build_dashboard_settlement_timeline(
        [_event("filled", "ORDER_FILLED", 1000, complete=False)]
    )
    assert incomplete.reasons == ("EVENT_INCOMPLETE:filled",)


def test_malformed_duplicate_order_lineage_and_tampering_fail_closed() -> None:
    with pytest.raises(DashboardSettlementTimelineError, match="EVENT_FIELD_INVALID"):
        _event("bad", "ORDER_FILLED", -1)
    with pytest.raises(DashboardSettlementTimelineError, match="EVENT_ID_DUPLICATE"):
        build_dashboard_settlement_timeline(
            [_event("same", "ORDER_FILLED", 1), _event("same", "AWAITING_SETTLEMENT", 2)]
        )
    with pytest.raises(DashboardSettlementTimelineError, match="EVENT_STAGE_ORDER_INVALID"):
        build_dashboard_settlement_timeline(
            [_event("waiting", "AWAITING_SETTLEMENT", 1), _event("filled", "ORDER_FILLED", 2)]
        )
    with pytest.raises(DashboardSettlementTimelineError, match="EVENT_LINEAGE_MIXED"):
        build_dashboard_settlement_timeline(
            [
                _event("filled", "ORDER_FILLED", 1),
                _event("waiting", "AWAITING_SETTLEMENT", 2, ticker="other"),
            ]
        )
    item = _event("filled", "ORDER_FILLED", 1)
    with pytest.raises(DashboardSettlementTimelineError, match="EVENT_HASH_MISMATCH"):
        build_dashboard_settlement_timeline([replace(item, complete=False)])


def test_result_tampering_and_safety_boundary_fail_closed() -> None:
    timeline = build_dashboard_settlement_timeline(_pending_events())
    with pytest.raises(DashboardSettlementTimelineError, match="TIMELINE_HASH_MISMATCH"):
        validate_dashboard_settlement_timeline(replace(timeline, timeline_hash="0" * 64))
    with pytest.raises(DashboardSettlementTimelineError, match="TIMELINE_SAFETY_BOUNDARY_INVALID"):
        validate_dashboard_settlement_timeline(replace(timeline, execution_authorized=True))


def test_timeline_has_no_query_publication_or_mutation_surface() -> None:
    names = set(build_dashboard_settlement_timeline.__code__.co_names)
    assert names.isdisjoint({"commit", "connect", "execute", "open", "publish", "unlink", "write"})


def _event(event_id, stage, at, *, age=1, complete=True, ticker="KXRAINAUSM-26AUG-1"):
    return make_dashboard_settlement_event(
        event_id=event_id,
        stage=stage,
        occurred_at_ms=at,
        paper_order_id=204,
        ticker=ticker,
        forecast_id=523912,
        source_identity_hash="a" * 64,
        source_watermark="w",
        lineage_hash="lineage:231:231",
        evidence_age_seconds=age,
        complete=complete,
    )


def _pending_events(*, age=1):
    return [
        _event("filled", "ORDER_FILLED", 1000, age=age),
        _event("waiting", "AWAITING_SETTLEMENT", 1100, age=age),
    ]
