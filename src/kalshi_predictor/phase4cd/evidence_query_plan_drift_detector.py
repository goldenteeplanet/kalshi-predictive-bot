from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any, Literal

from kalshi_predictor.phase4cd.evidence_query_regression_corpus import (
    EvidenceQueryRegressionCorpus,
    validate_regression_corpus,
)

DETECTOR_SCHEMA_VERSION = "phase4gl-evidence-query-plan-drift-detector-v1"
DriftStatus = Literal["STABLE", "DRIFT", "STALE"]


class EvidenceQueryPlanDriftError(ValueError):
    """Stable fail-closed query-plan drift error."""


@dataclass(frozen=True)
class QueryPlanObservation:
    query_fingerprint: str
    baseline_plan_hash: str
    current_plan_hash: str
    corpus_hash: str
    source_identity_hash: str
    source_watermark: str
    evidence_age_seconds: int
    observation_hash: str


@dataclass(frozen=True)
class EvidenceQueryPlanDrift:
    status: DriftStatus
    reasons: tuple[str, ...]
    corpus_hash: str
    source_identity_hash: str
    source_watermark: str
    observation_count: int
    drift_count: int
    drifted_query_fingerprints: tuple[str, ...]
    observed_max_age_seconds: int
    max_evidence_age_seconds: int
    observations_hash: str
    detector_hash: str
    execution_authorized: bool = False


def make_query_plan_observation(
    *,
    query_fingerprint: str,
    baseline_plan_hash: str,
    current_plan_hash: str,
    corpus_hash: str,
    source_identity_hash: str,
    source_watermark: str,
    evidence_age_seconds: int,
) -> QueryPlanObservation:
    unsigned = {
        "query_fingerprint": query_fingerprint,
        "baseline_plan_hash": baseline_plan_hash,
        "current_plan_hash": current_plan_hash,
        "corpus_hash": corpus_hash,
        "source_identity_hash": source_identity_hash,
        "source_watermark": source_watermark,
        "evidence_age_seconds": evidence_age_seconds,
    }
    _validate_observation_fields(unsigned)
    return QueryPlanObservation(**unsigned, observation_hash=_hash(unsigned))


def detect_evidence_query_plan_drift(
    *,
    corpus: Any,
    observations: Sequence[Any],
    max_observations: int = 256,
    max_evidence_age_seconds: int = 300,
) -> EvidenceQueryPlanDrift:
    if max_observations <= 0 or max_evidence_age_seconds < 0:
        raise EvidenceQueryPlanDriftError("DETECTOR_BOUND_INVALID")
    if not observations:
        raise EvidenceQueryPlanDriftError("OBSERVATIONS_EMPTY")
    if len(observations) > max_observations:
        raise EvidenceQueryPlanDriftError("OBSERVATION_BOUND_EXCEEDED")
    try:
        validate_regression_corpus(corpus)
    except (TypeError, ValueError) as exc:
        raise EvidenceQueryPlanDriftError("CORPUS_INPUT_INVALID") from exc
    if not isinstance(corpus, EvidenceQueryRegressionCorpus):
        raise EvidenceQueryPlanDriftError("CORPUS_INPUT_INVALID")
    if corpus.status != "READY":
        raise EvidenceQueryPlanDriftError("CORPUS_NOT_READY")
    validated = [_validated_observation(item) for item in observations]
    queries = [item.query_fingerprint for item in validated]
    if len(set(queries)) != len(queries):
        raise EvidenceQueryPlanDriftError("QUERY_OBSERVATION_DUPLICATE")
    if any(item.corpus_hash != corpus.corpus_hash for item in validated):
        raise EvidenceQueryPlanDriftError("CORPUS_LINK_MISMATCH")
    if any(item.source_identity_hash != corpus.source_identity_hash for item in validated):
        raise EvidenceQueryPlanDriftError("SOURCE_IDENTITY_MISMATCH")
    if any(item.source_watermark != corpus.source_watermark for item in validated):
        raise EvidenceQueryPlanDriftError("SOURCE_WATERMARK_MISMATCH")

    ordered = sorted(validated, key=lambda item: item.query_fingerprint)
    observed_age = max(item.evidence_age_seconds for item in ordered)
    drifted = tuple(
        item.query_fingerprint
        for item in ordered
        if item.current_plan_hash != item.baseline_plan_hash
    )
    reasons: list[str] = []
    if observed_age > max_evidence_age_seconds:
        status: DriftStatus = "STALE"
        reasons.append("PLAN_EVIDENCE_STALE")
        reported_drift: tuple[str, ...] = ()
    elif drifted:
        status = "DRIFT"
        reasons.append("QUERY_PLAN_HASH_CHANGED")
        reported_drift = drifted
    else:
        status = "STABLE"
        reported_drift = ()
    observations_hash = _hash([asdict(item) for item in ordered])
    unsigned = {
        "schema_version": DETECTOR_SCHEMA_VERSION,
        "status": status,
        "reasons": sorted(reasons),
        "corpus_hash": corpus.corpus_hash,
        "source_identity_hash": corpus.source_identity_hash,
        "source_watermark": corpus.source_watermark,
        "observation_count": len(ordered),
        "drift_count": len(reported_drift),
        "drifted_query_fingerprints": list(reported_drift),
        "observed_max_age_seconds": observed_age,
        "max_evidence_age_seconds": max_evidence_age_seconds,
        "observations_hash": observations_hash,
        "execution_authorized": False,
    }
    return EvidenceQueryPlanDrift(
        status=status,
        reasons=tuple(unsigned["reasons"]),
        corpus_hash=corpus.corpus_hash,
        source_identity_hash=corpus.source_identity_hash,
        source_watermark=corpus.source_watermark,
        observation_count=len(ordered),
        drift_count=len(reported_drift),
        drifted_query_fingerprints=reported_drift,
        observed_max_age_seconds=observed_age,
        max_evidence_age_seconds=max_evidence_age_seconds,
        observations_hash=observations_hash,
        detector_hash=_hash(unsigned),
    )


