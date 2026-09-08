from __future__ import annotations

from dataclasses import replace

import pytest

from kalshi_predictor.ui.dashboard_loading_state_contract import (
    DashboardLoadingStateContractError,
    build_dashboard_loading_state_contract,
    make_dashboard_panel_load_observation,
    validate_dashboard_loading_state_contract,
)
from kalshi_predictor.ui.dashboard_panel_registry import (
    build_dashboard_panel_registry,
    make_dashboard_panel_descriptor,
)
from kalshi_predictor.ui.dashboard_progressive_disclosure import (
    build_dashboard_progressive_disclosure,
    make_dashboard_panel_disclosure_input,
)


def test_valid_loaded_and_deferred_states_are_settled_and_deterministic() -> None:
    first = _contract([_observation("summary", state="LOADED", count=0), _observation("detail")])
    second = _contract([_observation("detail"), _observation("summary", state="LOADED", count=0)])
    validate_dashboard_loading_state_contract(first)
    assert first.status == "SETTLED"
    assert [item.state for item in first.panels] == ["READY", "DEFERRED"]
    assert first.contract_hash == second.contract_hash
    assert first.execution_authorized is False


def test_empty_partial_and_observation_bound_fail_closed() -> None:
    with pytest.raises(DashboardLoadingStateContractError, match="OBSERVATIONS_EMPTY"):
        _contract([])
    with pytest.raises(DashboardLoadingStateContractError, match="OBSERVATION_SET_INVALID"):
        _contract([_observation("summary")])
    with pytest.raises(DashboardLoadingStateContractError, match="OBSERVATION_BOUND_EXCEEDED"):
        _contract([_observation("summary"), _observation("detail")], max_observations=1)


def test_exact_timeout_boundary_loads_and_one_millisecond_over_times_out() -> None:
    exact = _contract(
        [_observation("summary", state="LOADING", elapsed=2000), _observation("detail")]
    )
    assert exact.status == "LOADING"
    late = _contract(
        [_observation("summary", state="LOADING", elapsed=2001), _observation("detail")]
    )
    assert late.status == "DEGRADED"
    assert late.panels[0].state == "TIMED_OUT"


def test_stale_failed_and_zero_result_states_are_honest() -> None:
    stale = _contract([_observation("summary", age=301), _observation("detail", age=301)])
    assert stale.status == "STALE"
    assert all(item.state == "UNAVAILABLE" for item in stale.panels)
    failed = _contract([_observation("summary", state="FAILED"), _observation("detail")])
    assert failed.status == "DEGRADED"
    loaded_empty = _contract(
        [_observation("summary", state="LOADED", count=0), _observation("detail")]
    )
    assert loaded_empty.panels[0].state == "READY"
    assert loaded_empty.panels[0].result_count == 0


def test_malformed_deferred_activity_lineage_and_tampering_fail_closed() -> None:
    with pytest.raises(DashboardLoadingStateContractError, match="LOADED_RESULT_COUNT_MISSING"):
        _observation("summary", state="LOADED")
    with pytest.raises(DashboardLoadingStateContractError, match="DEFERRED_PANEL_ACTIVITY_INVALID"):
        _contract([_observation("summary"), _observation("detail", state="LOADING")])
    with pytest.raises(DashboardLoadingStateContractError, match="OBSERVATION_LINEAGE_MISMATCH"):
        _contract([_observation("summary"), _observation("detail", identity="b" * 64)])
    item = _observation("summary")
    with pytest.raises(DashboardLoadingStateContractError, match="OBSERVATION_HASH_MISMATCH"):
        _contract([replace(item, elapsed_ms=1), _observation("detail")])


def test_result_tampering_and_safety_boundary_fail_closed() -> None:
    contract = _contract([_observation("summary"), _observation("detail")])
    with pytest.raises(DashboardLoadingStateContractError, match="CONTRACT_HASH_MISMATCH"):
        validate_dashboard_loading_state_contract(replace(contract, contract_hash="0" * 64))
    with pytest.raises(
        DashboardLoadingStateContractError, match="CONTRACT_SAFETY_BOUNDARY_INVALID"
    ):
        validate_dashboard_loading_state_contract(replace(contract, execution_authorized=True))


def test_contract_has_no_query_publication_or_mutation_surface() -> None:
    names = set(build_dashboard_loading_state_contract.__code__.co_names)
    assert names.isdisjoint({"commit", "connect", "execute", "open", "publish", "unlink", "write"})


def _disclosure():
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
    return build_dashboard_progressive_disclosure(
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


def _observation(
    panel_id: str,
    *,
    state: str = "NOT_STARTED",
    elapsed: int = 0,
    count=None,
    age: int = 1,
    identity: str = "a" * 64,
):
    return make_dashboard_panel_load_observation(
        panel_id=panel_id,
        state=state,
        elapsed_ms=elapsed,
        result_count=count,
        source_identity_hash=identity,
        source_watermark="w",
        evidence_age_seconds=age,
    )


def _contract(observations, **kwargs):
    return build_dashboard_loading_state_contract(
        disclosure=_disclosure(), observations=observations, **kwargs
    )
