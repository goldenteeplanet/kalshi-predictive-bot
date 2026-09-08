"""Apply offline changed-candidate updates with full-ranking equivalence."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

INPUT_SCHEMA = "phase4dk.incremental-ranking-input.v1"
REPORT_SCHEMA = "phase4dk.incremental-ranking-report.v1"
MAX_CANDIDATES = 100_000


def _hash(payload: Any) -> str:
    if isinstance(payload, dict):
        payload = {key: value for key, value in payload.items() if key != "artifact_hash"}
    return canonical_hash(payload)


def _score(value: Any) -> Decimal:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError("PHASE4DK_SCORE_INVALID")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("PHASE4DK_SCORE_INVALID") from exc
    if not parsed.is_finite():
        raise ValueError("PHASE4DK_SCORE_INVALID")
    return parsed


def _render(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in {"", "-0"} else text


def _ranking(candidates: dict[str, str]) -> list[dict[str, Any]]:
    ordered = sorted(candidates.items(), key=lambda item: (-_score(item[1]), item[0]))
    return [
        {"rank": index, "candidate_id": identifier, "score": _render(_score(score))}
        for index, (identifier, score) in enumerate(ordered, start=1)
    ]


def build_report(payload: dict[str, Any]) -> dict[str, Any]:
    required = {"schema", "candidates", "previous_ranking", "updates", "artifact_hash"}
    if set(payload) != required:
        raise ValueError("PHASE4DK_INPUT_FIELDS_INVALID")
    if payload.get("schema") != INPUT_SCHEMA or payload.get("artifact_hash") != _hash(payload):
        raise ValueError("PHASE4DK_INPUT_SCHEMA_OR_HASH_INVALID")
    candidates = payload["candidates"]
    if (
        not isinstance(candidates, dict)
        or not candidates
        or len(candidates) > MAX_CANDIDATES
        or any(not isinstance(key, str) or not key for key in candidates)
    ):
        raise ValueError("PHASE4DK_CANDIDATES_INVALID")
    normalized = {identifier: _render(_score(score)) for identifier, score in candidates.items()}
    previous = payload["previous_ranking"]
    if not isinstance(previous, list) or previous != _ranking(normalized):
        raise ValueError("PHASE4DK_PREVIOUS_RANKING_MISMATCH")
    updates = payload["updates"]
    if not isinstance(updates, list) or not updates or len(updates) > MAX_CANDIDATES:
        raise ValueError("PHASE4DK_UPDATE_COUNT_INVALID")
    changed: set[str] = set()
    working = dict(normalized)
    for update in updates:
        if not isinstance(update, dict) or set(update) != {"candidate_id", "operation", "score"}:
            raise ValueError("PHASE4DK_UPDATE_FIELDS_INVALID")
        identifier = update["candidate_id"]
        if not isinstance(identifier, str) or not identifier or identifier in changed:
            raise ValueError("PHASE4DK_UPDATE_ID_INVALID")
        changed.add(identifier)
        if update["operation"] == "UPSERT":
            working[identifier] = _render(_score(update["score"]))
        elif update["operation"] == "DELETE":
            if update["score"] is not None or identifier not in working:
                raise ValueError("PHASE4DK_DELETE_INVALID")
            del working[identifier]
        else:
            raise ValueError("PHASE4DK_OPERATION_INVALID")
    if not working:
        raise ValueError("PHASE4DK_EMPTY_RANKING_INVALID")
    incremental = _ranking(working)
    full_baseline = _ranking(dict(working))
    if incremental != full_baseline:
        raise ValueError("PHASE4DK_FULL_EQUIVALENCE_FAILED")
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "phase": "4DK",
        "input_hash": payload["artifact_hash"],
        "changed_candidate_ids": sorted(changed),
        "unchanged_candidate_ids": sorted(set(working) - changed),
        "ranking": incremental,
        "incremental_ranking_hash": canonical_hash(incremental),
        "full_ranking_hash": canonical_hash(full_baseline),
        "full_equivalence": True,
        "candidate_count": len(working),
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
