from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any, Literal

REGISTRY_SCHEMA_VERSION = "phase4gn-dashboard-panel-registry-v1"
RegistryStatus = Literal["READY", "BLOCKED", "STALE"]


class DashboardPanelRegistryError(ValueError):
    """Stable fail-closed dashboard-panel registry error."""


@dataclass(frozen=True)
class DashboardPanelDescriptor:
    panel_id: str
    title: str
    route: str
    artifact_schema_version: str
    source_identity_hash: str
    source_watermark: str
    evidence_age_seconds: int
    priority: int
    required: bool
    enabled: bool
    descriptor_hash: str


@dataclass(frozen=True)
class DashboardPanelRegistry:
    status: RegistryStatus
    reasons: tuple[str, ...]
    source_identity_hash: str
    source_watermark: str
    panel_count: int
    enabled_panel_count: int
    required_panel_count: int
    observed_max_age_seconds: int
    max_evidence_age_seconds: int
    panel_ids: tuple[str, ...]
    descriptors_hash: str
    registry_hash: str
    read_only: bool = True
    execution_authorized: bool = False


def make_dashboard_panel_descriptor(
    *,
    panel_id: str,
    title: str,
    route: str,
    artifact_schema_version: str,
    source_identity_hash: str,
    source_watermark: str,
    evidence_age_seconds: int,
    priority: int,
    required: bool,
    enabled: bool,
) -> DashboardPanelDescriptor:
    unsigned = {
        "panel_id": panel_id,
        "title": title,
        "route": route,
        "artifact_schema_version": artifact_schema_version,
        "source_identity_hash": source_identity_hash,
        "source_watermark": source_watermark,
        "evidence_age_seconds": evidence_age_seconds,
        "priority": priority,
        "required": required,
        "enabled": enabled,
    }
    _validate_descriptor_fields(unsigned)
    return DashboardPanelDescriptor(**unsigned, descriptor_hash=_hash(unsigned))


def build_dashboard_panel_registry(
    panels: Sequence[Any],
    *,
    max_panels: int = 32,
    max_evidence_age_seconds: int = 300,
) -> DashboardPanelRegistry:
    if (
        isinstance(max_panels, bool)
        or not isinstance(max_panels, int)
        or max_panels <= 0
        or isinstance(max_evidence_age_seconds, bool)
        or not isinstance(max_evidence_age_seconds, int)
        or max_evidence_age_seconds < 0
    ):
        raise DashboardPanelRegistryError("REGISTRY_BOUND_INVALID")
    if not panels:
        raise DashboardPanelRegistryError("PANELS_EMPTY")
    if len(panels) > max_panels:
        raise DashboardPanelRegistryError("PANEL_BOUND_EXCEEDED")

    validated = [_validated_descriptor(item) for item in panels]
    ids = [item.panel_id for item in validated]
    routes = [item.route for item in validated]
    priorities = [item.priority for item in validated]
    if len(set(ids)) != len(ids):
        raise DashboardPanelRegistryError("PANEL_ID_DUPLICATE")
    if len(set(routes)) != len(routes):
        raise DashboardPanelRegistryError("PANEL_ROUTE_DUPLICATE")
    if len(set(priorities)) != len(priorities):
        raise DashboardPanelRegistryError("PANEL_PRIORITY_DUPLICATE")
    identities = {item.source_identity_hash for item in validated}
    watermarks = {item.source_watermark for item in validated}
    if len(identities) != 1 or len(watermarks) != 1:
        raise DashboardPanelRegistryError("PANEL_LINEAGE_MIXED")

    ordered = sorted(validated, key=lambda item: (item.priority, item.panel_id))
    observed_age = max(item.evidence_age_seconds for item in ordered)
    reasons: list[str] = []
    if observed_age > max_evidence_age_seconds:
        status: RegistryStatus = "STALE"
        reasons.append("PANEL_REGISTRY_EVIDENCE_STALE")
    else:
        disabled_required = [
            item.panel_id for item in ordered if item.required and not item.enabled
        ]
        reasons.extend(f"REQUIRED_PANEL_DISABLED:{panel_id}" for panel_id in disabled_required)
        status = "BLOCKED" if reasons else "READY"

    descriptors_hash = _hash([asdict(item) for item in ordered])
    identity = ordered[0].source_identity_hash
    watermark = ordered[0].source_watermark
    unsigned = {
        "schema_version": REGISTRY_SCHEMA_VERSION,
        "status": status,
        "reasons": sorted(reasons),
        "source_identity_hash": identity,
        "source_watermark": watermark,
        "panel_count": len(ordered),
        "enabled_panel_count": sum(item.enabled for item in ordered),
        "required_panel_count": sum(item.required for item in ordered),
        "observed_max_age_seconds": observed_age,
        "max_evidence_age_seconds": max_evidence_age_seconds,
        "panel_ids": [item.panel_id for item in ordered],
        "descriptors_hash": descriptors_hash,
        "read_only": True,
        "execution_authorized": False,
    }
    return DashboardPanelRegistry(
        status=status,
        reasons=tuple(unsigned["reasons"]),
        source_identity_hash=identity,
        source_watermark=watermark,
        panel_count=len(ordered),
        enabled_panel_count=unsigned["enabled_panel_count"],
        required_panel_count=unsigned["required_panel_count"],
        observed_max_age_seconds=observed_age,
        max_evidence_age_seconds=max_evidence_age_seconds,
        panel_ids=tuple(unsigned["panel_ids"]),
        descriptors_hash=descriptors_hash,
        registry_hash=_hash(unsigned),
    )


