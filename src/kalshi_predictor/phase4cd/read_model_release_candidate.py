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

RELEASE_SCHEMA_VERSION = "phase4fx-read-model-release-candidate-v1"
ReleaseDecision = Literal["ACCEPT", "REJECT"]


class ReadModelReleaseCandidateError(ValueError):
    """Stable fail-closed read-model release-candidate rejection."""


@dataclass(frozen=True)
class ReadModelReleaseCandidate:
    decision: ReleaseDecision
    reasons: tuple[str, ...]
    dashboard_hash: str
    replay_evidence_hash: str
    source_identity_hash: str
    source_watermark: str
    release_hash: str
    execution_authorized: bool = False


def build_release_candidate(
    *,
    dashboard: Any,
    replay: DifferentialReplayResult,
) -> ReadModelReleaseCandidate:
    try:
        validate_provenance_dashboard(dashboard)
        validate_replay_result(replay)
    except (TypeError, ValueError) as exc:
        raise ReadModelReleaseCandidateError("UPSTREAM_EVIDENCE_INVALID") from exc
    if dashboard["status"] == "UNAVAILABLE" or dashboard["provenance"] is None:
        raise ReadModelReleaseCandidateError("PROVENANCE_UNAVAILABLE")

    reasons: list[str] = []
    if dashboard["status"] != "HEALTHY":
        reasons.append(f"DASHBOARD_{dashboard['status']}")
    if replay.status != "MATCH":
        reasons.append("REPLAY_DIVERGENCE")
    decision: ReleaseDecision = "ACCEPT" if not reasons else "REJECT"
    unsigned = {
        "schema_version": RELEASE_SCHEMA_VERSION,
        "decision": decision,
        "reasons": sorted(reasons),
        "dashboard_hash": dashboard["dashboard_hash"],
        "replay_evidence_hash": replay.evidence_hash,
        "source_identity_hash": dashboard["provenance"]["source_identity_hash"],
        "source_watermark": dashboard["provenance"]["source_watermark"],
        "execution_authorized": False,
    }
    return ReadModelReleaseCandidate(
        decision=decision,
        reasons=tuple(unsigned["reasons"]),
        dashboard_hash=unsigned["dashboard_hash"],
        replay_evidence_hash=unsigned["replay_evidence_hash"],
        source_identity_hash=unsigned["source_identity_hash"],
        source_watermark=unsigned["source_watermark"],
        release_hash=_hash(unsigned),
    )


def validate_release_candidate(candidate: Any) -> None:
    if not isinstance(candidate, ReadModelReleaseCandidate):
        raise ReadModelReleaseCandidateError("RELEASE_TYPE_INVALID")
    if candidate.execution_authorized is not False:
        raise ReadModelReleaseCandidateError("RELEASE_SAFETY_BOUNDARY_INVALID")
    if candidate.decision == "ACCEPT" and candidate.reasons:
        raise ReadModelReleaseCandidateError("ACCEPT_REASONS_INVALID")
    if candidate.decision == "REJECT" and not candidate.reasons:
        raise ReadModelReleaseCandidateError("REJECT_REASONS_MISSING")
    unsigned = asdict(candidate)
    unsigned.pop("release_hash")
    unsigned["schema_version"] = RELEASE_SCHEMA_VERSION
    unsigned["reasons"] = sorted(unsigned["reasons"])
    if candidate.release_hash != _hash(unsigned):
        raise ReadModelReleaseCandidateError("RELEASE_HASH_MISMATCH")


def _hash(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
