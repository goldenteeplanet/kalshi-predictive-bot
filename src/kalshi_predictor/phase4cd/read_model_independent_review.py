from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any, Literal

from kalshi_predictor.phase4cd.read_model_differential_replay import (
    DifferentialReplayResult,
    validate_replay_result,
)
from kalshi_predictor.phase4cd.read_model_provenance_dashboard import (
    validate_provenance_dashboard,
)
from kalshi_predictor.phase4cd.read_model_release_candidate import (
    ReadModelReleaseCandidate,
    build_release_candidate,
    validate_release_candidate,
)

REVIEW_SCHEMA_VERSION = "phase4fy-read-model-independent-review-v1"
ReviewDecision = Literal["APPROVE", "REJECT"]


class IndependentReviewError(ValueError):
    """Stable fail-closed independent-review error."""


@dataclass(frozen=True)
class IndependentReviewResult:
    decision: ReviewDecision
    reasons: tuple[str, ...]
    release_hash: str
    dashboard_hash: str
    replay_evidence_hash: str
    source_identity_hash: str
    source_watermark: str
    snapshot_age_seconds: int
    progress_age_seconds: int
    max_snapshot_age_seconds: int
    max_progress_age_seconds: int
    review_hash: str
    execution_authorized: bool = False


def independently_review_candidate(
    *,
    candidate: Any,
    dashboard: Any,
    replay: Any,
    max_snapshot_age_seconds: int = 300,
    max_progress_age_seconds: int = 900,
) -> IndependentReviewResult:
    if max_snapshot_age_seconds < 0 or max_progress_age_seconds < 0:
        raise IndependentReviewError("FRESHNESS_BOUND_INVALID")
    try:
        validate_release_candidate(candidate)
        validate_provenance_dashboard(dashboard)
        validate_replay_result(replay)
    except (TypeError, ValueError) as exc:
        raise IndependentReviewError("REVIEW_INPUT_INVALID") from exc
    if not isinstance(candidate, ReadModelReleaseCandidate):
        raise IndependentReviewError("REVIEW_INPUT_INVALID")
    if not isinstance(replay, DifferentialReplayResult):
        raise IndependentReviewError("REVIEW_INPUT_INVALID")
    if dashboard["status"] == "UNAVAILABLE" or dashboard["freshness"] is None:
        raise IndependentReviewError("REVIEW_EVIDENCE_INCOMPLETE")

    expected = build_release_candidate(dashboard=dashboard, replay=replay)
    if candidate != expected:
        raise IndependentReviewError("RELEASE_RECOMPUTATION_MISMATCH")
    if candidate.dashboard_hash != dashboard["dashboard_hash"]:
        raise IndependentReviewError("DASHBOARD_LINK_MISMATCH")
    if candidate.replay_evidence_hash != replay.evidence_hash:
        raise IndependentReviewError("REPLAY_LINK_MISMATCH")

    snapshot_age = _bounded_age(
        dashboard["freshness"].get("snapshot_age_seconds"), "SNAPSHOT_AGE_INVALID"
    )
    progress_age = _bounded_age(
        dashboard["freshness"].get("progress_age_seconds"), "PROGRESS_AGE_INVALID"
    )
    reasons: list[str] = []
    if candidate.decision != "ACCEPT":
        reasons.append("RELEASE_NOT_ACCEPTED")
    if snapshot_age > max_snapshot_age_seconds:
        reasons.append("SNAPSHOT_STALE")
    if progress_age > max_progress_age_seconds:
        reasons.append("PROGRESS_STALE")
    decision: ReviewDecision = "APPROVE" if not reasons else "REJECT"
    unsigned = {
        "schema_version": REVIEW_SCHEMA_VERSION,
        "decision": decision,
        "reasons": sorted(reasons),
        "release_hash": candidate.release_hash,
        "dashboard_hash": dashboard["dashboard_hash"],
        "replay_evidence_hash": replay.evidence_hash,
        "source_identity_hash": candidate.source_identity_hash,
        "source_watermark": candidate.source_watermark,
        "snapshot_age_seconds": snapshot_age,
        "progress_age_seconds": progress_age,
        "max_snapshot_age_seconds": max_snapshot_age_seconds,
        "max_progress_age_seconds": max_progress_age_seconds,
        "execution_authorized": False,
    }
    return IndependentReviewResult(
        decision=decision,
        reasons=tuple(unsigned["reasons"]),
        release_hash=candidate.release_hash,
        dashboard_hash=dashboard["dashboard_hash"],
        replay_evidence_hash=replay.evidence_hash,
        source_identity_hash=candidate.source_identity_hash,
        source_watermark=candidate.source_watermark,
        snapshot_age_seconds=snapshot_age,
        progress_age_seconds=progress_age,
        max_snapshot_age_seconds=max_snapshot_age_seconds,
        max_progress_age_seconds=max_progress_age_seconds,
        review_hash=_hash(unsigned),
    )


def validate_independent_review(review: Any) -> None:
    if not isinstance(review, IndependentReviewResult):
        raise IndependentReviewError("REVIEW_RESULT_TYPE_INVALID")
    if review.execution_authorized is not False:
        raise IndependentReviewError("REVIEW_SAFETY_BOUNDARY_INVALID")
    if review.decision == "APPROVE" and review.reasons:
        raise IndependentReviewError("APPROVAL_REASONS_INVALID")
    if review.decision == "REJECT" and not review.reasons:
        raise IndependentReviewError("REJECTION_REASONS_MISSING")
    unsigned = asdict(review)
    unsigned.pop("review_hash")
    unsigned["schema_version"] = REVIEW_SCHEMA_VERSION
    unsigned["reasons"] = sorted(unsigned["reasons"])
    if review.review_hash != _hash(unsigned):
        raise IndependentReviewError("REVIEW_HASH_MISMATCH")


def _bounded_age(value: Any, reason: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise IndependentReviewError(reason)
    return value


def _hash(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
