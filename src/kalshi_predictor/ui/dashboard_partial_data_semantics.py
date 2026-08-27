from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any, Literal

from kalshi_predictor.ui.dashboard_loading_state_contract import (
    DashboardLoadingStateContract,
    DashboardPanelLoadState,
    validate_dashboard_loading_state_contract,
)

SEMANTICS_SCHEMA_VERSION = "phase4gq-dashboard-partial-data-semantics-v1"
SemanticsStatus = Literal["COMPLETE", "PARTIAL", "PENDING", "STALE"]
PanelDataState = Literal[
    "COMPLETE", "EMPTY", "PARTIAL", "PENDING", "DEFERRED", "UNAVAILABLE", "FAILED"
]


class DashboardPartialDataSemanticsError(ValueError):
    """Stable fail-closed dashboard partial-data error."""


@dataclass(frozen=True)
class DashboardPanelCompletenessEvidence:
    panel_id: str
    observed_items: int | None
    successful_components: int
    total_components: int
    truncated: bool
    source_identity_hash: str
    source_watermark: str
    evidence_age_seconds: int
    evidence_hash: str


@dataclass(frozen=True)
class DashboardPanelDataSemantics:
    panel_id: str
    state: PanelDataState
    observed_items: int | None
    successful_components: int
    total_components: int


@dataclass(frozen=True)
class DashboardPartialDataSemantics:
    status: SemanticsStatus
    reasons: tuple[str, ...]
    loading_contract_hash: str
    source_identity_hash: str
    source_watermark: str
    panel_count: int
    complete_panel_count: int
    partial_panel_count: int
    max_components_per_panel: int
    observed_max_age_seconds: int
    max_evidence_age_seconds: int
    panels: tuple[DashboardPanelDataSemantics, ...]
    evidence_set_hash: str
    semantics_hash: str
    read_only: bool = True
    execution_authorized: bool = False


def make_dashboard_panel_completeness_evidence(
    *,
    panel_id: str,
    observed_items: int | None,
    successful_components: int,
    total_components: int,
    truncated: bool,
    source_identity_hash: str,
    source_watermark: str,
    evidence_age_seconds: int,
) -> DashboardPanelCompletenessEvidence:
    unsigned = {
        "panel_id": panel_id,
        "observed_items": observed_items,
        "successful_components": successful_components,
        "total_components": total_components,
        "truncated": truncated,
        "source_identity_hash": source_identity_hash,
        "source_watermark": source_watermark,
        "evidence_age_seconds": evidence_age_seconds,
    }
    _validate_evidence_fields(unsigned)
    return DashboardPanelCompletenessEvidence(**unsigned, evidence_hash=_hash(unsigned))


