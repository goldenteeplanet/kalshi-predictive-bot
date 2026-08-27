from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any, Literal

CORPUS_SCHEMA_VERSION = "phase4gk-evidence-query-regression-corpus-v1"
CorpusStatus = Literal["READY", "STALE"]


class EvidenceQueryRegressionCorpusError(ValueError):
    """Stable fail-closed regression-corpus error."""


@dataclass(frozen=True)
class EvidenceQueryRegressionCase:
    case_id: str
    query_fingerprint: str
    fixture_hash: str
    expected_result_hash: str
    source_identity_hash: str
    source_watermark: str
    evidence_age_seconds: int
    case_hash: str


@dataclass(frozen=True)
class EvidenceQueryRegressionCorpus:
    status: CorpusStatus
    reasons: tuple[str, ...]
    source_identity_hash: str
    source_watermark: str
    case_count: int
    query_count: int
    observed_max_age_seconds: int
    max_evidence_age_seconds: int
    cases_hash: str
    corpus_hash: str
    execution_authorized: bool = False


def make_regression_case(
    *,
    case_id: str,
    query_fingerprint: str,
    fixture_hash: str,
    expected_result_hash: str,
    source_identity_hash: str,
    source_watermark: str,
    evidence_age_seconds: int,
) -> EvidenceQueryRegressionCase:
    unsigned = {
        "case_id": case_id,
        "query_fingerprint": query_fingerprint,
        "fixture_hash": fixture_hash,
        "expected_result_hash": expected_result_hash,
        "source_identity_hash": source_identity_hash,
        "source_watermark": source_watermark,
        "evidence_age_seconds": evidence_age_seconds,
    }
    _validate_case_fields(unsigned)
    return EvidenceQueryRegressionCase(**unsigned, case_hash=_hash(unsigned))


def build_evidence_query_regression_corpus(
    cases: Sequence[Any],
    *,
    max_cases: int = 256,
    max_evidence_age_seconds: int = 86_400,
) -> EvidenceQueryRegressionCorpus:
    if max_cases <= 0 or max_evidence_age_seconds < 0:
        raise EvidenceQueryRegressionCorpusError("CORPUS_BOUND_INVALID")
    if not cases:
        raise EvidenceQueryRegressionCorpusError("CASES_EMPTY")
    if len(cases) > max_cases:
        raise EvidenceQueryRegressionCorpusError("CASE_BOUND_EXCEEDED")
    validated = [_validated_case(item) for item in cases]
    case_ids = [item.case_id for item in validated]
    if len(set(case_ids)) != len(case_ids):
        raise EvidenceQueryRegressionCorpusError("CASE_ID_DUPLICATE")
    identities = {item.source_identity_hash for item in validated}
    watermarks = {item.source_watermark for item in validated}
    if len(identities) != 1 or len(watermarks) != 1:
        raise EvidenceQueryRegressionCorpusError("SOURCE_LINEAGE_MIXED")
    ordered = sorted(validated, key=lambda item: item.case_id)
    observed_age = max(item.evidence_age_seconds for item in ordered)
    reasons: list[str] = []
    if observed_age > max_evidence_age_seconds:
        status: CorpusStatus = "STALE"
        reasons.append("CORPUS_EVIDENCE_STALE")
    else:
        status = "READY"
    cases_hash = _hash([asdict(item) for item in ordered])
    unsigned = {
        "schema_version": CORPUS_SCHEMA_VERSION,
        "status": status,
        "reasons": sorted(reasons),
        "source_identity_hash": ordered[0].source_identity_hash,
        "source_watermark": ordered[0].source_watermark,
        "case_count": len(ordered),
        "query_count": len({item.query_fingerprint for item in ordered}),
        "observed_max_age_seconds": observed_age,
        "max_evidence_age_seconds": max_evidence_age_seconds,
        "cases_hash": cases_hash,
        "execution_authorized": False,
    }
    return EvidenceQueryRegressionCorpus(
        status=status,
        reasons=tuple(unsigned["reasons"]),
        source_identity_hash=ordered[0].source_identity_hash,
        source_watermark=ordered[0].source_watermark,
        case_count=len(ordered),
        query_count=unsigned["query_count"],
        observed_max_age_seconds=observed_age,
        max_evidence_age_seconds=max_evidence_age_seconds,
        cases_hash=cases_hash,
        corpus_hash=_hash(unsigned),
    )


def validate_regression_corpus(corpus: Any) -> None:
    if not isinstance(corpus, EvidenceQueryRegressionCorpus):
        raise EvidenceQueryRegressionCorpusError("CORPUS_RESULT_TYPE_INVALID")
    if corpus.execution_authorized is not False:
        raise EvidenceQueryRegressionCorpusError("CORPUS_SAFETY_BOUNDARY_INVALID")
    if corpus.status == "READY" and corpus.reasons:
        raise EvidenceQueryRegressionCorpusError("READY_REASONS_INVALID")
    if corpus.status == "STALE" and not corpus.reasons:
        raise EvidenceQueryRegressionCorpusError("STALE_REASONS_MISSING")
    unsigned = asdict(corpus)
    unsigned.pop("corpus_hash")
    unsigned["schema_version"] = CORPUS_SCHEMA_VERSION
    unsigned["reasons"] = sorted(unsigned["reasons"])
    if corpus.corpus_hash != _hash(unsigned):
        raise EvidenceQueryRegressionCorpusError("CORPUS_HASH_MISMATCH")


def _validated_case(value: Any) -> EvidenceQueryRegressionCase:
    if not isinstance(value, EvidenceQueryRegressionCase):
        raise EvidenceQueryRegressionCorpusError("CASE_TYPE_INVALID")
    unsigned = asdict(value)
    supplied_hash = unsigned.pop("case_hash")
    _validate_case_fields(unsigned)
    if supplied_hash != _hash(unsigned):
        raise EvidenceQueryRegressionCorpusError("CASE_HASH_MISMATCH")
    return value


def _validate_case_fields(payload: dict[str, Any]) -> None:
    for key in (
        "case_id",
        "query_fingerprint",
        "fixture_hash",
        "expected_result_hash",
        "source_identity_hash",
        "source_watermark",
    ):
        if not isinstance(payload[key], str) or not payload[key]:
            raise EvidenceQueryRegressionCorpusError("CASE_FIELD_INVALID")
    age = payload["evidence_age_seconds"]
    if isinstance(age, bool) or not isinstance(age, int) or age < 0:
        raise EvidenceQueryRegressionCorpusError("CASE_FIELD_INVALID")


def _hash(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
