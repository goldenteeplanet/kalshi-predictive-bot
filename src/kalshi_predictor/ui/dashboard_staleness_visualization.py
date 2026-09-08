from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any, Literal

from kalshi_predictor.ui.dashboard_partial_data_semantics import (
    DashboardPartialDataSemantics,
    validate_dashboard_partial_data_semantics,
)

VISUALIZATION_SCHEMA_VERSION = "phase4gr-dashboard-staleness-visualization-v1"
VisualizationStatus = Literal["FRESH", "ATTENTION", "STALE"]
FreshnessBand = Literal["FRESH", "AGING", "STALE", "UNAVAILABLE"]


class DashboardStalenessVisualizationError(ValueError):
    """Stable fail-closed dashboard-staleness visualization error."""


@dataclass(frozen=True)
class DashboardPanelFreshnessEvidence:
    panel_id: str
    age_seconds: int
    observed_at: str
    source_identity_hash: str
    source_watermark: str
    evidence_hash: str


@dataclass(frozen=True)
class DashboardPanelStalenessToken:
    panel_id: str
    band: FreshnessBand
    severity: Literal["INFO", "WARNING", "CRITICAL"]
    color_token: Literal["SUCCESS", "WARNING", "DANGER", "MUTED"]
    text_label: str
    age_seconds: int
    observed_at: str


@dataclass(frozen=True)
class DashboardStalenessVisualization:
    status: VisualizationStatus
    reasons: tuple[str, ...]
    semantics_hash: str
    source_identity_hash: str
    source_watermark: str
    panel_count: int
    aging_after_seconds: int
    stale_after_seconds: int
    observed_max_age_seconds: int
    tokens: tuple[DashboardPanelStalenessToken, ...]
    evidence_set_hash: str
    visualization_hash: str
    read_only: bool = True
    execution_authorized: bool = False


def make_dashboard_panel_freshness_evidence(
    *,
    panel_id: str,
    age_seconds: int,
    observed_at: str,
    source_identity_hash: str,
    source_watermark: str,
) -> DashboardPanelFreshnessEvidence:
    unsigned = {
        "panel_id": panel_id,
        "age_seconds": age_seconds,
        "observed_at": observed_at,
        "source_identity_hash": source_identity_hash,
        "source_watermark": source_watermark,
    }
    _validate_evidence_fields(unsigned)
    return DashboardPanelFreshnessEvidence(**unsigned, evidence_hash=_hash(unsigned))


def build_dashboard_staleness_visualization(
    *,
    semantics: Any,
    evidence: Sequence[Any],
    max_panels: int = 32,
    aging_after_seconds: int = 60,
    stale_after_seconds: int = 300,
) -> DashboardStalenessVisualization:
    for value in (max_panels, aging_after_seconds, stale_after_seconds):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise DashboardStalenessVisualizationError("VISUALIZATION_BOUND_INVALID")
    if aging_after_seconds >= stale_after_seconds:
        raise DashboardStalenessVisualizationError("FRESHNESS_THRESHOLDS_INVALID")
    try:
        validate_dashboard_partial_data_semantics(semantics)
    except (TypeError, ValueError) as exc:
        raise DashboardStalenessVisualizationError("SEMANTICS_INPUT_INVALID") from exc
    if not isinstance(semantics, DashboardPartialDataSemantics):
        raise DashboardStalenessVisualizationError("SEMANTICS_INPUT_INVALID")
    if not evidence:
        raise DashboardStalenessVisualizationError("EVIDENCE_EMPTY")
    if len(evidence) > max_panels:
        raise DashboardStalenessVisualizationError("PANEL_BOUND_EXCEEDED")

    validated = [_validated_evidence(item) for item in evidence]
    ids = [item.panel_id for item in validated]
    if len(set(ids)) != len(ids):
        raise DashboardStalenessVisualizationError("EVIDENCE_PANEL_DUPLICATE")
    semantics_ids = [item.panel_id for item in semantics.panels]
    if set(ids) != set(semantics_ids):
        raise DashboardStalenessVisualizationError("EVIDENCE_PANEL_SET_INVALID")
    identities = {item.source_identity_hash for item in validated}
    watermarks = {item.source_watermark for item in validated}
    if identities != {semantics.source_identity_hash} or watermarks != {semantics.source_watermark}:
        raise DashboardStalenessVisualizationError("EVIDENCE_LINEAGE_MISMATCH")

    by_id = {item.panel_id: item for item in validated}
    ordered = [by_id[panel_id] for panel_id in semantics_ids]
    tokens = tuple(
        _token(
            panel.panel_id,
            panel.state,
            item,
            aging_after_seconds=aging_after_seconds,
            stale_after_seconds=stale_after_seconds,
            aggregate_stale=semantics.status == "STALE",
        )
        for panel, item in zip(semantics.panels, ordered, strict=True)
    )
    bands = {item.band for item in tokens}
    reasons: list[str] = []
    if "STALE" in bands or semantics.status == "STALE":
        status: VisualizationStatus = "STALE"
        reasons.append("STALE_PANEL_EVIDENCE")
    elif bands & {"AGING", "UNAVAILABLE"} or semantics.status == "PARTIAL":
        status = "ATTENTION"
        reasons.extend(
            sorted(
                f"PANEL_{item.band}:{item.panel_id}"
                for item in tokens
                if item.band in {"AGING", "UNAVAILABLE"}
            )
        )
        if semantics.status == "PARTIAL":
            reasons.append("PARTIAL_DATA_PRESENT")
    else:
        status = "FRESH"

    evidence_set_hash = _hash([asdict(item) for item in ordered])
    unsigned = {
        "schema_version": VISUALIZATION_SCHEMA_VERSION,
        "status": status,
        "reasons": sorted(set(reasons)),
        "semantics_hash": semantics.semantics_hash,
        "source_identity_hash": semantics.source_identity_hash,
        "source_watermark": semantics.source_watermark,
        "panel_count": len(tokens),
        "aging_after_seconds": aging_after_seconds,
        "stale_after_seconds": stale_after_seconds,
        "observed_max_age_seconds": max(item.age_seconds for item in ordered),
        "tokens": [asdict(item) for item in tokens],
        "evidence_set_hash": evidence_set_hash,
        "read_only": True,
        "execution_authorized": False,
    }
    return DashboardStalenessVisualization(
        status=status,
        reasons=tuple(unsigned["reasons"]),
        semantics_hash=semantics.semantics_hash,
        source_identity_hash=semantics.source_identity_hash,
        source_watermark=semantics.source_watermark,
        panel_count=len(tokens),
        aging_after_seconds=aging_after_seconds,
        stale_after_seconds=stale_after_seconds,
        observed_max_age_seconds=unsigned["observed_max_age_seconds"],
        tokens=tokens,
        evidence_set_hash=evidence_set_hash,
        visualization_hash=_hash(unsigned),
    )