def evaluate_dashboard_partial_data_semantics(
    *,
    loading_contract: Any,
    evidence: Sequence[Any],
    max_panels: int = 32,
    max_components_per_panel: int = 16,
    max_evidence_age_seconds: int = 300,
) -> DashboardPartialDataSemantics:
    for value in (max_panels, max_components_per_panel):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise DashboardPartialDataSemanticsError("SEMANTICS_BOUND_INVALID")
    if (
        isinstance(max_evidence_age_seconds, bool)
        or not isinstance(max_evidence_age_seconds, int)
        or max_evidence_age_seconds < 0
    ):
        raise DashboardPartialDataSemanticsError("SEMANTICS_BOUND_INVALID")
    try:
        validate_dashboard_loading_state_contract(loading_contract)
    except (TypeError, ValueError) as exc:
        raise DashboardPartialDataSemanticsError("LOADING_CONTRACT_INPUT_INVALID") from exc
    if not isinstance(loading_contract, DashboardLoadingStateContract):
        raise DashboardPartialDataSemanticsError("LOADING_CONTRACT_INPUT_INVALID")
    if not evidence:
        raise DashboardPartialDataSemanticsError("EVIDENCE_EMPTY")
    if len(evidence) > max_panels:
        raise DashboardPartialDataSemanticsError("PANEL_BOUND_EXCEEDED")

    validated = [_validated_evidence(item) for item in evidence]
    ids = [item.panel_id for item in validated]
    if len(set(ids)) != len(ids):
        raise DashboardPartialDataSemanticsError("EVIDENCE_PANEL_DUPLICATE")
    contract_ids = [item.panel_id for item in loading_contract.panels]
    if set(ids) != set(contract_ids):
        raise DashboardPartialDataSemanticsError("EVIDENCE_PANEL_SET_INVALID")
    identities = {item.source_identity_hash for item in validated}
    watermarks = {item.source_watermark for item in validated}
    if identities != {loading_contract.source_identity_hash} or watermarks != {
        loading_contract.source_watermark
    }:
        raise DashboardPartialDataSemanticsError("EVIDENCE_LINEAGE_MISMATCH")
    if any(item.total_components > max_components_per_panel for item in validated):
        raise DashboardPartialDataSemanticsError("COMPONENT_BOUND_EXCEEDED")

    by_id = {item.panel_id: item for item in validated}
    ordered = [by_id[panel_id] for panel_id in contract_ids]
    observed_age = max(
        loading_contract.observed_max_age_seconds,
        *(item.evidence_age_seconds for item in ordered),
    )
    stale = loading_contract.status == "STALE" or observed_age > max_evidence_age_seconds
    panels = tuple(
        _panel_semantics(load_state, item, stale=stale)
        for load_state, item in zip(loading_contract.panels, ordered, strict=True)
    )

    reasons: list[str] = []
    states = {item.state for item in panels}
    if stale:
        status: SemanticsStatus = "STALE"
        reasons.append("PARTIAL_DATA_EVIDENCE_STALE")
    elif states & {"PARTIAL", "FAILED"}:
        status = "PARTIAL"
        reasons.extend(
            sorted(
                f"PANEL_{item.state}:{item.panel_id}"
                for item in panels
                if item.state in {"PARTIAL", "FAILED"}
            )
        )
    elif "PENDING" in states:
        status = "PENDING"
    else:
        status = "COMPLETE"

    evidence_set_hash = _hash([asdict(item) for item in ordered])
    unsigned = {
        "schema_version": SEMANTICS_SCHEMA_VERSION,
        "status": status,
        "reasons": sorted(reasons),
        "loading_contract_hash": loading_contract.contract_hash,
        "source_identity_hash": loading_contract.source_identity_hash,
        "source_watermark": loading_contract.source_watermark,
        "panel_count": len(panels),
        "complete_panel_count": sum(item.state in {"COMPLETE", "EMPTY"} for item in panels),
        "partial_panel_count": sum(item.state in {"PARTIAL", "FAILED"} for item in panels),
        "max_components_per_panel": max_components_per_panel,
        "observed_max_age_seconds": observed_age,
        "max_evidence_age_seconds": max_evidence_age_seconds,
        "panels": [asdict(item) for item in panels],
        "evidence_set_hash": evidence_set_hash,
        "read_only": True,
        "execution_authorized": False,
    }
    return DashboardPartialDataSemantics(
        status=status,
        reasons=tuple(unsigned["reasons"]),
        loading_contract_hash=loading_contract.contract_hash,
        source_identity_hash=loading_contract.source_identity_hash,
        source_watermark=loading_contract.source_watermark,
        panel_count=len(panels),
        complete_panel_count=unsigned["complete_panel_count"],
        partial_panel_count=unsigned["partial_panel_count"],
        max_components_per_panel=max_components_per_panel,
        observed_max_age_seconds=observed_age,
        max_evidence_age_seconds=max_evidence_age_seconds,
        panels=panels,
        evidence_set_hash=evidence_set_hash,
        semantics_hash=_hash(unsigned),
    )


