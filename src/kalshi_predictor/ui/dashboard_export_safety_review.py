from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any, Literal

REVIEW_SCHEMA_VERSION = "phase4gy-dashboard-export-safety-review-v1"
ReviewStatus = Literal["PASS", "FAIL", "STALE"]


class DashboardExportSafetyReviewError(ValueError):
    """Stable fail-closed dashboard-export review error."""


@dataclass(frozen=True)
class ExportEvidence:
    export_id: str
    export_format: Literal["CSV", "JSON"]
    record_count: int
    encoded_size_bytes: int
    sensitive_fields_present: bool
    spreadsheet_formula_neutralized: bool
    lineage_included: bool
    complete: bool
    source_identity_hash: str
    source_watermark: str
    evidence_age_seconds: int
    evidence_hash: str


@dataclass(frozen=True)
class DashboardExportSafetyReview:
    status: ReviewStatus
    reasons: tuple[str, ...]
    source_identity_hash: str
    source_watermark: str
    export_count: int
    violation_count: int
    max_records_per_export: int
    max_bytes_per_export: int
    observed_max_age_seconds: int
    max_evidence_age_seconds: int
    exports_hash: str
    review_hash: str
    read_only: bool = True
    export_authorized: bool = False
    execution_authorized: bool = False


def make_export_evidence(
    *,
    export_id: str,
    export_format: Literal["CSV", "JSON"],
    record_count: int,
    encoded_size_bytes: int,
    sensitive_fields_present: bool,
    spreadsheet_formula_neutralized: bool,
    lineage_included: bool,
    complete: bool,
    source_identity_hash: str,
    source_watermark: str,
    evidence_age_seconds: int,
) -> ExportEvidence:
    unsigned = {
        "export_id": export_id,
        "export_format": export_format,
        "record_count": record_count,
        "encoded_size_bytes": encoded_size_bytes,
        "sensitive_fields_present": sensitive_fields_present,
        "spreadsheet_formula_neutralized": spreadsheet_formula_neutralized,
        "lineage_included": lineage_included,
        "complete": complete,
        "source_identity_hash": source_identity_hash,
        "source_watermark": source_watermark,
        "evidence_age_seconds": evidence_age_seconds,
    }
    _validate_evidence_fields(unsigned)
    return ExportEvidence(**unsigned, evidence_hash=_hash(unsigned))


def review_dashboard_export_safety(
    exports: Sequence[Any],
    *,
    max_exports: int = 16,
    max_records_per_export: int = 10_000,
    max_bytes_per_export: int = 5_000_000,
    max_evidence_age_seconds: int = 300,
) -> DashboardExportSafetyReview:
    for value in (max_exports, max_records_per_export, max_bytes_per_export):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise DashboardExportSafetyReviewError("REVIEW_BOUND_INVALID")
    if (
        isinstance(max_evidence_age_seconds, bool)
        or not isinstance(max_evidence_age_seconds, int)
        or max_evidence_age_seconds < 0
    ):
        raise DashboardExportSafetyReviewError("REVIEW_BOUND_INVALID")
    if not exports:
        raise DashboardExportSafetyReviewError("EXPORTS_EMPTY")
    if len(exports) > max_exports:
        raise DashboardExportSafetyReviewError("EXPORT_BOUND_EXCEEDED")

    validated = [_validated_evidence(item) for item in exports]
    ids = [item.export_id for item in validated]
    if len(set(ids)) != len(ids):
        raise DashboardExportSafetyReviewError("EXPORT_ID_DUPLICATE")
    identities = {item.source_identity_hash for item in validated}
    watermarks = {item.source_watermark for item in validated}
    if len(identities) != 1 or len(watermarks) != 1:
        raise DashboardExportSafetyReviewError("EXPORT_LINEAGE_MIXED")
    ordered = sorted(validated, key=lambda item: item.export_id)
    observed_age = max(item.evidence_age_seconds for item in ordered)
    violations: list[str] = []
    for item in ordered:
        if not item.complete:
            violations.append(f"EXPORT_EVIDENCE_INCOMPLETE:{item.export_id}")
        if item.record_count > max_records_per_export:
            violations.append(f"EXPORT_RECORD_BOUND_EXCEEDED:{item.export_id}")
        if item.encoded_size_bytes > max_bytes_per_export:
            violations.append(f"EXPORT_BYTE_BOUND_EXCEEDED:{item.export_id}")
        if item.sensitive_fields_present:
            violations.append(f"SENSITIVE_FIELDS_PRESENT:{item.export_id}")
        if item.export_format == "CSV" and not item.spreadsheet_formula_neutralized:
            violations.append(f"CSV_FORMULA_UNSAFE:{item.export_id}")
        if not item.lineage_included:
            violations.append(f"EXPORT_LINEAGE_MISSING:{item.export_id}")

    if observed_age > max_evidence_age_seconds:
        status: ReviewStatus = "STALE"
        reasons = ["EXPORT_EVIDENCE_STALE"]
    elif violations:
        status = "FAIL"
        reasons = sorted(violations)
    else:
        status = "PASS"
        reasons = []

    exports_hash = _hash([asdict(item) for item in ordered])
    unsigned = {
        "schema_version": REVIEW_SCHEMA_VERSION,
        "status": status,
        "reasons": reasons,
        "source_identity_hash": ordered[0].source_identity_hash,
        "source_watermark": ordered[0].source_watermark,
        "export_count": len(ordered),
        "violation_count": len(reasons) if status == "FAIL" else 0,
        "max_records_per_export": max_records_per_export,
        "max_bytes_per_export": max_bytes_per_export,
        "observed_max_age_seconds": observed_age,
        "max_evidence_age_seconds": max_evidence_age_seconds,
        "exports_hash": exports_hash,
        "read_only": True,
        "export_authorized": False,
        "execution_authorized": False,
    }
    return DashboardExportSafetyReview(
        status=status,
        reasons=tuple(reasons),
        source_identity_hash=ordered[0].source_identity_hash,
        source_watermark=ordered[0].source_watermark,
        export_count=len(ordered),
        violation_count=unsigned["violation_count"],
        max_records_per_export=max_records_per_export,
        max_bytes_per_export=max_bytes_per_export,
        observed_max_age_seconds=observed_age,
        max_evidence_age_seconds=max_evidence_age_seconds,
        exports_hash=exports_hash,
        review_hash=_hash(unsigned),
    )


