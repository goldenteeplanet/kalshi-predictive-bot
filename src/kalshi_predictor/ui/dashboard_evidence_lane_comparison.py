from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any, Literal

COMPARISON_SCHEMA_VERSION = "phase4gu-dashboard-evidence-lane-comparison-v1"
ComparisonStatus = Literal["CONSISTENT", "DIVERGENT", "INCOMPLETE", "STALE"]


class DashboardEvidenceLaneComparisonError(ValueError):
    """Stable fail-closed evidence-lane comparison error."""


@dataclass(frozen=True)
class DashboardEvidenceLane:
    lane_id: str
    subject_id: str
    verdict: str
    value_hash: str
    source_identity_hash: str
    source_watermark: str
    lineage_hash: str
    evidence_age_seconds: int
    complete: bool
    lane_hash: str


@dataclass(frozen=True)
class DashboardEvidenceLaneComparison:
    status: ComparisonStatus
    reasons: tuple[str, ...]
    subject_id: str
    expected_lane_ids: tuple[str, ...]
    observed_lane_ids: tuple[str, ...]
    lane_count: int
    agreement_groups: tuple[tuple[str, ...], ...]
    observed_max_age_seconds: int
    max_evidence_age_seconds: int
    lanes_hash: str
    comparison_hash: str
    read_only: bool = True
    execution_authorized: bool = False


def make_dashboard_evidence_lane(
    *,
    lane_id: str,
    subject_id: str,
    verdict: str,
    value_hash: str,
    source_identity_hash: str,
    source_watermark: str,
    lineage_hash: str,
    evidence_age_seconds: int,
    complete: bool,
) -> DashboardEvidenceLane:
    unsigned = {
        "lane_id": lane_id,
        "subject_id": subject_id,
        "verdict": verdict,
        "value_hash": value_hash,
        "source_identity_hash": source_identity_hash,
        "source_watermark": source_watermark,
        "lineage_hash": lineage_hash,
        "evidence_age_seconds": evidence_age_seconds,
        "complete": complete,
    }
    _validate_lane_fields(unsigned)
    return DashboardEvidenceLane(**unsigned, lane_hash=_hash(unsigned))


def compare_dashboard_evidence_lanes(
    lanes: Sequence[Any],
    *,
    expected_lane_ids: Sequence[str],
    max_lanes: int = 8,
    max_evidence_age_seconds: int = 300,
) -> DashboardEvidenceLaneComparison:
    if (
        isinstance(max_lanes, bool)
        or not isinstance(max_lanes, int)
        or max_lanes < 2
        or isinstance(max_evidence_age_seconds, bool)
        or not isinstance(max_evidence_age_seconds, int)
        or max_evidence_age_seconds < 0
    ):
        raise DashboardEvidenceLaneComparisonError("COMPARISON_BOUND_INVALID")
    if not lanes:
        raise DashboardEvidenceLaneComparisonError("LANES_EMPTY")
    if len(lanes) > max_lanes:
        raise DashboardEvidenceLaneComparisonError("LANE_BOUND_EXCEEDED")
    if not expected_lane_ids:
        raise DashboardEvidenceLaneComparisonError("EXPECTED_LANES_EMPTY")
    if len(expected_lane_ids) > max_lanes or len(set(expected_lane_ids)) != len(expected_lane_ids):
        raise DashboardEvidenceLaneComparisonError("EXPECTED_LANES_INVALID")
    if any(not isinstance(item, str) or not item for item in expected_lane_ids):
        raise DashboardEvidenceLaneComparisonError("EXPECTED_LANES_INVALID")

    validated = [_validated_lane(item) for item in lanes]
    observed_ids = [item.lane_id for item in validated]
    if len(set(observed_ids)) != len(observed_ids):
        raise DashboardEvidenceLaneComparisonError("LANE_ID_DUPLICATE")
    if not set(observed_ids).issubset(set(expected_lane_ids)):
        raise DashboardEvidenceLaneComparisonError("UNEXPECTED_LANE")
    subjects = {item.subject_id for item in validated}
    watermarks = {item.source_watermark for item in validated}
    if len(subjects) != 1 or len(watermarks) != 1:
        raise DashboardEvidenceLaneComparisonError("LANE_SUBJECT_MIXED")

    ordered = sorted(validated, key=lambda item: item.lane_id)
    expected = tuple(sorted(expected_lane_ids))
    observed = tuple(item.lane_id for item in ordered)
    missing = sorted(set(expected) - set(observed))
    observed_age = max(item.evidence_age_seconds for item in ordered)
    groups: dict[tuple[str, str], list[str]] = {}
    for item in ordered:
        groups.setdefault((item.verdict, item.value_hash), []).append(item.lane_id)
    agreement_groups = tuple(
        sorted((tuple(sorted(items)) for items in groups.values()), key=lambda item: item)
    )

    reasons: list[str] = []
    if observed_age > max_evidence_age_seconds:
        status: ComparisonStatus = "STALE"
        reasons.append("LANE_EVIDENCE_STALE")
    else:
        reasons.extend(f"LANE_MISSING:{lane_id}" for lane_id in missing)
        reasons.extend(f"LANE_INCOMPLETE:{item.lane_id}" for item in ordered if not item.complete)
        if reasons:
            status = "INCOMPLETE"
        elif len(groups) > 1:
            status = "DIVERGENT"
            reasons.append("LANE_VERDICT_DIVERGENCE")
        else:
            status = "CONSISTENT"

    lanes_hash = _hash([asdict(item) for item in ordered])
    unsigned = {
        "schema_version": COMPARISON_SCHEMA_VERSION,
        "status": status,
        "reasons": sorted(reasons),
        "subject_id": ordered[0].subject_id,
        "expected_lane_ids": list(expected),
        "observed_lane_ids": list(observed),
        "lane_count": len(ordered),
        "agreement_groups": [list(item) for item in agreement_groups],
        "observed_max_age_seconds": observed_age,
        "max_evidence_age_seconds": max_evidence_age_seconds,
        "lanes_hash": lanes_hash,
        "read_only": True,
        "execution_authorized": False,
    }
    return DashboardEvidenceLaneComparison(
        status=status,
        reasons=tuple(unsigned["reasons"]),
        subject_id=ordered[0].subject_id,
        expected_lane_ids=expected,
        observed_lane_ids=observed,
        lane_count=len(ordered),
        agreement_groups=agreement_groups,
        observed_max_age_seconds=observed_age,
        max_evidence_age_seconds=max_evidence_age_seconds,
        lanes_hash=lanes_hash,
        comparison_hash=_hash(unsigned),
    )


