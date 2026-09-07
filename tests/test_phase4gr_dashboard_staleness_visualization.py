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
    evaluate_dashboard_partial_data_semantics,
    make_dashboard_panel_completeness_evidence,
)
from kalshi_predictor.ui.dashboard_progressive_disclosure import (
    build_dashboard_progressive_disclosure,
    make_dashboard_panel_disclosure_input,
)
from kalshi_predictor.ui.dashboard_staleness_visualization import (
    DashboardStalenessVisualizationError,
    build_dashboard_staleness_visualization,
    make_dashboard_panel_freshness_evidence,
    validate_dashboard_staleness_visualization,
)


def test_fresh_tokens_are_deterministic_and_accessible() -> None:
    first = _visualization([_freshness("summary", 1), _freshness("detail", 1)])
    second = _visualization([_freshness("detail", 1), _freshness("summary", 1)])
    validate_dashboard_staleness_visualization(first)
    assert first.status == "FRESH"
    assert all(item.text_label and item.band == "FRESH" for item in first.tokens)
    assert first.visualization_hash == second.visualization_hash
    assert first.execution_authorized is False


def test_empty_partial_and_panel_bound_fail_closed() -> None:
    with pytest.raises(DashboardStalenessVisualizationError, match="EVIDENCE_EMPTY"):
        _visualization([])
    with pytest.raises(DashboardStalenessVisualizationError, match="EVIDENCE_PANEL_SET_INVALID"):
        _visualization([_freshness("summary", 1)])
    with pytest.raises(DashboardStalenessVisualizationError, match="PANEL_BOUND_EXCEEDED"):
        _visualization([_freshness("summary", 1), _freshness("detail", 1)], max_panels=1)


def test_exact_aging_and_stale_boundaries_are_not_crossed_early() -> None:
    fresh = _visualization([_freshness("summary", 60), _freshness("detail", 60)])
    assert fresh.status == "FRESH"
    aging = _visualization([_freshness("summary", 61), _freshness("detail", 61)])
    assert aging.status == "ATTENTION"
    assert all(item.band == "AGING" for item in aging.tokens)
    not_stale = _visualization([_freshness("summary", 300), _freshness("detail", 300)])
    assert all(item.band == "AGING" for item in not_stale.tokens)
    stale = _visualization([_freshness("summary", 301), _freshness("detail", 301)])
    assert stale.status == "STALE"


def test_partial_and_unavailable_states_require_attention() -> None:
    partial = _visualization([_freshness("summary", 1), _freshness("detail", 1)], partial=True)
    assert partial.status == "ATTENTION"
    unavailable = _visualization(
        [_freshness("summary", 1), _freshness("detail", 1)], summary_loaded=False
    )
    assert unavailable.status == "ATTENTION"
    assert unavailable.tokens[0].band == "UNAVAILABLE"


def test_malformed_duplicate_lineage_and_tampering_fail_closed() -> None:
    with pytest.raises(DashboardStalenessVisualizationError, match="EVIDENCE_FIELD_INVALID"):
        _freshness("summary", -1)
    with pytest.raises(DashboardStalenessVisualizationError, match="EVIDENCE_PANEL_DUPLICATE"):
        _visualization([_freshness("summary", 1), _freshness("summary", 1)])
    with pytest.raises(DashboardStalenessVisualizationError, match="EVIDENCE_LINEAGE_MISMATCH"):
        _visualization([_freshness("summary", 1), _freshness("detail", 1, identity="b" * 64)])
    item = _freshness("summary", 1)
    with pytest.raises(DashboardStalenessVisualizationError, match="EVIDENCE_HASH_MISMATCH"):
        _visualization([replace(item, age_seconds=2), _freshness("detail", 1)])


def test_result_tampering_and_safety_boundary_fail_closed() -> None:
    result = _visualization([_freshness("summary", 1), _freshness("detail", 1)])
    with pytest.raises(DashboardStalenessVisualizationError, match="VISUALIZATION_HASH_MISMATCH"):
        validate_dashboard_staleness_visualization(replace(result, visualization_hash="0" * 64))
    with pytest.raises(
        DashboardStalenessVisualizationError, match="VISUALIZATION_SAFETY_BOUNDARY_INVALID"
    ):
        validate_dashboard_staleness_visualization(replace(result, execution_authorized=True))


def test_visualization_has_no_query_publication_or_mutation_surface() -> None:
    names = set(build_dashboard_staleness_visualization.__code__.co_names)
    assert names.isdisjoint({"commit", "connect", "execute", "open", "publish", "unlink", "write"})


def _semantics(*, partial=False, summary_loaded=True):
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
                available=summary_loaded,
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
    observations = [
        make_dashboard_panel_load_observation(
            panel_id="summary",
            state="LOADED" if summary_loaded else "NOT_STARTED",
            elapsed_ms=1,
            result_count=1 if summary_loaded else None,
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
    ]
    loading = build_dashboard_loading_state_contract(
        disclosure=disclosure, observations=observations
    )
    evidence = [
        make_dashboard_panel_completeness_evidence(
            panel_id="summary",
            observed_items=1 if summary_loaded else None,
            successful_components=0 if not summary_loaded else 1,
            total_components=0 if not summary_loaded else 2 if partial else 1,
            truncated=False,
            source_identity_hash="a" * 64,
            source_watermark="w",
            evidence_age_seconds=1,
        ),
        make_dashboard_panel_completeness_evidence(
            panel_id="detail",
            observed_items=None,
            successful_components=0,
            total_components=0,
            truncated=False,
            source_identity_hash="a" * 64,
            source_watermark="w",
            evidence_age_seconds=1,
        ),
    ]
    return evaluate_dashboard_partial_data_semantics(loading_contract=loading, evidence=evidence)


def _freshness(panel_id, age, identity="a" * 64):
    return make_dashboard_panel_freshness_evidence(
        panel_id=panel_id,
        age_seconds=age,
        observed_at="2026-08-27T00:00:00Z",
        source_identity_hash=identity,
        source_watermark="w",
    )


def _visualization(evidence, **kwargs):
    semantic_keys = {
        key: kwargs.pop(key) for key in list(kwargs) if key in {"partial", "summary_loaded"}
    }
    return build_dashboard_staleness_visualization(
        semantics=_semantics(**semantic_keys), evidence=evidence, **kwargs
    )