def validate_dashboard_export_safety_review(review: Any) -> None:
    if not isinstance(review, DashboardExportSafetyReview):
        raise DashboardExportSafetyReviewError("REVIEW_RESULT_TYPE_INVALID")
    if review.read_only is not True or review.export_authorized is not False:
        raise DashboardExportSafetyReviewError("REVIEW_SAFETY_BOUNDARY_INVALID")
    if review.execution_authorized is not False:
        raise DashboardExportSafetyReviewError("REVIEW_SAFETY_BOUNDARY_INVALID")
    if review.status == "PASS" and (review.reasons or review.violation_count):
        raise DashboardExportSafetyReviewError("PASS_STATE_INVALID")
    if review.status == "FAIL" and (not review.reasons or not review.violation_count):
        raise DashboardExportSafetyReviewError("FAIL_STATE_INVALID")
    if review.status == "STALE" and not review.reasons:
        raise DashboardExportSafetyReviewError("STALE_REASONS_MISSING")
    unsigned = asdict(review)
    unsigned.pop("review_hash")
    unsigned["schema_version"] = REVIEW_SCHEMA_VERSION
    unsigned["reasons"] = list(unsigned["reasons"])
    if review.review_hash != _hash(unsigned):
        raise DashboardExportSafetyReviewError("REVIEW_HASH_MISMATCH")


def _validated_evidence(value: Any) -> ExportEvidence:
    if not isinstance(value, ExportEvidence):
        raise DashboardExportSafetyReviewError("EXPORT_TYPE_INVALID")
    unsigned = asdict(value)
    supplied_hash = unsigned.pop("evidence_hash")
    _validate_evidence_fields(unsigned)
    if supplied_hash != _hash(unsigned):
        raise DashboardExportSafetyReviewError("EXPORT_HASH_MISMATCH")
    return value


def _validate_evidence_fields(payload: dict[str, Any]) -> None:
    for key in ("export_id", "source_identity_hash", "source_watermark"):
        if not isinstance(payload[key], str) or not payload[key]:
            raise DashboardExportSafetyReviewError("EXPORT_FIELD_INVALID")
    if payload["export_format"] not in {"CSV", "JSON"}:
        raise DashboardExportSafetyReviewError("EXPORT_FORMAT_INVALID")
    for key in (
        "sensitive_fields_present",
        "spreadsheet_formula_neutralized",
        "lineage_included",
        "complete",
    ):
        if not isinstance(payload[key], bool):
            raise DashboardExportSafetyReviewError("EXPORT_FIELD_INVALID")
    for key in ("record_count", "encoded_size_bytes", "evidence_age_seconds"):
        value = payload[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise DashboardExportSafetyReviewError("EXPORT_FIELD_INVALID")


def _hash(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
