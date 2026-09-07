from __future__ import annotations

from dataclasses import replace

import pytest
from kalshi_predictor.ui.dashboard_loading_state_contract import (
    build_dashboard_loading_state_contract,
    make_dashboard_panel_load_observation,
)
from kalshi_predictor.ui.dashboard_panel_registry import (
    build_dashboard_panel_registry,
    make_dashboard_panel_descriptor,
)
from kalshi_predictor.ui.dashboard_partial_data_semantics import (
    DashboardPartialDataSemanticsError,
    evaluate_dashboard_partial_data_semantics,
    make_dashboard_panel_completeness_evidence,
    validate_dashboard_partial_data_semantics,
)
from kalshi_predictor.ui.dashboard_progressive_disclosure import (
    build_dashboard_progressive_disclosure,
    make_dashboard_panel_disclosure_input,
)


def test_complete_empty_and_deferred_states_are_deterministic() -> None:
    first = _semantics([_evidence("summary", items=0, success=1, total=1), _evidence("detail")])
    second = _semantics([_evidence("detail"), _evidence("summary", items=0, success=1, total=1)])
    validate_dashboard_partial_data_semantics(first)
    assert first.status == "COMPLETE"
    assert [item.state for item in first.panels] == ["EMPTY", "DEFERRED"]
    assert first.semantics_hash == second.semantics_hash
    assert first.execution_authorized is False


def test_empty_partial_and_bounds_fail_closed() -> None:
    with pytest.raises(DashboardPartialDataSemanticsError, match="EVIDENCE_EMPTY"):
        _semantics([])
    with pytest.raises(DashboardPartialDataSemanticsError, match="EVIDENCE_PANEL_SET_INVALID"):
        _semantics([_evidence("summary", items=0, success=1, total=1)])
    with pytest.raises(DashboardPartialDataSemanticsError, match="COMPONENT_BOUND_EXCEEDED"):
        _semantics([_evidence("summary", items=0, success=17, total=17), _evidence("detail")])


def test_exact_component_boundary_is_complete_and_partial_failure_is_partial() -> None:
    exact = _semantics([_evidence("summary", items=2, success=16, total=16), _evidence("detail")])
    assert exact.status == "COMPLETE"
    partial = _semantics([_evidence("summary", items=2, success=15, total=16), _evidence("detail")])
    assert partial.status == "PARTIAL"
    assert partial.panels[0].state == "PARTIAL"


def test_staleness_and_truncation_are_honest() -> None:
    stale = _semantics(
        [_evidence("summary", items=0, success=1, total=1, age=301), _evidence("detail", age=301)]
    )
    assert stale.status == "STALE"
    assert all(item.state == "UNAVAILABLE" for item in stale.panels)
    truncated = _semantics(
        [_evidence("summary", items=2, success=1, total=1, truncated=True), _evidence("detail")]
    )
    assert truncated.status == "PARTIAL"


def test_malformed_count_lineage_and_tampering_fail_closed() -> None:
    with pytest.raises(DashboardPartialDataSemanticsError, match="EVIDENCE_COMPONENTS_INVALID"):
        _evidence("summary", items=1, success=2, total=1)
    with pytest.raises(DashboardPartialDataSemanticsError, match="RESULT_COUNT_MISMATCH"):
        _semantics(
            [_evidence("summary", items=1, success=1, total=1), _evidence("detail")],
            loading_result_count=0,
        )
    with pytest.raises(DashboardPartialDataSemanticsError, match="EVIDENCE_LINEAGE_MISMATCH"):
        _semantics(
            [
                _evidence("summary", items=0, success=1, total=1),
                _evidence("detail", identity="b" * 64),
            ]
        )
    item = _evidence("summary", items=0, success=1, total=1)
    with pytest.raises(DashboardPartialDataSemanticsError, match="EVIDENCE_HASH_MISMATCH"):
        _semantics([replace(item, truncated=True), _evidence("detail")])


def test_result_tampering_and_safety_boundary_fail_closed() -> None:
    semantics = _semantics([_evidence("summary", items=0, success=1, total=1), _evidence("detail")])
    with pytest.raises(DashboardPartialDataSemanticsError, match="SEMANTICS_HASH_MISMATCH"):
        validate_dashboard_partial_data_semantics(replace(semantics, semantics_hash="0" * 64))
    with pytest.raises(
        DashboardPartialDataSemanticsError, match="SEMANTICS_SAFETY_BOUNDARY_INVALID"
    ):
        validate_dashboard_partial_data_semantics(replace(semantics, execution_authorized=True))


def test_semantics_has_no_query_publication_or_mutation_surface() -> None:
    names = set(evaluate_dashboard_partial_data_semantics.__code__.co_names)
    assert names.isdisjoint({"commit", "connect", "execute", "open", "publish", "unlink", "write"})


def _loading_contract(result_count=0):
    registry = build_dashboard_panel_registry(
        [
            make_dashboard_panel_descriptor(
                panel_id=p,
                title=p,
                route="/evidence#" + p,
                artifact_schema_version="v1",
                source_identity_hash="a" * 64,
                source_watermark="w",
                evidence_age_seconds=1,
                priority=i,
                required=True,
                enabled=True,
            )
            for i, p in enumerate(("summary", "detail"), 1)
        ]
    )
    disclosure = build_dashboard_progressive_disclosure(
        registry=registry,
        panel_inputs=[
            make_dashboard_panel_disclosure_input(
                panel_id="summary",
                cost_class="CHEAP",
                available=True,
                detail_requested=False,
                source_identity_hash="a" * 64,
                source_watermark="w",
                evidence_age_seconds=1,
            ),
            make_dashboard_panel_disclosure_input(
                panel_id="detail",
                cost_class="EXPENSIVE",
                available=True,
                detail_requested=False,
                source_identity_hash="a" * 64,
                source_watermark="w",
                evidence_age_seconds=1,
            ),
        ],
    )
    return build_dashboard_loading_state_contract(
        disclosure=disclosure,
        observations=[
            make_dashboard_panel_load_observation(
                panel_id="summary",
                state="LOADED",
                elapsed_ms=1,
                result_count=result_count,
                source_identity_hash="a" * 64,
                source_watermark="w",
                evidence_age_seconds=1,
            ),
            make_dashboard_panel_load_observation(
                panel_id="detail",
                state="NOT_STARTED",
                elapsed_ms=0,
                result_count=None,
                source_identity_hash="a" * 64,
                source_watermark="w",
                evidence_age_seconds=1,
            ),
        ],
    )


def _evidence(
    panel_id: str, *, items=None, success=0, total=0, truncated=False, age=1, identity="a" * 64
):
    return make_dashboard_panel_completeness_evidence(
        panel_id=panel_id,
        observed_items=items,
        successful_components=success,
        total_components=total,
        truncated=truncated,
        source_identity_hash=identity,
        source_watermark="w",
        evidence_age_seconds=age,
    )


def _semantics(evidence, loading_result_count=None, **kwargs):
    summary = next((item for item in evidence if item.panel_id == "summary"), None)
    result_count = (
        loading_result_count
        if loading_result_count is not None
        else summary.observed_items
        if summary and summary.observed_items is not None
        else 0
    )
    return evaluate_dashboard_partial_data_semantics(
        loading_contract=_loading_contract(result_count), evidence=evidence, **kwargs
    )
