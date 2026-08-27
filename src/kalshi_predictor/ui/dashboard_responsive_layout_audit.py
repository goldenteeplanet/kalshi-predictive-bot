from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any, Literal

AUDIT_SCHEMA_VERSION = "phase4gw-dashboard-responsive-layout-audit-v1"
AuditStatus = Literal["PASS", "FAIL", "STALE"]


class DashboardResponsiveLayoutAuditError(ValueError):
    """Stable fail-closed responsive-layout audit error."""


@dataclass(frozen=True)
class ResponsiveLayoutSample:
    sample_id: str
    viewport_width_px: int
    viewport_height_px: int
    horizontal_overflow_px: int
    minimum_touch_target_px: int
    required_content_visible: bool
    navigation_reachable: bool
    complete: bool
    source_identity_hash: str
    source_watermark: str
    evidence_age_seconds: int
    sample_hash: str


@dataclass(frozen=True)
class DashboardResponsiveLayoutAudit:
    status: AuditStatus
    reasons: tuple[str, ...]
    source_identity_hash: str
    source_watermark: str
    sample_count: int
    violation_count: int
    minimum_viewport_width_px: int
    maximum_viewport_width_px: int
    required_touch_target_px: int
    observed_max_age_seconds: int
    max_evidence_age_seconds: int
    samples_hash: str
    audit_hash: str
    read_only: bool = True
    execution_authorized: bool = False


def make_responsive_layout_sample(
    *,
    sample_id: str,
    viewport_width_px: int,
    viewport_height_px: int,
    horizontal_overflow_px: int,
    minimum_touch_target_px: int,
    required_content_visible: bool,
    navigation_reachable: bool,
    complete: bool,
    source_identity_hash: str,
    source_watermark: str,
    evidence_age_seconds: int,
) -> ResponsiveLayoutSample:
    unsigned = {
        "sample_id": sample_id,
        "viewport_width_px": viewport_width_px,
        "viewport_height_px": viewport_height_px,
        "horizontal_overflow_px": horizontal_overflow_px,
        "minimum_touch_target_px": minimum_touch_target_px,
        "required_content_visible": required_content_visible,
        "navigation_reachable": navigation_reachable,
        "complete": complete,
        "source_identity_hash": source_identity_hash,
        "source_watermark": source_watermark,
        "evidence_age_seconds": evidence_age_seconds,
    }
    _validate_sample_fields(unsigned)
    return ResponsiveLayoutSample(**unsigned, sample_hash=_hash(unsigned))


def audit_dashboard_responsive_layout(
    samples: Sequence[Any],
    *,
    max_samples: int = 32,
    required_touch_target_px: int = 44,
    max_evidence_age_seconds: int = 300,
) -> DashboardResponsiveLayoutAudit:
    for value in (max_samples, required_touch_target_px):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise DashboardResponsiveLayoutAuditError("AUDIT_BOUND_INVALID")
    if (
        isinstance(max_evidence_age_seconds, bool)
        or not isinstance(max_evidence_age_seconds, int)
        or max_evidence_age_seconds < 0
    ):
        raise DashboardResponsiveLayoutAuditError("AUDIT_BOUND_INVALID")
    if not samples:
        raise DashboardResponsiveLayoutAuditError("SAMPLES_EMPTY")
    if len(samples) > max_samples:
        raise DashboardResponsiveLayoutAuditError("SAMPLE_BOUND_EXCEEDED")

    validated = [_validated_sample(item) for item in samples]
    ids = [item.sample_id for item in validated]
    if len(set(ids)) != len(ids):
        raise DashboardResponsiveLayoutAuditError("SAMPLE_ID_DUPLICATE")
    identities = {item.source_identity_hash for item in validated}
    watermarks = {item.source_watermark for item in validated}
    if len(identities) != 1 or len(watermarks) != 1:
        raise DashboardResponsiveLayoutAuditError("SAMPLE_LINEAGE_MIXED")
    ordered = sorted(validated, key=lambda item: (item.viewport_width_px, item.sample_id))
    observed_age = max(item.evidence_age_seconds for item in ordered)
    violations: list[str] = []
    for item in ordered:
        if not item.complete:
            violations.append(f"SAMPLE_INCOMPLETE:{item.sample_id}")
        if item.horizontal_overflow_px > 0:
            violations.append(f"HORIZONTAL_OVERFLOW:{item.sample_id}")
        if item.minimum_touch_target_px < required_touch_target_px:
            violations.append(f"TOUCH_TARGET_TOO_SMALL:{item.sample_id}")
        if not item.required_content_visible:
            violations.append(f"REQUIRED_CONTENT_HIDDEN:{item.sample_id}")
        if not item.navigation_reachable:
            violations.append(f"NAVIGATION_UNREACHABLE:{item.sample_id}")

    if observed_age > max_evidence_age_seconds:
        status: AuditStatus = "STALE"
        reasons = ["RESPONSIVE_LAYOUT_EVIDENCE_STALE"]
    elif violations:
        status = "FAIL"
        reasons = sorted(violations)
    else:
        status = "PASS"
        reasons = []

    samples_hash = _hash([asdict(item) for item in ordered])
    unsigned = {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "status": status,
        "reasons": reasons,
        "source_identity_hash": ordered[0].source_identity_hash,
        "source_watermark": ordered[0].source_watermark,
        "sample_count": len(ordered),
        "violation_count": len(reasons) if status == "FAIL" else 0,
        "minimum_viewport_width_px": ordered[0].viewport_width_px,
        "maximum_viewport_width_px": ordered[-1].viewport_width_px,
        "required_touch_target_px": required_touch_target_px,
        "observed_max_age_seconds": observed_age,
        "max_evidence_age_seconds": max_evidence_age_seconds,
        "samples_hash": samples_hash,
        "read_only": True,
        "execution_authorized": False,
    }
    return DashboardResponsiveLayoutAudit(
        status=status,
        reasons=tuple(reasons),
        source_identity_hash=ordered[0].source_identity_hash,
        source_watermark=ordered[0].source_watermark,
        sample_count=len(ordered),
        violation_count=unsigned["violation_count"],
        minimum_viewport_width_px=ordered[0].viewport_width_px,
        maximum_viewport_width_px=ordered[-1].viewport_width_px,
        required_touch_target_px=required_touch_target_px,
        observed_max_age_seconds=observed_age,
        max_evidence_age_seconds=max_evidence_age_seconds,
        samples_hash=samples_hash,
        audit_hash=_hash(unsigned),
    )


