"""Detect inclusion-minimal cross-candidate risk conflicts using exact arithmetic."""

from __future__ import annotations

import argparse
import itertools
import json
import os
import tempfile
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

INPUT_SCHEMA = "phase4eh.conflict-input.v1"
REPORT_SCHEMA = "phase4eh.conflict-report.v1"
METRICS = ("exposure", "expected_loss", "drawdown", "liquidity_use")
MAX_CANDIDATES = 16


def _hash(value: Any) -> str:
    if isinstance(value, dict):
        value = {key: item for key, item in value.items() if key != "artifact_hash"}
    return canonical_hash(value)


def _decimal(value: Any) -> Decimal:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError("PHASE4EH_DECIMAL_INVALID")
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("PHASE4EH_DECIMAL_INVALID") from exc
    if not result.is_finite() or result < 0:
        raise ValueError("PHASE4EH_DECIMAL_INVALID")
    return result


def _canonical_decimal(value: Decimal) -> str:
    return format(value, "f")


def build_report(payload: dict[str, Any]) -> dict[str, Any]:
    if set(payload) != {
        "schema",
        "snapshot_artifact_hash",
        "limits",
        "candidates",
        "artifact_hash",
    }:
        raise ValueError("PHASE4EH_INPUT_FIELDS_INVALID")
    if payload.get("schema") != INPUT_SCHEMA or payload.get("artifact_hash") != _hash(payload):
        raise ValueError("PHASE4EH_INPUT_SCHEMA_OR_HASH_INVALID")
    snapshot_hash = payload["snapshot_artifact_hash"]
    if not isinstance(snapshot_hash, str) or len(snapshot_hash) != 64:
        raise ValueError("PHASE4EH_SNAPSHOT_HASH_INVALID")
    try:
        int(snapshot_hash, 16)
    except ValueError as exc:
        raise ValueError("PHASE4EH_SNAPSHOT_HASH_INVALID") from exc
    limits = payload.get("limits")
    limit_fields = {
        "max_exposure",
        "max_expected_loss",
        "max_drawdown",
        "max_liquidity_use",
        "max_group_concentration",
    }
    if not isinstance(limits, dict) or set(limits) != limit_fields:
        raise ValueError("PHASE4EH_LIMIT_FIELDS_INVALID")
    parsed_limits = {key: _decimal(value) for key, value in limits.items()}
    candidates = payload.get("candidates")
    if not isinstance(candidates, list) or not candidates or len(candidates) > MAX_CANDIDATES:
        raise ValueError("PHASE4EH_CANDIDATE_COUNT_INVALID")
    seen = set()
    parsed = {}
    candidate_fields = {
        "candidate_id",
        "snapshot_artifact_hash",
        *METRICS,
        "concentration_group",
        "concentration",
    }
    for row in candidates:
        if not isinstance(row, dict) or set(row) != candidate_fields:
            raise ValueError("PHASE4EH_CANDIDATE_FIELDS_INVALID")
        identifier = row["candidate_id"]
        if not isinstance(identifier, str) or not identifier or identifier in seen:
            raise ValueError("PHASE4EH_CANDIDATE_ID_INVALID")
        seen.add(identifier)
        if row["snapshot_artifact_hash"] != snapshot_hash:
            raise ValueError("PHASE4EH_SNAPSHOT_MISMATCH")
        group = row["concentration_group"]
        if not isinstance(group, str) or not group:
            raise ValueError("PHASE4EH_GROUP_INVALID")
        parsed[identifier] = {metric: _decimal(row[metric]) for metric in METRICS}
        parsed[identifier]["concentration"] = _decimal(row["concentration"])
        parsed[identifier]["group"] = group

    identifiers = sorted(parsed)
    conflicts = []
    minimal_sets: list[frozenset[str]] = []
    for size in range(1, len(identifiers) + 1):
        for combination in itertools.combinations(identifiers, size):
            chosen = frozenset(combination)
            if any(existing.issubset(chosen) for existing in minimal_sets):
                continue
            totals = {
                metric: sum((parsed[name][metric] for name in combination), Decimal(0))
                for metric in METRICS
            }
            group_totals: dict[str, Decimal] = {}
            for name in combination:
                group = str(parsed[name]["group"])
                group_totals[group] = (
                    group_totals.get(group, Decimal(0)) + parsed[name]["concentration"]
                )
            reasons = []
            for metric, limit_name, reason in (
                ("exposure", "max_exposure", "EXPOSURE_LIMIT_EXCEEDED"),
                ("expected_loss", "max_expected_loss", "EXPECTED_LOSS_LIMIT_EXCEEDED"),
                ("drawdown", "max_drawdown", "DRAWDOWN_LIMIT_EXCEEDED"),
                ("liquidity_use", "max_liquidity_use", "LIQUIDITY_LIMIT_EXCEEDED"),
            ):
                if totals[metric] > parsed_limits[limit_name]:
                    reasons.append(reason)
            breached_groups = sorted(
                group
                for group, total in group_totals.items()
                if total > parsed_limits["max_group_concentration"]
            )
            reasons.extend(f"CONCENTRATION_LIMIT_EXCEEDED:{group}" for group in breached_groups)
            if reasons:
                minimal_sets.append(chosen)
                conflicts.append(
                    {
                        "candidate_ids": list(combination),
                        "reasons": reasons,
                        "totals": {key: _canonical_decimal(value) for key, value in totals.items()},
                        "group_concentrations": {
                            key: _canonical_decimal(group_totals[key])
                            for key in sorted(group_totals)
                        },
                    }
                )
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "phase": "4EH",
        "input_hash": payload["artifact_hash"],
        "snapshot_artifact_hash": snapshot_hash,
        "status": "CONFLICTS_DETECTED" if conflicts else "NO_CONFLICTS",
        "minimal_conflicts": conflicts,
        "conflict_count": len(conflicts),
        "candidate_count": len(identifiers),
        "capital_reserved": False,
        "risk_decisions_created": 0,
        "execution_authorized": False,
        "production_records_created": 0,
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
