"""Simulate source outages and evidence-gated failover without network access."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

INPUT_SCHEMA = "phase4co.failover-input.v1"
REPORT_SCHEMA = "phase4co.failover-report.v1"
MAX_SOURCES = 100
MAX_SCENARIOS = 1_000


def _hash(payload: Any) -> str:
    if isinstance(payload, dict):
        payload = {key: value for key, value in payload.items() if key != "artifact_hash"}
    return canonical_hash(payload)


def _digest(value: Any, error: str) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise ValueError(error)
    try:
        int(value, 16)
    except ValueError as exc:
        raise ValueError(error) from exc
    return value


def _pair(left: str, right: str) -> tuple[str, str]:
    return tuple(sorted((left, right)))


def build_report(payload: dict[str, Any]) -> dict[str, Any]:
    fields = {
        "schema",
        "primary_source",
        "sources",
        "equivalence_claims",
        "scenarios",
        "artifact_hash",
    }
    if set(payload) != fields:
        raise ValueError("PHASE4CO_INPUT_FIELDS_INVALID")
    if payload.get("schema") != INPUT_SCHEMA or payload.get("artifact_hash") != _hash(payload):
        raise ValueError("PHASE4CO_INPUT_SCHEMA_OR_HASH_INVALID")
    sources = payload.get("sources")
    if not isinstance(sources, list) or not sources or len(sources) > MAX_SOURCES:
        raise ValueError("PHASE4CO_SOURCE_COUNT_INVALID")
    by_id: dict[str, dict[str, Any]] = {}
    priorities: set[int] = set()
    for source in sources:
        required = {"source_id", "priority", "contract_hash"}
        if not isinstance(source, dict) or set(source) != required:
            raise ValueError("PHASE4CO_SOURCE_FIELDS_INVALID")
        identifier = source["source_id"]
        priority = source["priority"]
        if not isinstance(identifier, str) or not identifier or identifier in by_id:
            raise ValueError("PHASE4CO_SOURCE_ID_INVALID")
        if isinstance(priority, bool) or not isinstance(priority, int) or priority < 0:
            raise ValueError("PHASE4CO_PRIORITY_INVALID")
        if priority in priorities:
            raise ValueError("PHASE4CO_PRIORITY_DUPLICATE")
        _digest(source["contract_hash"], "PHASE4CO_CONTRACT_HASH_INVALID")
        by_id[identifier] = source
        priorities.add(priority)
    primary = payload.get("primary_source")
    if primary not in by_id:
        raise ValueError("PHASE4CO_PRIMARY_SOURCE_INVALID")

    claims = payload.get("equivalence_claims")
    if not isinstance(claims, list):
        raise ValueError("PHASE4CO_CLAIMS_INVALID")
    by_pair: dict[tuple[str, str], dict[str, Any]] = {}
    for claim in claims:
        required = {
            "left_source",
            "right_source",
            "left_contract_hash",
            "right_contract_hash",
            "schema_equivalent",
            "unit_equivalent",
            "timestamp_equivalent",
            "tolerance_proven",
        }
        if not isinstance(claim, dict) or set(claim) != required:
            raise ValueError("PHASE4CO_CLAIM_FIELDS_INVALID")
        left, right = claim["left_source"], claim["right_source"]
        if left not in by_id or right not in by_id or left == right:
            raise ValueError("PHASE4CO_CLAIM_SOURCE_INVALID")
        pair = _pair(left, right)
        if pair in by_pair:
            raise ValueError("PHASE4CO_CLAIM_DUPLICATE")
        for flag in (
            "schema_equivalent",
            "unit_equivalent",
            "timestamp_equivalent",
            "tolerance_proven",
        ):
            if not isinstance(claim[flag], bool):
                raise ValueError("PHASE4CO_CLAIM_FLAG_INVALID")
        by_pair[pair] = claim

    scenarios = payload.get("scenarios")
    if not isinstance(scenarios, list) or not scenarios or len(scenarios) > MAX_SCENARIOS:
        raise ValueError("PHASE4CO_SCENARIO_COUNT_INVALID")
    names: set[str] = set()
    results = []
    ordered = sorted(sources, key=lambda row: row["priority"])
    for scenario in scenarios:
        if not isinstance(scenario, dict) or set(scenario) != {"name", "unavailable_sources"}:
            raise ValueError("PHASE4CO_SCENARIO_FIELDS_INVALID")
        name = scenario["name"]
        unavailable = scenario["unavailable_sources"]
        if not isinstance(name, str) or not name or name in names:
            raise ValueError("PHASE4CO_SCENARIO_NAME_INVALID")
        if (
            not isinstance(unavailable, list)
            or len(set(unavailable)) != len(unavailable)
            or any(source not in by_id for source in unavailable)
        ):
            raise ValueError("PHASE4CO_UNAVAILABLE_SOURCES_INVALID")
        names.add(name)
        selected = None
        rejected = []
        for candidate in ordered:
            candidate_id = candidate["source_id"]
            if candidate_id in unavailable:
                continue
            if candidate_id == primary:
                selected = candidate_id
                break
            claim = by_pair.get(_pair(primary, candidate_id))
            if claim is None:
                rejected.append({"source_id": candidate_id, "reason": "EQUIVALENCE_MISSING"})
                continue
            hashes_match = (
                claim["left_contract_hash"] == by_id[claim["left_source"]]["contract_hash"]
                and claim["right_contract_hash"] == by_id[claim["right_source"]]["contract_hash"]
            )
            flags = all(
                claim[field]
                for field in (
                    "schema_equivalent",
                    "unit_equivalent",
                    "timestamp_equivalent",
                    "tolerance_proven",
                )
            )
            if hashes_match and flags:
                selected = candidate_id
                break
            reason = "CONTRACT_HASH_MISMATCH" if not hashes_match else "EQUIVALENCE_UNPROVEN"
            rejected.append({"source_id": candidate_id, "reason": reason})
        status = "PRIMARY" if selected == primary else "FAILOVER" if selected else "REFUSE"
        results.append(
            {
                "name": name,
                "status": status,
                "selected_source": selected,
                "rejected_candidates": rejected,
            }
        )

    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "phase": "4CO",
        "input_hash": payload["artifact_hash"],
        "scenarios": results,
        "network_calls_performed": 0,
        "runtime_failover_applied": False,
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
    parser.add_argument("--simulation", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = build_report(json.loads(args.simulation.read_text(encoding="utf-8")))
    publish(args.output, report)
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
