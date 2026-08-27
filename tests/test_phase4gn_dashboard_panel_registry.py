from __future__ import annotations

from dataclasses import replace

import pytest

from kalshi_predictor.ui.dashboard_panel_registry import (
    DashboardPanelRegistryError,
    build_dashboard_panel_registry,
    make_dashboard_panel_descriptor,
    validate_dashboard_panel_registry,
)


def test_valid_registry_is_deterministic_ordered_and_read_only() -> None:
    first = build_dashboard_panel_registry([_panel("b", priority=2), _panel("a", priority=1)])
    second = build_dashboard_panel_registry([_panel("a", priority=1), _panel("b", priority=2)])
    validate_dashboard_panel_registry(first)
    assert first.status == "READY"
    assert first.panel_ids == ("a", "b")
    assert first.registry_hash == second.registry_hash
    assert first.read_only is True
    assert first.execution_authorized is False


def test_empty_and_panel_bound_fail_closed() -> None:
    with pytest.raises(DashboardPanelRegistryError, match="PANELS_EMPTY"):
        build_dashboard_panel_registry([])
    with pytest.raises(DashboardPanelRegistryError, match="PANEL_BOUND_EXCEEDED"):
        build_dashboard_panel_registry(
            [_panel("a", priority=1), _panel("b", priority=2)], max_panels=1
        )


def test_exact_freshness_boundary_is_ready_and_one_second_over_is_stale() -> None:
    assert build_dashboard_panel_registry([_panel("a", age=300)]).status == "READY"
    stale = build_dashboard_panel_registry([_panel("a", age=301)])
    assert stale.status == "STALE"
    assert stale.reasons == ("PANEL_REGISTRY_EVIDENCE_STALE",)


def test_required_disabled_blocks_but_optional_disabled_is_honest() -> None:
    blocked = build_dashboard_panel_registry([_panel("a", required=True, enabled=False)])
    assert blocked.status == "BLOCKED"
    assert blocked.reasons == ("REQUIRED_PANEL_DISABLED:a",)
    optional = build_dashboard_panel_registry([_panel("a", required=False, enabled=False)])
    assert optional.status == "READY"
    assert optional.enabled_panel_count == 0


def test_malformed_duplicate_lineage_and_tampering_fail_closed() -> None:
    with pytest.raises(DashboardPanelRegistryError, match="PANEL_ROUTE_INVALID"):
        _panel("a", route="https://example.test")
    with pytest.raises(DashboardPanelRegistryError, match="PANEL_ID_DUPLICATE"):
        build_dashboard_panel_registry([_panel("a", priority=1), _panel("a", priority=2)])
    with pytest.raises(DashboardPanelRegistryError, match="PANEL_LINEAGE_MIXED"):
        build_dashboard_panel_registry(
            [_panel("a", priority=1), _panel("b", priority=2, identity="b" * 64)]
        )
    item = _panel("a")
    with pytest.raises(DashboardPanelRegistryError, match="PANEL_HASH_MISMATCH"):
        build_dashboard_panel_registry([replace(item, title="tampered")])


def test_result_tampering_and_safety_boundary_fail_closed() -> None:
    registry = build_dashboard_panel_registry([_panel("a")])
    with pytest.raises(DashboardPanelRegistryError, match="REGISTRY_HASH_MISMATCH"):
        validate_dashboard_panel_registry(replace(registry, registry_hash="0" * 64))
    with pytest.raises(DashboardPanelRegistryError, match="REGISTRY_SAFETY_BOUNDARY_INVALID"):
        validate_dashboard_panel_registry(replace(registry, execution_authorized=True))


def test_registry_has_no_query_publication_or_mutation_surface() -> None:
    names = set(build_dashboard_panel_registry.__code__.co_names)
    assert names.isdisjoint(
        {"commit", "connect", "execute", "open", "publish", "replace", "unlink", "write"}
    )


def _panel(
    panel_id: str,
    *,
    priority: int = 0,
    age: int = 1,
    route: str | None = None,
    identity: str = "a" * 64,
    required: bool = True,
    enabled: bool = True,
):
    return make_dashboard_panel_descriptor(
        panel_id=panel_id,
        title="Panel " + panel_id,
        route=route or "/evidence#" + panel_id,
        artifact_schema_version="phase4gm-evidence-latency-workstream-gate-v1",
        source_identity_hash=identity,
        source_watermark="w",
        evidence_age_seconds=age,
        priority=priority,
        required=required,
        enabled=enabled,
    )
