from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any, Literal

from kalshi_predictor.phase4cd.evidence_cache_stampede_prevention import (
    EvidenceCacheStampedePolicy,
    validate_stampede_policy,
)
from kalshi_predictor.phase4cd.evidence_query_deadline_propagation import (
    EvidenceQueryDeadlinePropagation,
    validate_query_deadline_propagation,
)

PROPOSAL_SCHEMA_VERSION = "phase4gg-evidence-cache-single-flight-proposal-v1"
ProposalDecision = Literal["READY", "REJECT"]
ParticipantRole = Literal["CACHE_READER", "LEADER", "FOLLOWER"]


class EvidenceCacheSingleFlightError(ValueError):
    """Stable fail-closed single-flight proposal error."""


@dataclass(frozen=True)
class SingleFlightParticipant:
    requester_id: str
    role: ParticipantRole


@dataclass(frozen=True)
class EvidenceCacheSingleFlightProposal:
    decision: ProposalDecision
    reasons: tuple[str, ...]
    policy_hash: str
    propagation_hash: str
    cache_key_hash: str
    source_identity_hash: str
    source_watermark: str
    policy_age_seconds: int
    max_policy_age_seconds: int
    follower_wait_ms: int
    participants: tuple[SingleFlightParticipant, ...]
    leader_count: int
    proposal_hash: str
    execution_authorized: bool = False


def propose_evidence_cache_single_flight(
    *,
    policy: Any,
    propagation: Any,
    requester_ids: Sequence[Any],
    policy_age_seconds: int,
    follower_wait_ms: int,
    max_policy_age_seconds: int = 60,
    max_participants: int = 32,
) -> EvidenceCacheSingleFlightProposal:
    for value in (policy_age_seconds, follower_wait_ms, max_policy_age_seconds):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise EvidenceCacheSingleFlightError("PROPOSAL_FIELD_INVALID")
    if max_participants <= 0:
        raise EvidenceCacheSingleFlightError("PARTICIPANT_BOUND_INVALID")
    if not requester_ids:
        raise EvidenceCacheSingleFlightError("PARTICIPANTS_EMPTY")
    if len(requester_ids) > max_participants:
        raise EvidenceCacheSingleFlightError("PARTICIPANT_BOUND_EXCEEDED")
    if any(not isinstance(item, str) or not item.strip() for item in requester_ids):
        raise EvidenceCacheSingleFlightError("PARTICIPANT_INVALID")
    requesters = tuple(sorted(item.strip() for item in requester_ids))
    if len(set(requesters)) != len(requesters):
        raise EvidenceCacheSingleFlightError("PARTICIPANT_DUPLICATE")
    try:
        validate_stampede_policy(policy)
        validate_query_deadline_propagation(propagation)
    except (TypeError, ValueError) as exc:
        raise EvidenceCacheSingleFlightError("PROPOSAL_INPUT_INVALID") from exc
    if not isinstance(policy, EvidenceCacheStampedePolicy):
        raise EvidenceCacheSingleFlightError("PROPOSAL_INPUT_INVALID")
    if not isinstance(propagation, EvidenceQueryDeadlinePropagation):
        raise EvidenceCacheSingleFlightError("PROPOSAL_INPUT_INVALID")
    if policy.propagation_hash != propagation.propagation_hash:
        raise EvidenceCacheSingleFlightError("POLICY_PROPAGATION_LINK_MISMATCH")
    if policy.contender_count != len(requesters):
        raise EvidenceCacheSingleFlightError("PARTICIPANT_COUNT_MISMATCH")
    if policy.elected_requester is not None and policy.elected_requester not in requesters:
        raise EvidenceCacheSingleFlightError("ELECTED_REQUESTER_MISSING")

    reasons: list[str] = []
    if propagation.decision != "PROPAGATE":
        reasons.append("PROPAGATION_NOT_ACTIVE")
    if policy_age_seconds > max_policy_age_seconds:
        reasons.append("POLICY_STALE")
    if follower_wait_ms > propagation.remaining_ms:
        reasons.append("FOLLOWER_WAIT_EXCEEDS_DEADLINE")
    if policy.decision == "CACHE_HIT":
        participants = tuple(SingleFlightParticipant(item, "CACHE_READER") for item in requesters)
    elif policy.decision == "WAIT":
        participants = tuple(SingleFlightParticipant(item, "FOLLOWER") for item in requesters)
    else:
        participants = tuple(
            SingleFlightParticipant(
                item, "LEADER" if item == policy.elected_requester else "FOLLOWER"
            )
            for item in requesters
        )
    leader_count = sum(item.role == "LEADER" for item in participants)
    if policy.decision == "ELECT_ONE" and leader_count != 1:
        raise EvidenceCacheSingleFlightError("LEADER_CARDINALITY_INVALID")
    if policy.decision != "ELECT_ONE" and leader_count:
        raise EvidenceCacheSingleFlightError("UNEXPECTED_LEADER")
    decision: ProposalDecision = "READY" if not reasons else "REJECT"
    unsigned = {
        "schema_version": PROPOSAL_SCHEMA_VERSION,
        "decision": decision,
        "reasons": sorted(reasons),
        "policy_hash": policy.policy_hash,
        "propagation_hash": propagation.propagation_hash,
        "cache_key_hash": policy.cache_key_hash,
        "source_identity_hash": policy.source_identity_hash,
        "source_watermark": policy.source_watermark,
        "policy_age_seconds": policy_age_seconds,
        "max_policy_age_seconds": max_policy_age_seconds,
        "follower_wait_ms": follower_wait_ms,
        "participants": [asdict(item) for item in participants],
        "leader_count": leader_count,
        "execution_authorized": False,
    }
    return EvidenceCacheSingleFlightProposal(
        decision=decision,
        reasons=tuple(unsigned["reasons"]),
        policy_hash=policy.policy_hash,
        propagation_hash=propagation.propagation_hash,
        cache_key_hash=policy.cache_key_hash,
        source_identity_hash=policy.source_identity_hash,
        source_watermark=policy.source_watermark,
        policy_age_seconds=policy_age_seconds,
        max_policy_age_seconds=max_policy_age_seconds,
        follower_wait_ms=follower_wait_ms,
        participants=participants,
        leader_count=leader_count,
        proposal_hash=_hash(unsigned),
    )


def validate_single_flight_proposal(proposal: Any) -> None:
    if not isinstance(proposal, EvidenceCacheSingleFlightProposal):
        raise EvidenceCacheSingleFlightError("PROPOSAL_RESULT_TYPE_INVALID")
    if proposal.execution_authorized is not False:
        raise EvidenceCacheSingleFlightError("PROPOSAL_SAFETY_BOUNDARY_INVALID")
    if proposal.decision == "READY" and proposal.reasons:
        raise EvidenceCacheSingleFlightError("READY_REASONS_INVALID")
    if proposal.decision == "REJECT" and not proposal.reasons:
        raise EvidenceCacheSingleFlightError("REJECT_REASONS_MISSING")
    if proposal.leader_count != sum(item.role == "LEADER" for item in proposal.participants):
        raise EvidenceCacheSingleFlightError("LEADER_COUNT_MISMATCH")
    unsigned = asdict(proposal)
    unsigned.pop("proposal_hash")
    unsigned["schema_version"] = PROPOSAL_SCHEMA_VERSION
    unsigned["reasons"] = sorted(unsigned["reasons"])
    if proposal.proposal_hash != _hash(unsigned):
        raise EvidenceCacheSingleFlightError("PROPOSAL_HASH_MISMATCH")


def _hash(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
