from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any, Literal

from kalshi_predictor.ui.dashboard_panel_registry import (
    DashboardPanelRegistry,
    validate_dashboard_panel_registry,
)

DISCLOSURE_SCHEMA_VERSION = "phase4go-dashboard-progressive-disclosure-v1"
DisclosureStatus = Literal["READY", "BLOCKED", "STALE"]
DisclosureMode = Literal["SUMMARY", "DEFERRED", "DETAIL", "UNAVAILABLE"]


class DashboardProgressiveDisclosureError(ValueError):
    """Stable fail-closed progressive-disclosure error."""


@dataclass(frozen=True)
class DashboardPanelDisclosureInput:
    panel_id: str
    cost_class: Literal["CHEAP", "EXPENSIVE"]
    available: bool
    detail_requested: bool
    source_identity_hash: str
    source_watermark: str
    evidence_age_seconds: int
    input_hash: str


@dataclass(frozen=True)
class DashboardPanelDisclosure:
    panel_id: str
    mode: DisclosureMode
    detail_query_allowed: bool


@dataclass(frozen=True)
class DashboardProgressiveDisclosure:
    status: DisclosureStatus
    reasons: tuple[str, ...]
    registry_hash: str
    source_identity_hash: str
    source_watermark: str
    panel_count: int
    detail_panel_count: int
    max_detail_panels: int
    observed_max_age_seconds: int
    max_evidence_age_seconds: int
    panels: tuple[DashboardPanelDisclosure, ...]
    inputs_hash: str
    disclosure_hash: str
    read_only: bool = True
    execution_authorized: bool = False


def make_dashboard_panel_disclosure_input(
    *,
    panel_id: str,
    cost_class: Literal["CHEAP", "EXPENSIVE"],
    available: bool,
    detail_requested: bool,
    source_identity_hash: str,
    source_watermark: str,
    evidence_age_seconds: int,
) -> DashboardPanelDisclosureInput:
    unsigned = {
        "panel_id": panel_id,
        "cost_class": cost_class,
        "available": available,
        "detail_requested": detail_requested,
        "source_identity_hash": source_identity_hash,
        "source_watermark": source_watermark,
        "evidence_age_seconds": evidence_age_seconds,
    }
    _validate_input_fields(unsigned)
    return DashboardPanelDisclosureInput(**unsigned, input_hash=_hash(unsigned))


def build_dashboard_progressive_disclosure(
    *,
    registry: Any,
    panel_inputs: Sequence[Any],
    max_inputs: int = 32,
    max_detail_panels: int = 4,
    max_evidence_age_seconds: int = 300,
) -> DashboardProgressiveDisclosure:
    for value in (max_inputs, max_detail_panels):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise DashboardProgressiveDisclosureError("DISCLOSURE_BOUND_INVALID")
    if (
        isinstance(max_evidence_age_seconds, bool)
        or not isinstance(max_evidence_age_seconds, int)
        or max_evidence_age_seconds < 0
    ):
        raise DashboardProgressiveDisclosureError("DISCLOSURE_BOUND_INVALID")
    try:
        validate_dashboard_panel_registry(registry)
    except (TypeError, ValueError) as exc:
        raise DashboardProgressiveDisclosureError("REGISTRY_INPUT_INVALID") from exc
    if not isinstance(registry, DashboardPanelRegistry):
        raise DashboardProgressiveDisclosureError("REGISTRY_INPUT_INVALID")
    if not panel_inputs:
        raise DashboardProgressiveDisclosureError("PANEL_INPUTS_EMPTY")
    if len(panel_inputs) > max_inputs:
        raise DashboardProgressiveDisclosureError("PANEL_INPUT_BOUND_EXCEEDED")

    validated = [_validated_input(item) for item in panel_inputs]
    ids = [item.panel_id for item in validated]
    if len(set(ids)) != len(ids):
        raise DashboardProgressiveDisclosureError("PANEL_INPUT_DUPLICATE")
    if set(ids) != set(registry.panel_ids):
        raise DashboardProgressiveDisclosureError("PANEL_INPUT_SET_INVALID")
    identities = {item.source_identity_hash for item in validated}
    watermarks = {item.source_watermark for item in validated}
    if identities != {registry.source_identity_hash} or watermarks != {registry.source_watermark}:
        raise DashboardProgressiveDisclosureError("PANEL_LINEAGE_MISMATCH")

    ordered = sorted(validated, key=lambda item: registry.panel_ids.index(item.panel_id))
    observed_age = max(
        registry.observed_max_age_seconds,
        *(item.evidence_age_seconds for item in ordered),
    )
    requested_detail = sum(
        item.available and item.cost_class == "EXPENSIVE" and item.detail_requested
        for item in ordered
    )
    if requested_detail > max_detail_panels:
        raise DashboardProgressiveDisclosureError("DETAIL_PANEL_BOUND_EXCEEDED")

    reasons: list[str] = []
    if registry.status == "STALE" or observed_age > max_evidence_age_seconds:
        status: DisclosureStatus = "STALE"
        reasons.append("DISCLOSURE_EVIDENCE_STALE")
    elif registry.status == "BLOCKED":
        status = "BLOCKED"
        reasons.append("PANEL_REGISTRY_BLOCKED")
    else:
        status = "READY"

    panels: list[DashboardPanelDisclosure] = []
    for item in ordered:
        if status != "READY" or not item.available:
            mode: DisclosureMode = "UNAVAILABLE"
        elif item.cost_class == "CHEAP":
            mode = "SUMMARY"
        elif item.detail_requested:
            mode = "DETAIL"
        else:
            mode = "DEFERRED"
        panels.append(
            DashboardPanelDisclosure(
                panel_id=item.panel_id,
                mode=mode,
                detail_query_allowed=mode == "DETAIL",
            )
        )

    inputs_hash = _hash([asdict(item) for item in ordered])
    unsigned = {
        "schema_version": DISCLOSURE_SCHEMA_VERSION,
        "status": status,
        "reasons": sorted(reasons),
        "registry_hash": registry.registry_hash,
        "source_identity_hash": registry.source_identity_hash,
        "source_watermark": registry.source_watermark,
        "panel_count": len(panels),
        "detail_panel_count": sum(item.mode == "DETAIL" for item in panels),
        "max_detail_panels": max_detail_panels,
        "observed_max_age_seconds": observed_age,
        "max_evidence_age_seconds": max_evidence_age_seconds,
        "panels": [asdict(item) for item in panels],
        "inputs_hash": inputs_hash,
        "read_only": True,
        "execution_authorized": False,
    }
    return DashboardProgressiveDisclosure(
        status=status,
        reasons=tuple(unsigned["reasons"]),
        registry_hash=registry.registry_hash,
        source_identity_hash=registry.source_identity_hash,
        source_watermark=registry.source_watermark,
        panel_count=len(panels),
        detail_panel_count=unsigned["detail_panel_count"],
        max_detail_panels=max_detail_panels,
        observed_max_age_seconds=observed_age,
        max_evidence_age_seconds=max_evidence_age_seconds,
        panels=tuple(panels),
        inputs_hash=inputs_hash,
        disclosure_hash=_hash(unsigned),
    )


