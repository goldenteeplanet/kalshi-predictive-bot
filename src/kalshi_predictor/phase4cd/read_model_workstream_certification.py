from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any, Literal

from kalshi_predictor.phase4cd.read_model_independent_review import (
    IndependentReviewResult,
    validate_independent_review,
)
from kalshi_predictor.phase4cd.read_model_release_candidate import (
    ReadModelReleaseCandidate,
    validate_release_candidate,
)

CERTIFICATION_SCHEMA_VERSION = "phase4fz-read-model-workstream-certification-v1"
CertificationStatus = Literal["CERTIFIED", "NOT_CERTIFIED"]


class WorkstreamCertificationError(ValueError):
    """Stable fail-closed read-model certification error."""


@dataclass(frozen=True)
class WorkstreamCertification:
    status: CertificationStatus
    reasons: tuple[str, ...]
    release_hash: str
    review_hash: str
    source_identity_hash: str
    source_watermark: str
    evidence_age_seconds: int
    max_evidence_age_seconds: int
    certification_hash: str
    execution_authorized: bool = False


def certify_read_model_workstream(
    *,
    candidate: Any,
    review: Any,
    max_evidence_age_seconds: int = 300,
) -> WorkstreamCertification:
    if max_evidence_age_seconds < 0:
        raise WorkstreamCertificationError("EVIDENCE_AGE_BOUND_INVALID")
    try:
        validate_release_candidate(candidate)
        validate_independent_review(review)
    except (TypeError, ValueError) as exc:
        raise WorkstreamCertificationError("CERTIFICATION_INPUT_INVALID") from exc
    if not isinstance(candidate, ReadModelReleaseCandidate):
        raise WorkstreamCertificationError("CERTIFICATION_INPUT_INVALID")
    if not isinstance(review, IndependentReviewResult):
        raise WorkstreamCertificationError("CERTIFICATION_INPUT_INVALID")
    if review.release_hash != candidate.release_hash:
        raise WorkstreamCertificationError("RELEASE_REVIEW_LINK_MISMATCH")
    if review.source_identity_hash != candidate.source_identity_hash:
        raise WorkstreamCertificationError("SOURCE_IDENTITY_LINK_MISMATCH")
    if review.source_watermark != candidate.source_watermark:
        raise WorkstreamCertificationError("SOURCE_WATERMARK_LINK_MISMATCH")

    evidence_age = max(review.snapshot_age_seconds, review.progress_age_seconds)
    reasons: list[str] = []
    if candidate.decision != "ACCEPT":
        reasons.append("RELEASE_NOT_ACCEPTED")
    if review.decision != "APPROVE":
        reasons.append("INDEPENDENT_REVIEW_NOT_APPROVED")
    if evidence_age > max_evidence_age_seconds:
        reasons.append("CERTIFICATION_EVIDENCE_STALE")
    status: CertificationStatus = "CERTIFIED" if not reasons else "NOT_CERTIFIED"
    unsigned = {
        "schema_version": CERTIFICATION_SCHEMA_VERSION,
        "status": status,
        "reasons": sorted(reasons),
        "release_hash": candidate.release_hash,
        "review_hash": review.review_hash,
        "source_identity_hash": candidate.source_identity_hash,
        "source_watermark": candidate.source_watermark,
        "evidence_age_seconds": evidence_age,
        "max_evidence_age_seconds": max_evidence_age_seconds,
        "execution_authorized": False,
    }
    return WorkstreamCertification(
        status=status,
        reasons=tuple(unsigned["reasons"]),
        release_hash=candidate.release_hash,
        review_hash=review.review_hash,
        source_identity_hash=candidate.source_identity_hash,
        source_watermark=candidate.source_watermark,
        evidence_age_seconds=evidence_age,
        max_evidence_age_seconds=max_evidence_age_seconds,
        certification_hash=_hash(unsigned),
    )


def validate_workstream_certification(certification: Any) -> None:
    if not isinstance(certification, WorkstreamCertification):
        raise WorkstreamCertificationError("CERTIFICATION_RESULT_TYPE_INVALID")
    if certification.execution_authorized is not False:
        raise WorkstreamCertificationError("CERTIFICATION_SAFETY_BOUNDARY_INVALID")
    if certification.status == "CERTIFIED" and certification.reasons:
        raise WorkstreamCertificationError("CERTIFIED_REASONS_INVALID")
    if certification.status == "NOT_CERTIFIED" and not certification.reasons:
        raise WorkstreamCertificationError("NOT_CERTIFIED_REASONS_MISSING")
    unsigned = asdict(certification)
    unsigned.pop("certification_hash")
    unsigned["schema_version"] = CERTIFICATION_SCHEMA_VERSION
    unsigned["reasons"] = sorted(unsigned["reasons"])
    if certification.certification_hash != _hash(unsigned):
        raise WorkstreamCertificationError("CERTIFICATION_HASH_MISMATCH")


def _hash(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
