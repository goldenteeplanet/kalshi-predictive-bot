from __future__ import annotations

import copy
from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader, select_autoescape

from kalshi_predictor.phase4cd.read_model_chain import ReadModelChainResult
from kalshi_predictor.phase4cd.read_model_compatibility import CompatibilityResult
from kalshi_predictor.phase4cd.read_model_consumer import ReadModelView
from kalshi_predictor.phase4cd.read_model_provenance_dashboard import (
    ProvenanceDashboardError,
    build_provenance_dashboard,
    validate_provenance_dashboard,
)
from kalshi_predictor.phase4cd.read_model_staleness import StalenessEscalation

_DEFAULT = object()


def test_healthy_dashboard_preserves_provenance() -> None:
    model = _build()
    assert model["status"] == "HEALTHY"
    assert model["execution_authorized"] is False
    assert model["provenance"]["source_watermark"] == "paper_pnl:10"
    assert model["chain"]["head_hash"] == "d" * 64


def test_missing_component_renders_honest_unavailable_state() -> None:
    model = _build(chain=None)
    assert model["status"] == "UNAVAILABLE"
    assert model["missing_components"] == ["chain"]
    assert model["provenance"] is None


@pytest.mark.parametrize(
    ("decision", "staleness_status", "expected"),
    [
        ("INCOMPATIBLE", "FRESH", "BLOCKED"),
        ("COMPATIBLE", "WARNING", "WARNING"),
        ("COMPATIBLE", "STALE", "STALE"),
        ("COMPATIBLE", "STALLED", "STALLED"),
        ("COMPATIBLE", "LINEAGE_FAILURE", "LINEAGE_FAILURE"),
    ],
)
def test_dashboard_state_precedence(decision: str, staleness_status: str, expected: str) -> None:
    model = _build(
        compatibility=_compatibility(decision),
        staleness=_staleness(staleness_status),
    )
    assert model["status"] == expected
    assert model["execution_authorized"] is False


def test_identity_mismatch_and_lane_bound_fail_closed() -> None:
    chain = _chain(identity="f" * 64)
    with pytest.raises(ProvenanceDashboardError, match="SOURCE_IDENTITY_MISMATCH"):
        _build(chain=chain)
    with pytest.raises(ProvenanceDashboardError, match="LANE_FIELD_BOUND_EXCEEDED"):
        _build(max_lane_fields=0 + 1, view=_view(lane={"a": 1, "b": 2}))


def test_tampering_and_partial_dashboard_fail_closed() -> None:
    model = _build()
    tampered = copy.deepcopy(model)
    tampered["execution_authorized"] = True
    with pytest.raises(ProvenanceDashboardError, match="DASHBOARD_SAFETY_BOUNDARY_INVALID"):
        validate_provenance_dashboard(tampered)
    partial = copy.deepcopy(model)
    partial.pop("chain")
    with pytest.raises(ProvenanceDashboardError, match="DASHBOARD_FIELDS_INVALID"):
        validate_provenance_dashboard(partial)


def test_template_renders_healthy_and_unavailable_states() -> None:
    templates = Path(__file__).parents[1] / "src/kalshi_predictor/ui/templates"
    environment = Environment(loader=FileSystemLoader(templates), autoescape=select_autoescape())
    template = environment.from_string(
        "{% from 'read_model_provenance_panel.html' import read_model_provenance_panel %}"
        "{{ read_model_provenance_panel(model) }}"
    )
    healthy = template.render(model=_build())
    unavailable = template.render(model=_build(view=None))
    assert "paper_pnl:10" in healthy
    assert "Execution authorization: blocked" in healthy
    assert "Required evidence is unavailable: view" in unavailable


def test_dashboard_builder_is_pure_and_has_no_writer_surface() -> None:
    view = _view()
    original = copy.deepcopy(view)
    _build(view=view)
    assert view == original
    assert "execute" not in dir(build_provenance_dashboard)
    assert "commit" not in dir(build_provenance_dashboard)


def _build(
    *,
    view=_DEFAULT,
    compatibility=_DEFAULT,
    chain=_DEFAULT,
    staleness=_DEFAULT,
    max_lane_fields: int = 32,
):
    return build_provenance_dashboard(
        view=_view() if view is _DEFAULT else view,
        compatibility=(
            _compatibility("COMPATIBLE") if compatibility is _DEFAULT else compatibility
        ),
        chain=_chain() if chain is _DEFAULT else chain,
        staleness=_staleness("FRESH") if staleness is _DEFAULT else staleness,
        max_lane_fields=max_lane_fields,
    )


def _view(*, lane: dict | None = None) -> ReadModelView:
    return ReadModelView(
        schema_version="phase4fm-evidence-read-model-v1",
        generated_at="2026-08-27T00:00:00+00:00",
        source_watermark="paper_pnl:10",
        source_database_identity_hash="a" * 64,
        age_seconds=1,
        guarded_paper_settled=203,
        realized_pnl="0",
        paper_order_count=204,
        evidence_lane=lane or {"evaluated": 1},
        payload_hash="b" * 64,
        manifest_hash="c" * 64,
    )


def _compatibility(decision: str) -> CompatibilityResult:
    return CompatibilityResult(
        producer_schema="phase4fm-evidence-read-model-v1",
        consumer_schema="phase4fm-evidence-read-model-v1",
        decision=decision,
        reason="test",
        matrix_hash="e" * 64,
        age_seconds=1,
    )


def _chain(*, identity: str = "a" * 64) -> ReadModelChainResult:
    return ReadModelChainResult(
        node_count=2,
        genesis_hash="1" * 64,
        head_hash="d" * 64,
        source="paper_pnl",
        source_identity_hash=identity,
        first_sequence=9,
        last_sequence=10,
        progress=1,
        head_age_seconds=1,
    )


def _staleness(status: str) -> StalenessEscalation:
    return StalenessEscalation(
        status=status,
        action="NO_ACTION" if status == "FRESH" else "ESCALATE",
        snapshot_age_seconds=1,
        progress_age_seconds=1,
        reason="test",
        evidence_hash="f" * 64,
    )