def validate_dashboard_partial_data_semantics(semantics: Any) -> None:
    if not isinstance(semantics, DashboardPartialDataSemantics):
        raise DashboardPartialDataSemanticsError("SEMANTICS_RESULT_TYPE_INVALID")
    if semantics.read_only is not True or semantics.execution_authorized is not False:
        raise DashboardPartialDataSemanticsError("SEMANTICS_SAFETY_BOUNDARY_INVALID")
    if semantics.status in {"COMPLETE", "PENDING"} and semantics.reasons:
        raise DashboardPartialDataSemanticsError("SEMANTICS_STATE_INVALID")
    if semantics.status in {"PARTIAL", "STALE"} and not semantics.reasons:
        raise DashboardPartialDataSemanticsError("SEMANTICS_REASONS_MISSING")
    unsigned = asdict(semantics)
    unsigned.pop("semantics_hash")
    unsigned["schema_version"] = SEMANTICS_SCHEMA_VERSION
    unsigned["reasons"] = sorted(unsigned["reasons"])
    unsigned["panels"] = [asdict(item) for item in semantics.panels]
    if semantics.semantics_hash != _hash(unsigned):
        raise DashboardPartialDataSemanticsError("SEMANTICS_HASH_MISMATCH")


def _panel_semantics(
    load_state: DashboardPanelLoadState,
    evidence: DashboardPanelCompletenessEvidence,
    *,
    stale: bool,
) -> DashboardPanelDataSemantics:
    if load_state.state == "READY":
        if evidence.observed_items != load_state.result_count:
            raise DashboardPartialDataSemanticsError("RESULT_COUNT_MISMATCH")
        if evidence.total_components == 0:
            raise DashboardPartialDataSemanticsError("READY_COMPONENTS_EMPTY")
        if evidence.truncated or evidence.successful_components < evidence.total_components:
            state: PanelDataState = "PARTIAL"
        elif evidence.observed_items == 0:
            state = "EMPTY"
        else:
            state = "COMPLETE"
    else:
        if evidence.observed_items is not None or evidence.total_components != 0:
            raise DashboardPartialDataSemanticsError("NON_READY_EVIDENCE_INVALID")
        state = {
            "DEFERRED": "DEFERRED",
            "ERROR": "FAILED",
            "TIMED_OUT": "FAILED",
            "PENDING": "PENDING",
            "LOADING": "PENDING",
        }.get(load_state.state, "UNAVAILABLE")
    if stale:
        state = "UNAVAILABLE"
    return DashboardPanelDataSemantics(
        panel_id=evidence.panel_id,
        state=state,
        observed_items=evidence.observed_items
        if state in {"COMPLETE", "EMPTY", "PARTIAL"}
        else None,
        successful_components=evidence.successful_components,
        total_components=evidence.total_components,
    )


def _validated_evidence(value: Any) -> DashboardPanelCompletenessEvidence:
    if not isinstance(value, DashboardPanelCompletenessEvidence):
        raise DashboardPartialDataSemanticsError("EVIDENCE_TYPE_INVALID")
    unsigned = asdict(value)
    supplied_hash = unsigned.pop("evidence_hash")
    _validate_evidence_fields(unsigned)
    if supplied_hash != _hash(unsigned):
        raise DashboardPartialDataSemanticsError("EVIDENCE_HASH_MISMATCH")
    return value


def _validate_evidence_fields(payload: dict[str, Any]) -> None:
    for key in ("panel_id", "source_identity_hash", "source_watermark"):
        if not isinstance(payload[key], str) or not payload[key]:
            raise DashboardPartialDataSemanticsError("EVIDENCE_FIELD_INVALID")
    for key in ("successful_components", "total_components", "evidence_age_seconds"):
        value = payload[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise DashboardPartialDataSemanticsError("EVIDENCE_FIELD_INVALID")
    observed = payload["observed_items"]
    if observed is not None and (
        isinstance(observed, bool) or not isinstance(observed, int) or observed < 0
    ):
        raise DashboardPartialDataSemanticsError("EVIDENCE_FIELD_INVALID")
    if payload["successful_components"] > payload["total_components"]:
        raise DashboardPartialDataSemanticsError("EVIDENCE_COMPONENTS_INVALID")
    if not isinstance(payload["truncated"], bool):
        raise DashboardPartialDataSemanticsError("EVIDENCE_FIELD_INVALID")


def _hash(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
