from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any, Literal

AUDIT_SCHEMA_VERSION = "phase4gv-dashboard-accessibility-audit-v1"
AuditStatus = Literal["PASS", "FAIL", "STALE"]


class DashboardAccessibilityAuditError(ValueError):
    """Stable fail-closed dashboard-accessibility audit error."""


@dataclass(frozen=True)
class DashboardAccessibilityComponent:
    component_id: str
    role: str
    text_label: str
    interactive: bool
    keyboard_focusable: bool
    color_only_status: bool
    aria_live: Literal["OFF", "POLITE", "ASSERTIVE"]
    contrast_milli: int
    complete: bool
    source_identity_hash: str
    source_watermark: str
    evidence_age_seconds: int
    component_hash: str


@dataclass(frozen=True)
class DashboardAccessibilityAudit:
    status: AuditStatus
    reasons: tuple[str, ...]
    source_identity_hash: str
    source_watermark: str
    component_count: int
    violation_count: int
    minimum_contrast_milli: int
    observed_max_age_seconds: int
    max_evidence_age_seconds: int
    components_hash: str
    audit_hash: str
    read_only: bool = True
    execution_authorized: bool = False


def make_dashboard_accessibility_component(
    *,
    component_id: str,
    role: str,
    text_label: str,
    interactive: bool,
    keyboard_focusable: bool,
    color_only_status: bool,
    aria_live: Literal["OFF", "POLITE", "ASSERTIVE"],
    contrast_milli: int,
    complete: bool,
    source_identity_hash: str,
    source_watermark: str,
    evidence_age_seconds: int,
) -> DashboardAccessibilityComponent:
    unsigned = {
        "component_id": component_id,
        "role": role,
        "text_label": text_label,
        "interactive": interactive,
        "keyboard_focusable": keyboard_focusable,
        "color_only_status": color_only_status,
        "aria_live": aria_live,
        "contrast_milli": contrast_milli,
        "complete": complete,
        "source_identity_hash": source_identity_hash,
        "source_watermark": source_watermark,
        "evidence_age_seconds": evidence_age_seconds,
    }
    _validate_component_fields(unsigned)
    return DashboardAccessibilityComponent(**unsigned, component_hash=_hash(unsigned))


def audit_dashboard_accessibility(
    components: Sequence[Any],
    *,
    max_components: int = 128,
    minimum_contrast_milli: int = 4_500,
    max_evidence_age_seconds: int = 300,
) -> DashboardAccessibilityAudit:
    for value in (max_components, minimum_contrast_milli):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise DashboardAccessibilityAuditError("AUDIT_BOUND_INVALID")
    if (
        isinstance(max_evidence_age_seconds, bool)
        or not isinstance(max_evidence_age_seconds, int)
        or max_evidence_age_seconds < 0
    ):
        raise DashboardAccessibilityAuditError("AUDIT_BOUND_INVALID")
    if not components:
        raise DashboardAccessibilityAuditError("COMPONENTS_EMPTY")
    if len(components) > max_components:
        raise DashboardAccessibilityAuditError("COMPONENT_BOUND_EXCEEDED")

    validated = [_validated_component(item) for item in components]
    ids = [item.component_id for item in validated]
    if len(set(ids)) != len(ids):
        raise DashboardAccessibilityAuditError("COMPONENT_ID_DUPLICATE")
    identities = {item.source_identity_hash for item in validated}
    watermarks = {item.source_watermark for item in validated}
    if len(identities) != 1 or len(watermarks) != 1:
        raise DashboardAccessibilityAuditError("COMPONENT_LINEAGE_MIXED")
    ordered = sorted(validated, key=lambda item: item.component_id)
    observed_age = max(item.evidence_age_seconds for item in ordered)
    violations: list[str] = []
    for item in ordered:
        if not item.complete:
            violations.append(f"COMPONENT_INCOMPLETE:{item.component_id}")
        if not item.text_label.strip():
            violations.append(f"TEXT_LABEL_MISSING:{item.component_id}")
        if item.color_only_status:
            violations.append(f"COLOR_ONLY_STATUS:{item.component_id}")
        if item.interactive and not item.keyboard_focusable:
            violations.append(f"KEYBOARD_FOCUS_MISSING:{item.component_id}")
        if item.contrast_milli < minimum_contrast_milli:
            violations.append(f"CONTRAST_BELOW_MINIMUM:{item.component_id}")
        if item.role == "status" and item.aria_live == "OFF":
            violations.append(f"STATUS_LIVE_REGION_MISSING:{item.component_id}")

    if observed_age > max_evidence_age_seconds:
        status: AuditStatus = "STALE"
        reasons = ["ACCESSIBILITY_EVIDENCE_STALE"]
    elif violations:
        status = "FAIL"
        reasons = sorted(violations)
    else:
        status = "PASS"
        reasons = []

    components_hash = _hash([asdict(item) for item in ordered])
    identity = ordered[0].source_identity_hash
    watermark = ordered[0].source_watermark
    unsigned = {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "status": status,
        "reasons": reasons,
        "source_identity_hash": identity,
        "source_watermark": watermark,
        "component_count": len(ordered),
        "violation_count": len(reasons) if status == "FAIL" else 0,
        "minimum_contrast_milli": minimum_contrast_milli,
        "observed_max_age_seconds": observed_age,
        "max_evidence_age_seconds": max_evidence_age_seconds,
        "components_hash": components_hash,
        "read_only": True,
        "execution_authorized": False,
    }
    return DashboardAccessibilityAudit(
        status=status,
        reasons=tuple(reasons),
        source_identity_hash=identity,
        source_watermark=watermark,
        component_count=len(ordered),
        violation_count=unsigned["violation_count"],
        minimum_contrast_milli=minimum_contrast_milli,
        observed_max_age_seconds=observed_age,
        max_evidence_age_seconds=max_evidence_age_seconds,
        components_hash=components_hash,
        audit_hash=_hash(unsigned),
    )


