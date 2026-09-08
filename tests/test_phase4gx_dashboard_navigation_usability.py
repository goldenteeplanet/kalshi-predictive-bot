from __future__ import annotations

from dataclasses import replace

import pytest

from kalshi_predictor.ui.dashboard_navigation_usability import (
    DashboardNavigationUsabilityError,
    audit_dashboard_navigation_usability,
    make_navigation_route_evidence,
    validate_dashboard_navigation_usability_audit,
)


def test_valid_navigation_is_deterministic_and_read_only() -> None:
    first = audit_dashboard_navigation_usability(list(reversed(_routes())))
    second = audit_dashboard_navigation_usability(_routes())
    validate_dashboard_navigation_usability_audit(first)
    assert first.status == "PASS"
    assert first.audit_hash == second.audit_hash
    assert first.execution_authorized is False


def test_empty_and_route_bound_fail_closed() -> None:
    with pytest.raises(DashboardNavigationUsabilityError, match="ROUTES_EMPTY"):
        audit_dashboard_navigation_usability([])
    with pytest.raises(DashboardNavigationUsabilityError, match="ROUTE_BOUND_EXCEEDED"):
        audit_dashboard_navigation_usability(_routes(), max_routes=1)


def test_exact_interaction_and_freshness_boundaries_pass() -> None:
    exact = audit_dashboard_navigation_usability([_route("status", interactions=3, age=300)])
    assert exact.status == "PASS"
    assert audit_dashboard_navigation_usability([_route("status", interactions=4)]).status == "FAIL"
    assert audit_dashboard_navigation_usability([_route("status", age=301)]).status == "STALE"


def test_usability_and_partial_failures_are_explicit() -> None:
    result = audit_dashboard_navigation_usability(
        [_route("status", label="", keyboard=False, current=False, available=False, complete=False)]
    )
    assert result.status == "FAIL"
    assert result.violation_count == 5
    assert "DESTINATION_UNAVAILABLE:status" in result.reasons


def test_malformed_duplicate_lineage_and_tampering_fail_closed() -> None:
    with pytest.raises(DashboardNavigationUsabilityError, match="ROUTE_FIELD_INVALID"):
        _route("status", interactions=-1)
    with pytest.raises(DashboardNavigationUsabilityError, match="ROUTE_ID_DUPLICATE"):
        audit_dashboard_navigation_usability([_route("same"), _route("same")])
    with pytest.raises(DashboardNavigationUsabilityError, match="ROUTE_LINEAGE_MIXED"):
        audit_dashboard_navigation_usability([_route("a"), _route("b", identity="b" * 64)])
    item = _route("status")
    with pytest.raises(DashboardNavigationUsabilityError, match="ROUTE_HASH_MISMATCH"):
        audit_dashboard_navigation_usability([replace(item, destination_available=False)])


def test_result_tampering_and_safety_boundary_fail_closed() -> None:
    result = audit_dashboard_navigation_usability(_routes())
    with pytest.raises(DashboardNavigationUsabilityError, match="AUDIT_HASH_MISMATCH"):
        validate_dashboard_navigation_usability_audit(replace(result, audit_hash="0" * 64))
    with pytest.raises(DashboardNavigationUsabilityError, match="AUDIT_SAFETY_BOUNDARY_INVALID"):
        validate_dashboard_navigation_usability_audit(replace(result, execution_authorized=True))


def test_audit_has_no_query_navigation_or_mutation_surface() -> None:
    names = set(audit_dashboard_navigation_usability.__code__.co_names)
    assert names.isdisjoint(
        {"click", "commit", "connect", "execute", "navigate", "open", "publish", "write"}
    )


def _route(
    route_id,
    *,
    label="Evidence",
    interactions=1,
    keyboard=True,
    current=True,
    available=True,
    complete=True,
    identity="a" * 64,
    age=1,
):
    return make_navigation_route_evidence(
        route_id=route_id,
        visible_label=label,
        interaction_count=interactions,
        keyboard_reachable=keyboard,
        current_location_exposed=current,
        destination_available=available,
        complete=complete,
        source_identity_hash=identity,
        source_watermark="w",
        evidence_age_seconds=age,
    )


def _routes():
    return [_route("overview"), _route("evidence", interactions=2)]
