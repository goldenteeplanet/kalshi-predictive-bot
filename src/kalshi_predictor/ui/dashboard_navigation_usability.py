from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any, Literal

AUDIT_SCHEMA_VERSION = "phase4gx-dashboard-navigation-usability-v1"
AuditStatus = Literal["PASS", "FAIL", "STALE"]


class DashboardNavigationUsabilityError(ValueError):
    """Stable fail-closed dashboard-navigation usability error."""


@dataclass(frozen=True)
class NavigationRouteEvidence:
    route_id: str
    visible_label: str
    interaction_count: int
    keyboard_reachable: bool
    current_location_exposed: bool
    destination_available: bool
    complete: bool
    source_identity_hash: str
    source_watermark: str
    evidence_age_seconds: int
    evidence_hash: str


@dataclass(frozen=True)
class DashboardNavigationUsabilityAudit:
    status: AuditStatus
    reasons: tuple[str, ...]
    source_identity_hash: str
    source_watermark: str
    route_count: int
    violation_count: int
    maximum_interaction_count: int
    observed_max_age_seconds: int
    max_evidence_age_seconds: int
    routes_hash: str
    audit_hash: str
    read_only: bool = True
    execution_authorized: bool = False


def make_navigation_route_evidence(
    *,
    route_id: str,
    visible_label: str,
    interaction_count: int,
    keyboard_reachable: bool,
    current_location_exposed: bool,
    destination_available: bool,
    complete: bool,
    source_identity_hash: str,
    source_watermark: str,
    evidence_age_seconds: int,
) -> NavigationRouteEvidence:
    unsigned = {
        "route_id": route_id,
        "visible_label": visible_label,
        "interaction_count": interaction_count,
        "keyboard_reachable": keyboard_reachable,
        "current_location_exposed": current_location_exposed,
        "destination_available": destination_available,
        "complete": complete,
        "source_identity_hash": source_identity_hash,
        "source_watermark": source_watermark,
        "evidence_age_seconds": evidence_age_seconds,
    }
    _validate_route_fields(unsigned)
    return NavigationRouteEvidence(**unsigned, evidence_hash=_hash(unsigned))


def audit_dashboard_navigation_usability(
    routes: Sequence[Any],
    *,
    max_routes: int = 64,
    maximum_interaction_count: int = 3,
    max_evidence_age_seconds: int = 300,
) -> DashboardNavigationUsabilityAudit:
    for value in (max_routes, maximum_interaction_count):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise DashboardNavigationUsabilityError("AUDIT_BOUND_INVALID")
    if (
        isinstance(max_evidence_age_seconds, bool)
        or not isinstance(max_evidence_age_seconds, int)
        or max_evidence_age_seconds < 0
    ):
        raise DashboardNavigationUsabilityError("AUDIT_BOUND_INVALID")
    if not routes:
        raise DashboardNavigationUsabilityError("ROUTES_EMPTY")
    if len(routes) > max_routes:
        raise DashboardNavigationUsabilityError("ROUTE_BOUND_EXCEEDED")

    validated = [_validated_route(item) for item in routes]
    ids = [item.route_id for item in validated]
    if len(set(ids)) != len(ids):
        raise DashboardNavigationUsabilityError("ROUTE_ID_DUPLICATE")
    identities = {item.source_identity_hash for item in validated}
    watermarks = {item.source_watermark for item in validated}
    if len(identities) != 1 or len(watermarks) != 1:
        raise DashboardNavigationUsabilityError("ROUTE_LINEAGE_MIXED")
    ordered = sorted(validated, key=lambda item: item.route_id)
    observed_age = max(item.evidence_age_seconds for item in ordered)
    violations: list[str] = []
    for item in ordered:
        if not item.complete:
            violations.append(f"ROUTE_INCOMPLETE:{item.route_id}")
        if not item.visible_label.strip():
            violations.append(f"VISIBLE_LABEL_MISSING:{item.route_id}")
        if item.interaction_count > maximum_interaction_count:
            violations.append(f"INTERACTION_BUDGET_EXCEEDED:{item.route_id}")
        if not item.keyboard_reachable:
            violations.append(f"KEYBOARD_ROUTE_UNREACHABLE:{item.route_id}")
        if not item.current_location_exposed:
            violations.append(f"CURRENT_LOCATION_HIDDEN:{item.route_id}")
        if not item.destination_available:
            violations.append(f"DESTINATION_UNAVAILABLE:{item.route_id}")

    if observed_age > max_evidence_age_seconds:
        status: AuditStatus = "STALE"
        reasons = ["NAVIGATION_EVIDENCE_STALE"]
    elif violations:
        status = "FAIL"
        reasons = sorted(violations)
    else:
        status = "PASS"
        reasons = []

    routes_hash = _hash([asdict(item) for item in ordered])
    unsigned = {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "status": status,
        "reasons": reasons,
        "source_identity_hash": ordered[0].source_identity_hash,
        "source_watermark": ordered[0].source_watermark,
        "route_count": len(ordered),
        "violation_count": len(reasons) if status == "FAIL" else 0,
        "maximum_interaction_count": maximum_interaction_count,
        "observed_max_age_seconds": observed_age,
        "max_evidence_age_seconds": max_evidence_age_seconds,
        "routes_hash": routes_hash,
        "read_only": True,
        "execution_authorized": False,
    }
    return DashboardNavigationUsabilityAudit(
        status=status,
        reasons=tuple(reasons),
        source_identity_hash=ordered[0].source_identity_hash,
        source_watermark=ordered[0].source_watermark,
        route_count=len(ordered),
        violation_count=unsigned["violation_count"],
        maximum_interaction_count=maximum_interaction_count,
        observed_max_age_seconds=observed_age,
        max_evidence_age_seconds=max_evidence_age_seconds,
        routes_hash=routes_hash,
        audit_hash=_hash(unsigned),
    )