def validate_dashboard_accessibility_audit(audit: Any) -> None:
    if not isinstance(audit, DashboardAccessibilityAudit):
        raise DashboardAccessibilityAuditError("AUDIT_RESULT_TYPE_INVALID")
    if audit.read_only is not True or audit.execution_authorized is not False:
        raise DashboardAccessibilityAuditError("AUDIT_SAFETY_BOUNDARY_INVALID")
    if audit.status == "PASS" and (audit.reasons or audit.violation_count):
        raise DashboardAccessibilityAuditError("PASS_STATE_INVALID")
    if audit.status == "FAIL" and (not audit.reasons or not audit.violation_count):
        raise DashboardAccessibilityAuditError("FAIL_STATE_INVALID")
    if audit.status == "STALE" and not audit.reasons:
        raise DashboardAccessibilityAuditError("STALE_REASONS_MISSING")
    unsigned = asdict(audit)
    unsigned.pop("audit_hash")
    unsigned["schema_version"] = AUDIT_SCHEMA_VERSION
    unsigned["reasons"] = list(unsigned["reasons"])
    if audit.audit_hash != _hash(unsigned):
        raise DashboardAccessibilityAuditError("AUDIT_HASH_MISMATCH")


def _validated_component(value: Any) -> DashboardAccessibilityComponent:
    if not isinstance(value, DashboardAccessibilityComponent):
        raise DashboardAccessibilityAuditError("COMPONENT_TYPE_INVALID")
    unsigned = asdict(value)
    supplied_hash = unsigned.pop("component_hash")
    _validate_component_fields(unsigned)
    if supplied_hash != _hash(unsigned):
        raise DashboardAccessibilityAuditError("COMPONENT_HASH_MISMATCH")
    return value


def _validate_component_fields(payload: dict[str, Any]) -> None:
    for key in ("component_id", "role", "source_identity_hash", "source_watermark"):
        if not isinstance(payload[key], str) or not payload[key]:
            raise DashboardAccessibilityAuditError("COMPONENT_FIELD_INVALID")
    if not isinstance(payload["text_label"], str):
        raise DashboardAccessibilityAuditError("COMPONENT_FIELD_INVALID")
    for key in ("interactive", "keyboard_focusable", "color_only_status", "complete"):
        if not isinstance(payload[key], bool):
            raise DashboardAccessibilityAuditError("COMPONENT_FIELD_INVALID")
    if payload["aria_live"] not in {"OFF", "POLITE", "ASSERTIVE"}:
        raise DashboardAccessibilityAuditError("ARIA_LIVE_INVALID")
    for key in ("contrast_milli", "evidence_age_seconds"):
        value = payload[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise DashboardAccessibilityAuditError("COMPONENT_FIELD_INVALID")


def _hash(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
