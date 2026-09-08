"""Audit bounded stable top-K selection against a deterministic full sort."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

INPUT_SCHEMA = "phase4dl.top-k-input.v1"
REPORT_SCHEMA = "phase4dl.top-k-report.v1"
MAX_CANDIDATES = 100_000


def _hash(payload: Any) -> str:
    if isinstance(payload, dict):
        payload = {key: value for key, value in payload.items() if key != "artifact_hash"}
    return canonical_hash(payload)


def _score(value: Any) -> Decimal:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError("PHASE4DL_SCORE_INVALID")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("PHASE4DL_SCORE_INVALID") from exc
    if not parsed.is_finite():
        raise ValueError("PHASE4DL_SCORE_INVALID")
    return parsed


def _render(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in {"", "-0"} else text


def _key(candidate: dict[str, str]) -> tuple[Decimal, str]:
    return (-_score(candidate["score"]), candidate["candidate_id"])


def build_report(payload: dict[str, Any]) -> dict[str, Any]:
    if set(payload) != {"schema", "k", "candidates", "artifact_hash"}:
        raise ValueError("PHASE4DL_INPUT_FIELDS_INVALID")
    if payload.get("schema") != INPUT_SCHEMA or payload.get("artifact_hash") != _hash(payload):
        raise ValueError("PHASE4DL_INPUT_SCHEMA_OR_HASH_INVALID")
    candidates = payload["candidates"]
    if not isinstance(candidates, list) or not candidates or len(candidates) > MAX_CANDIDATES:
        raise ValueError("PHASE4DL_CANDIDATE_COUNT_INVALID")
    k = payload["k"]
    if not isinstance(k, int) or isinstance(k, bool) or not 1 <= k <= len(candidates):
        raise ValueError("PHASE4DL_K_INVALID")
    identifiers: set[str] = set()
    normalized = []
    for candidate in candidates:
        if not isinstance(candidate, dict) or set(candidate) != {"candidate_id", "score"}:
            raise ValueError("PHASE4DL_CANDIDATE_FIELDS_INVALID")
        identifier = candidate["candidate_id"]
        if not isinstance(identifier, str) or not identifier or identifier in identifiers:
            raise ValueError("PHASE4DL_CANDIDATE_ID_INVALID")
        identifiers.add(identifier)
        normalized.append(
            {"candidate_id": identifier, "score": _render(_score(candidate["score"]))}
        )
    baseline = sorted(normalized, key=_key)[:k]
    bounded: list[dict[str, str]] = []
    comparison_units = 0
    for candidate in normalized:
        insertion = len(bounded)
        for index, existing in enumerate(bounded):
            comparison_units += 1
            if _key(candidate) < _key(existing):
                insertion = index
                break
        bounded.insert(insertion, candidate)
        if len(bounded) > k:
            bounded.pop()
    if bounded != baseline:
        raise ValueError("PHASE4DL_TOP_K_EQUIVALENCE_FAILED")
    selected = [{"rank": index, **candidate} for index, candidate in enumerate(bounded, start=1)]
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "phase": "4DL",
        "input_hash": payload["artifact_hash"],
        "candidate_count": len(normalized),
        "k": k,
        "selected": selected,
        "baseline_hash": canonical_hash(baseline),
        "bounded_hash": canonical_hash(bounded),
        "full_equivalence": True,
        "stable_tie_breaker": "candidate_id ASC",
        "bounded_comparison_units": comparison_units,
        "comparison_unit_upper_bound": len(normalized) * k,
        "wall_clock_used_as_gate": False,
        "ranking_records_created": 0,
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