def validate_dashboard_navigation_usability_audit(audit: Any) -> None:
    if not isinstance(audit, DashboardNavigationUsabilityAudit):
        raise DashboardNavigationUsabilityError("AUDIT_RESULT_TYPE_INVALID")
    if audit.read_only is not True or audit.execution_authorized is not False:
        raise DashboardNavigationUsabilityError("AUDIT_SAFETY_BOUNDARY_INVALID")
    if audit.status == "PASS" and (audit.reasons or audit.violation_count):
        raise DashboardNavigationUsabilityError("PASS_STATE_INVALID")
    if audit.status == "FAIL" and (not audit.reasons or not audit.violation_count):
        raise DashboardNavigationUsabilityError("FAIL_STATE_INVALID")
    if audit.status == "STALE" and not audit.reasons:
        raise DashboardNavigationUsabilityError("STALE_REASONS_MISSING")
    unsigned = asdict(audit)
    unsigned.pop("audit_hash")
    unsigned["schema_version"] = AUDIT_SCHEMA_VERSION
    unsigned["reasons"] = list(unsigned["reasons"])
    if audit.audit_hash != _hash(unsigned):
        raise DashboardNavigationUsabilityError("AUDIT_HASH_MISMATCH")


def _validated_route(value: Any) -> NavigationRouteEvidence:
    if not isinstance(value, NavigationRouteEvidence):
        raise DashboardNavigationUsabilityError("ROUTE_TYPE_INVALID")
    unsigned = asdict(value)
    supplied_hash = unsigned.pop("evidence_hash")
    _validate_route_fields(unsigned)
    if supplied_hash != _hash(unsigned):
        raise DashboardNavigationUsabilityError("ROUTE_HASH_MISMATCH")
    return value


def _validate_route_fields(payload: dict[str, Any]) -> None:
    for key in ("route_id", "source_identity_hash", "source_watermark"):
        if not isinstance(payload[key], str) or not payload[key]:
            raise DashboardNavigationUsabilityError("ROUTE_FIELD_INVALID")
    if not isinstance(payload["visible_label"], str):
        raise DashboardNavigationUsabilityError("ROUTE_FIELD_INVALID")
    for key in (
        "keyboard_reachable",
        "current_location_exposed",
        "destination_available",
        "complete",
    ):
        if not isinstance(payload[key], bool):
            raise DashboardNavigationUsabilityError("ROUTE_FIELD_INVALID")
    for key in ("interaction_count", "evidence_age_seconds"):
        value = payload[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise DashboardNavigationUsabilityError("ROUTE_FIELD_INVALID")


def _hash(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