def validate_dashboard_evidence_lane_comparison(comparison: Any) -> None:
    if not isinstance(comparison, DashboardEvidenceLaneComparison):
        raise DashboardEvidenceLaneComparisonError("COMPARISON_RESULT_TYPE_INVALID")
    if comparison.read_only is not True or comparison.execution_authorized is not False:
        raise DashboardEvidenceLaneComparisonError("COMPARISON_SAFETY_BOUNDARY_INVALID")
    if comparison.status == "CONSISTENT" and comparison.reasons:
        raise DashboardEvidenceLaneComparisonError("CONSISTENT_STATE_INVALID")
    if comparison.status in {"DIVERGENT", "INCOMPLETE", "STALE"} and not comparison.reasons:
        raise DashboardEvidenceLaneComparisonError("NON_CONSISTENT_REASONS_MISSING")
    unsigned = asdict(comparison)
    unsigned.pop("comparison_hash")
    unsigned["schema_version"] = COMPARISON_SCHEMA_VERSION
    unsigned["reasons"] = sorted(unsigned["reasons"])
    unsigned["expected_lane_ids"] = list(unsigned["expected_lane_ids"])
    unsigned["observed_lane_ids"] = list(unsigned["observed_lane_ids"])
    unsigned["agreement_groups"] = [list(item) for item in comparison.agreement_groups]
    if comparison.comparison_hash != _hash(unsigned):
        raise DashboardEvidenceLaneComparisonError("COMPARISON_HASH_MISMATCH")


def _validated_lane(value: Any) -> DashboardEvidenceLane:
    if not isinstance(value, DashboardEvidenceLane):
        raise DashboardEvidenceLaneComparisonError("LANE_TYPE_INVALID")
    unsigned = asdict(value)
    supplied_hash = unsigned.pop("lane_hash")
    _validate_lane_fields(unsigned)
    if supplied_hash != _hash(unsigned):
        raise DashboardEvidenceLaneComparisonError("LANE_HASH_MISMATCH")
    return value


def _validate_lane_fields(payload: dict[str, Any]) -> None:
    for key in (
        "lane_id",
        "subject_id",
        "verdict",
        "value_hash",
        "source_identity_hash",
        "source_watermark",
        "lineage_hash",
    ):
        if not isinstance(payload[key], str) or not payload[key]:
            raise DashboardEvidenceLaneComparisonError("LANE_FIELD_INVALID")
    age = payload["evidence_age_seconds"]
    if isinstance(age, bool) or not isinstance(age, int) or age < 0:
        raise DashboardEvidenceLaneComparisonError("LANE_FIELD_INVALID")
    if not isinstance(payload["complete"], bool):
        raise DashboardEvidenceLaneComparisonError("LANE_FIELD_INVALID")


def _hash(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
