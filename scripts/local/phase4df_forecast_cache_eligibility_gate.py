"""Decide forecast-cache reuse from exact, hash-protected offline evidence."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

INPUT_SCHEMA = "phase4df.cache-gate-input.v1"
REPORT_SCHEMA = "phase4df.cache-gate-report.v1"
MAX_CANDIDATES = 10_000


def _hash(payload: Any) -> str:
    if isinstance(payload, dict):
        payload = {key: value for key, value in payload.items() if key != "artifact_hash"}
    return canonical_hash(payload)


def _digest(value: Any) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise ValueError("PHASE4DF_HASH_INVALID")
    try:
        int(value, 16)
    except ValueError as exc:
        raise ValueError("PHASE4DF_HASH_INVALID") from exc
    return value


def _time(value: Any) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("PHASE4DF_TIMESTAMP_INVALID")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ValueError("PHASE4DF_TIMESTAMP_INVALID") from exc
    if parsed.tzinfo != UTC:
        raise ValueError("PHASE4DF_TIMESTAMP_INVALID")
    return parsed


def build_report(payload: dict[str, Any]) -> dict[str, Any]:
    if set(payload) != {"schema", "request", "candidates", "artifact_hash"}:
        raise ValueError("PHASE4DF_INPUT_FIELDS_INVALID")
    if payload.get("schema") != INPUT_SCHEMA or payload.get("artifact_hash") != _hash(payload):
        raise ValueError("PHASE4DF_INPUT_SCHEMA_OR_HASH_INVALID")
    request = payload["request"]
    request_fields = {
        "request_id",
        "market_id",
        "ticker",
        "model_id",
        "model_hash",
        "feature_set_hash",
        "evidence_hash",
        "evaluated_at",
        "minimum_evidence_observed_at",
    }
    if not isinstance(request, dict) or set(request) != request_fields:
        raise ValueError("PHASE4DF_REQUEST_FIELDS_INVALID")
    for field in ("request_id", "market_id", "ticker", "model_id"):
        if not isinstance(request[field], str) or not request[field]:
            raise ValueError("PHASE4DF_REQUEST_IDENTITY_INVALID")
    for field in ("model_hash", "feature_set_hash", "evidence_hash"):
        _digest(request[field])
    evaluated_at = _time(request["evaluated_at"])
    minimum_observed = _time(request["minimum_evidence_observed_at"])
    if minimum_observed > evaluated_at:
        raise ValueError("PHASE4DF_FRESHNESS_POLICY_INVALID")

    candidates = payload["candidates"]
    if not isinstance(candidates, list) or len(candidates) > MAX_CANDIDATES:
        raise ValueError("PHASE4DF_CANDIDATES_INVALID")
    candidate_fields = {
        "cache_id",
        "market_id",
        "ticker",
        "model_id",
        "model_hash",
        "feature_set_hash",
        "evidence_hash",
        "evidence_observed_at",
        "created_at",
        "expires_at",
        "deterministic",
        "complete",
        "result_hash",
        "artifact_hash",
    }
    identifiers: set[str] = set()
    decisions = []
    eligible = []
    for candidate in candidates:
        if not isinstance(candidate, dict) or set(candidate) != candidate_fields:
            raise ValueError("PHASE4DF_CANDIDATE_FIELDS_INVALID")
        if candidate["artifact_hash"] != _hash(candidate):
            raise ValueError("PHASE4DF_CANDIDATE_HASH_INVALID")
        identifier = candidate["cache_id"]
        if not isinstance(identifier, str) or not identifier or identifier in identifiers:
            raise ValueError("PHASE4DF_CACHE_ID_INVALID")
        identifiers.add(identifier)
        for field in ("market_id", "ticker", "model_id"):
            if not isinstance(candidate[field], str) or not candidate[field]:
                raise ValueError("PHASE4DF_CANDIDATE_IDENTITY_INVALID")
        for field in ("model_hash", "feature_set_hash", "evidence_hash", "result_hash"):
            _digest(candidate[field])
        observed = _time(candidate["evidence_observed_at"])
        created = _time(candidate["created_at"])
        expires = _time(candidate["expires_at"])
        if observed > created or created > expires:
            raise ValueError("PHASE4DF_CANDIDATE_TIMELINE_INVALID")
        if not isinstance(candidate["deterministic"], bool) or not isinstance(
            candidate["complete"], bool
        ):
            raise ValueError("PHASE4DF_CANDIDATE_POLICY_INVALID")
        reasons = []
        for field in (
            "market_id",
            "ticker",
            "model_id",
            "model_hash",
            "feature_set_hash",
            "evidence_hash",
        ):
            if candidate[field] != request[field]:
                reasons.append(f"{field.upper()}_MISMATCH")
        if observed < minimum_observed:
            reasons.append("EVIDENCE_TOO_OLD")
        if evaluated_at < created:
            reasons.append("CACHE_NOT_YET_VALID")
        if evaluated_at > expires:
            reasons.append("CACHE_EXPIRED")
        if not candidate["deterministic"]:
            reasons.append("NONDETERMINISTIC_RESULT")
        if not candidate["complete"]:
            reasons.append("INCOMPLETE_RESULT")
        decision = {"cache_id": identifier, "eligible": not reasons, "reasons": reasons}
        decisions.append(decision)
        if not reasons:
            eligible.append(candidate)

    if len(eligible) == 1:
        disposition, selected, reasons = "REUSE", eligible[0]["cache_id"], []
    elif len(eligible) > 1:
        disposition, selected, reasons = "REFUSE", None, ["AMBIGUOUS_ELIGIBLE_CANDIDATES"]
    else:
        disposition, selected, reasons = "RECOMPUTE", None, ["NO_EXACT_ELIGIBLE_CANDIDATE"]
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "phase": "4DF",
        "input_hash": payload["artifact_hash"],
        "request_id": request["request_id"],
        "disposition": disposition,
        "selected_cache_id": selected,
        "reasons": reasons,
        "candidate_decisions": decisions,
        "cache_writes": 0,
        "forecast_records_created": 0,
        "execution_authorized": False,
    }
    report["artifact_hash"] = _hash(report)
    return report


def publish(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = build_report(json.loads(args.input.read_text(encoding="utf-8")))
    publish(args.output, report)
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