def validate_dashboard_progressive_disclosure(disclosure: Any) -> None:
    if not isinstance(disclosure, DashboardProgressiveDisclosure):
        raise DashboardProgressiveDisclosureError("DISCLOSURE_RESULT_TYPE_INVALID")
    if disclosure.read_only is not True or disclosure.execution_authorized is not False:
        raise DashboardProgressiveDisclosureError("DISCLOSURE_SAFETY_BOUNDARY_INVALID")
    if disclosure.status == "READY" and disclosure.reasons:
        raise DashboardProgressiveDisclosureError("READY_STATE_INVALID")
    if disclosure.status in {"BLOCKED", "STALE"} and not disclosure.reasons:
        raise DashboardProgressiveDisclosureError("NON_READY_REASONS_MISSING")
    if disclosure.status != "READY" and any(
        item.detail_query_allowed for item in disclosure.panels
    ):
        raise DashboardProgressiveDisclosureError("NON_READY_DETAIL_INVALID")
    unsigned = asdict(disclosure)
    unsigned.pop("disclosure_hash")
    unsigned["schema_version"] = DISCLOSURE_SCHEMA_VERSION
    unsigned["reasons"] = sorted(unsigned["reasons"])
    unsigned["panels"] = [asdict(item) for item in disclosure.panels]
    if disclosure.disclosure_hash != _hash(unsigned):
        raise DashboardProgressiveDisclosureError("DISCLOSURE_HASH_MISMATCH")


def _validated_input(value: Any) -> DashboardPanelDisclosureInput:
    if not isinstance(value, DashboardPanelDisclosureInput):
        raise DashboardProgressiveDisclosureError("PANEL_INPUT_TYPE_INVALID")
    unsigned = asdict(value)
    supplied_hash = unsigned.pop("input_hash")
    _validate_input_fields(unsigned)
    if supplied_hash != _hash(unsigned):
        raise DashboardProgressiveDisclosureError("PANEL_INPUT_HASH_MISMATCH")
    return value


def _validate_input_fields(payload: dict[str, Any]) -> None:
    for key in ("panel_id", "source_identity_hash", "source_watermark"):
        if not isinstance(payload[key], str) or not payload[key]:
            raise DashboardProgressiveDisclosureError("PANEL_INPUT_FIELD_INVALID")
    if payload["cost_class"] not in {"CHEAP", "EXPENSIVE"}:
        raise DashboardProgressiveDisclosureError("PANEL_COST_CLASS_INVALID")
    if not isinstance(payload["available"], bool) or not isinstance(
        payload["detail_requested"], bool
    ):
        raise DashboardProgressiveDisclosureError("PANEL_INPUT_FIELD_INVALID")
    age = payload["evidence_age_seconds"]
    if isinstance(age, bool) or not isinstance(age, int) or age < 0:
        raise DashboardProgressiveDisclosureError("PANEL_INPUT_FIELD_INVALID")


def _hash(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