def validate_query_plan_drift(drift: Any) -> None:
    if not isinstance(drift, EvidenceQueryPlanDrift):
        raise EvidenceQueryPlanDriftError("DETECTOR_RESULT_TYPE_INVALID")
    if drift.execution_authorized is not False:
        raise EvidenceQueryPlanDriftError("DETECTOR_SAFETY_BOUNDARY_INVALID")
    if drift.status == "STABLE" and (drift.reasons or drift.drift_count):
        raise EvidenceQueryPlanDriftError("STABLE_STATE_INVALID")
    if drift.status == "DRIFT" and not drift.drifted_query_fingerprints:
        raise EvidenceQueryPlanDriftError("DRIFT_DETAILS_MISSING")
    if drift.status == "STALE" and drift.drifted_query_fingerprints:
        raise EvidenceQueryPlanDriftError("STALE_DRIFT_DETAILS_INVALID")
    unsigned = asdict(drift)
    unsigned.pop("detector_hash")
    unsigned["schema_version"] = DETECTOR_SCHEMA_VERSION
    unsigned["reasons"] = sorted(unsigned["reasons"])
    unsigned["drifted_query_fingerprints"] = list(
        unsigned["drifted_query_fingerprints"]
    )
    if drift.detector_hash != _hash(unsigned):
        raise EvidenceQueryPlanDriftError("DETECTOR_HASH_MISMATCH")


def _validated_observation(value: Any) -> QueryPlanObservation:
    if not isinstance(value, QueryPlanObservation):
        raise EvidenceQueryPlanDriftError("OBSERVATION_TYPE_INVALID")
    unsigned = asdict(value)
    supplied_hash = unsigned.pop("observation_hash")
    _validate_observation_fields(unsigned)
    if supplied_hash != _hash(unsigned):
        raise EvidenceQueryPlanDriftError("OBSERVATION_HASH_MISMATCH")
    return value


def _validate_observation_fields(payload: dict[str, Any]) -> None:
    for key in (
        "query_fingerprint",
        "baseline_plan_hash",
        "current_plan_hash",
        "corpus_hash",
        "source_identity_hash",
        "source_watermark",
    ):
        if not isinstance(payload[key], str) or not payload[key]:
            raise EvidenceQueryPlanDriftError("OBSERVATION_FIELD_INVALID")
    age = payload["evidence_age_seconds"]
    if isinstance(age, bool) or not isinstance(age, int) or age < 0:
        raise EvidenceQueryPlanDriftError("OBSERVATION_FIELD_INVALID")


def _hash(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
