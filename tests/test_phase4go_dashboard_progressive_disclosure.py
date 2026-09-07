from __future__ import annotations

from dataclasses import replace

import pytest
from kalshi_predictor.ui.dashboard_panel_registry import (
    build_dashboard_panel_registry,
    make_dashboard_panel_descriptor,
)
from kalshi_predictor.ui.dashboard_progressive_disclosure import (
    DashboardProgressiveDisclosureError,
    build_dashboard_progressive_disclosure,
    make_dashboard_panel_disclosure_input,
    validate_dashboard_progressive_disclosure,
)


def test_valid_disclosure_is_deterministic_bounded_and_opt_in() -> None:
    first = _disclosure(_inputs())
    second = _disclosure(list(reversed(_inputs())))
    validate_dashboard_progressive_disclosure(first)
    assert first.status == "READY"
    assert [item.mode for item in first.panels] == ["SUMMARY", "DEFERRED"]
    assert first.disclosure_hash == second.disclosure_hash
    assert first.execution_authorized is False


def test_empty_partial_and_bounds_fail_closed() -> None:
    with pytest.raises(DashboardProgressiveDisclosureError, match="PANEL_INPUTS_EMPTY"):
        _disclosure([])
    with pytest.raises(DashboardProgressiveDisclosureError, match="PANEL_INPUT_SET_INVALID"):
        _disclosure(_inputs()[:-1])
    requested = [
        _input("summary", cost="EXPENSIVE", requested=True),
        _input("detail", requested=True),
    ]
    with pytest.raises(DashboardProgressiveDisclosureError, match="DETAIL_PANEL_BOUND_EXCEEDED"):
        _disclosure(requested, max_detail_panels=1)


def test_exact_freshness_boundary_is_ready_and_one_second_over_is_stale() -> None:
    assert _disclosure(_inputs(age=300)).status == "READY"
    stale = _disclosure(_inputs(age=301))
    assert stale.status == "STALE"
    assert all(item.mode == "UNAVAILABLE" for item in stale.panels)


def test_expensive_detail_requires_request_and_unavailable_is_honest() -> None:
    requested = _disclosure([_input("summary", cost="CHEAP"), _input("detail", requested=True)])
    assert requested.panels[1].mode == "DETAIL"
    assert requested.panels[1].detail_query_allowed is True
    unavailable = _disclosure(
        [_input("summary", cost="CHEAP"), _input("detail", available=False, requested=True)]
    )
    assert unavailable.panels[1].mode == "UNAVAILABLE"
    assert unavailable.panels[1].detail_query_allowed is False


def test_malformed_duplicate_lineage_and_tampering_fail_closed() -> None:
    with pytest.raises(DashboardProgressiveDisclosureError, match="PANEL_COST_CLASS_INVALID"):
        _input("summary", cost="UNKNOWN")
    with pytest.raises(DashboardProgressiveDisclosureError, match="PANEL_INPUT_DUPLICATE"):
        _disclosure([_input("summary"), _input("summary")])
    with pytest.raises(DashboardProgressiveDisclosureError, match="PANEL_LINEAGE_MISMATCH"):
        _disclosure([_input("summary"), _input("detail", identity="b" * 64)])
    item = _input("detail")
    with pytest.raises(DashboardProgressiveDisclosureError, match="PANEL_INPUT_HASH_MISMATCH"):
        _disclosure([_input("summary"), replace(item, available=False)])


def test_result_tampering_and_safety_boundary_fail_closed() -> None:
    disclosure = _disclosure(_inputs())
    with pytest.raises(DashboardProgressiveDisclosureError, match="DISCLOSURE_HASH_MISMATCH"):
        validate_dashboard_progressive_disclosure(replace(disclosure, disclosure_hash="0" * 64))
    with pytest.raises(
        DashboardProgressiveDisclosureError, match="DISCLOSURE_SAFETY_BOUNDARY_INVALID"
    ):
        validate_dashboard_progressive_disclosure(replace(disclosure, execution_authorized=True))


def test_disclosure_has_no_query_publication_or_mutation_surface() -> None:
    names = set(build_dashboard_progressive_disclosure.__code__.co_names)
    assert names.isdisjoint(
        {"commit", "connect", "execute", "open", "publish", "replace", "unlink", "write"}
    )


def _registry():
    panels = [
        make_dashboard_panel_descriptor(
            panel_id=panel_id,
            title=panel_id,
            route="/evidence#" + panel_id,
            artifact_schema_version="v1",
            source_identity_hash="a" * 64,
            source_watermark="w",
            evidence_age_seconds=1,
            priority=priority,
            required=True,
            enabled=True,
        )
        for priority, panel_id in enumerate(("summary", "detail"), start=1)
    ]
    return build_dashboard_panel_registry(panels)


def _inputs(*, age: int = 1):
    return [_input("summary", cost="CHEAP", age=age), _input("detail", age=age)]


def _input(
    panel_id: str,
    *,
    cost: str = "EXPENSIVE",
    available: bool = True,
    requested: bool = False,
    age: int = 1,
    identity: str = "a" * 64,
):
    return make_dashboard_panel_disclosure_input(
        panel_id=panel_id,
        cost_class=cost,
        available=available,
        detail_requested=requested,
        source_identity_hash=identity,
        source_watermark="w",
        evidence_age_seconds=age,
    )


def _disclosure(inputs, **kwargs):
    return build_dashboard_progressive_disclosure(
        registry=_registry(), panel_inputs=inputs, **kwargs
    )