def validate_dashboard_responsive_layout_audit(audit: Any) -> None:
    if not isinstance(audit, DashboardResponsiveLayoutAudit):
        raise DashboardResponsiveLayoutAuditError("AUDIT_RESULT_TYPE_INVALID")
    if audit.read_only is not True or audit.execution_authorized is not False:
        raise DashboardResponsiveLayoutAuditError("AUDIT_SAFETY_BOUNDARY_INVALID")
    if audit.status == "PASS" and (audit.reasons or audit.violation_count):
        raise DashboardResponsiveLayoutAuditError("PASS_STATE_INVALID")
    if audit.status == "FAIL" and (not audit.reasons or not audit.violation_count):
        raise DashboardResponsiveLayoutAuditError("FAIL_STATE_INVALID")
    if audit.status == "STALE" and not audit.reasons:
        raise DashboardResponsiveLayoutAuditError("STALE_REASONS_MISSING")
    unsigned = asdict(audit)
    unsigned.pop("audit_hash")
    unsigned["schema_version"] = AUDIT_SCHEMA_VERSION
    unsigned["reasons"] = list(unsigned["reasons"])
    if audit.audit_hash != _hash(unsigned):
        raise DashboardResponsiveLayoutAuditError("AUDIT_HASH_MISMATCH")


def _validated_sample(value: Any) -> ResponsiveLayoutSample:
    if not isinstance(value, ResponsiveLayoutSample):
        raise DashboardResponsiveLayoutAuditError("SAMPLE_TYPE_INVALID")
    unsigned = asdict(value)
    supplied_hash = unsigned.pop("sample_hash")
    _validate_sample_fields(unsigned)
    if supplied_hash != _hash(unsigned):
        raise DashboardResponsiveLayoutAuditError("SAMPLE_HASH_MISMATCH")
    return value


def _validate_sample_fields(payload: dict[str, Any]) -> None:
    for key in ("sample_id", "source_identity_hash", "source_watermark"):
        if not isinstance(payload[key], str) or not payload[key]:
            raise DashboardResponsiveLayoutAuditError("SAMPLE_FIELD_INVALID")
    for key in (
        "required_content_visible",
        "navigation_reachable",
        "complete",
    ):
        if not isinstance(payload[key], bool):
            raise DashboardResponsiveLayoutAuditError("SAMPLE_FIELD_INVALID")
    for key in ("viewport_width_px", "viewport_height_px", "minimum_touch_target_px"):
        value = payload[key]
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise DashboardResponsiveLayoutAuditError("SAMPLE_FIELD_INVALID")
    for key in ("horizontal_overflow_px", "evidence_age_seconds"):
        value = payload[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise DashboardResponsiveLayoutAuditError("SAMPLE_FIELD_INVALID")


def _hash(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