def validate_dashboard_staleness_visualization(visualization: Any) -> None:
    if not isinstance(visualization, DashboardStalenessVisualization):
        raise DashboardStalenessVisualizationError("VISUALIZATION_RESULT_TYPE_INVALID")
    if visualization.read_only is not True or visualization.execution_authorized is not False:
        raise DashboardStalenessVisualizationError("VISUALIZATION_SAFETY_BOUNDARY_INVALID")
    if visualization.status == "FRESH" and visualization.reasons:
        raise DashboardStalenessVisualizationError("FRESH_STATE_INVALID")
    if visualization.status in {"ATTENTION", "STALE"} and not visualization.reasons:
        raise DashboardStalenessVisualizationError("NON_FRESH_REASONS_MISSING")
    if any(not item.text_label for item in visualization.tokens):
        raise DashboardStalenessVisualizationError("ACCESSIBLE_LABEL_MISSING")
    unsigned = asdict(visualization)
    unsigned.pop("visualization_hash")
    unsigned["schema_version"] = VISUALIZATION_SCHEMA_VERSION
    unsigned["reasons"] = sorted(unsigned["reasons"])
    unsigned["tokens"] = [asdict(item) for item in visualization.tokens]
    if visualization.visualization_hash != _hash(unsigned):
        raise DashboardStalenessVisualizationError("VISUALIZATION_HASH_MISMATCH")


def _token(
    panel_id: str,
    panel_state: str,
    evidence: DashboardPanelFreshnessEvidence,
    *,
    aging_after_seconds: int,
    stale_after_seconds: int,
    aggregate_stale: bool,
) -> DashboardPanelStalenessToken:
    if panel_state in {"UNAVAILABLE", "FAILED"}:
        band: FreshnessBand = "UNAVAILABLE"
    elif aggregate_stale or evidence.age_seconds > stale_after_seconds:
        band = "STALE"
    elif evidence.age_seconds > aging_after_seconds:
        band = "AGING"
    else:
        band = "FRESH"
    severity, color, label = {
        "FRESH": ("INFO", "SUCCESS", "Fresh"),
        "AGING": ("WARNING", "WARNING", "Aging"),
        "STALE": ("CRITICAL", "DANGER", "Stale"),
        "UNAVAILABLE": ("WARNING", "MUTED", "Unavailable"),
    }[band]
    return DashboardPanelStalenessToken(
        panel_id=panel_id,
        band=band,
        severity=severity,
        color_token=color,
        text_label=f"{label} — {evidence.age_seconds}s old",
        age_seconds=evidence.age_seconds,
        observed_at=evidence.observed_at,
    )


def _validated_evidence(value: Any) -> DashboardPanelFreshnessEvidence:
    if not isinstance(value, DashboardPanelFreshnessEvidence):
        raise DashboardStalenessVisualizationError("EVIDENCE_TYPE_INVALID")
    unsigned = asdict(value)
    supplied_hash = unsigned.pop("evidence_hash")
    _validate_evidence_fields(unsigned)
    if supplied_hash != _hash(unsigned):
        raise DashboardStalenessVisualizationError("EVIDENCE_HASH_MISMATCH")
    return value


def _validate_evidence_fields(payload: dict[str, Any]) -> None:
    for key in ("panel_id", "observed_at", "source_identity_hash", "source_watermark"):
        if not isinstance(payload[key], str) or not payload[key]:
            raise DashboardStalenessVisualizationError("EVIDENCE_FIELD_INVALID")
    age = payload["age_seconds"]
    if isinstance(age, bool) or not isinstance(age, int) or age < 0:
        raise DashboardStalenessVisualizationError("EVIDENCE_FIELD_INVALID")


def _hash(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