def validate_dashboard_panel_registry(registry: Any) -> None:
    if not isinstance(registry, DashboardPanelRegistry):
        raise DashboardPanelRegistryError("REGISTRY_RESULT_TYPE_INVALID")
    if registry.read_only is not True or registry.execution_authorized is not False:
        raise DashboardPanelRegistryError("REGISTRY_SAFETY_BOUNDARY_INVALID")
    if registry.status == "READY" and registry.reasons:
        raise DashboardPanelRegistryError("READY_STATE_INVALID")
    if registry.status in {"BLOCKED", "STALE"} and not registry.reasons:
        raise DashboardPanelRegistryError("NON_READY_REASONS_MISSING")
    unsigned = asdict(registry)
    unsigned.pop("registry_hash")
    unsigned["schema_version"] = REGISTRY_SCHEMA_VERSION
    unsigned["reasons"] = sorted(unsigned["reasons"])
    unsigned["panel_ids"] = list(unsigned["panel_ids"])
    if registry.registry_hash != _hash(unsigned):
        raise DashboardPanelRegistryError("REGISTRY_HASH_MISMATCH")


def _validated_descriptor(value: Any) -> DashboardPanelDescriptor:
    if not isinstance(value, DashboardPanelDescriptor):
        raise DashboardPanelRegistryError("PANEL_TYPE_INVALID")
    unsigned = asdict(value)
    supplied_hash = unsigned.pop("descriptor_hash")
    _validate_descriptor_fields(unsigned)
    if supplied_hash != _hash(unsigned):
        raise DashboardPanelRegistryError("PANEL_HASH_MISMATCH")
    return value


def _validate_descriptor_fields(payload: dict[str, Any]) -> None:
    for key in (
        "panel_id",
        "title",
        "route",
        "artifact_schema_version",
        "source_identity_hash",
        "source_watermark",
    ):
        if not isinstance(payload[key], str) or not payload[key]:
            raise DashboardPanelRegistryError("PANEL_FIELD_INVALID")
    if not payload["route"].startswith("/") or "://" in payload["route"]:
        raise DashboardPanelRegistryError("PANEL_ROUTE_INVALID")
    for key in ("evidence_age_seconds", "priority"):
        value = payload[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise DashboardPanelRegistryError("PANEL_FIELD_INVALID")
    if not isinstance(payload["required"], bool) or not isinstance(payload["enabled"], bool):
        raise DashboardPanelRegistryError("PANEL_FIELD_INVALID")


def _hash(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
