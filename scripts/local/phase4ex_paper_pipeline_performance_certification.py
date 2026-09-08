"""Certify deterministic paper-pipeline latency improvement without enabling execution."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

INPUT_SCHEMA = "phase4ex.certification-input.v1"
REPORT_SCHEMA = "phase4ex.certification-report.v1"
PROOF_FIELDS = {
    "risk_paper_equivalence_certified",
    "airgapped_acceptance_passed",
    "mutation_scanner_advancement_allowed",
    "equivalence_artifact_hash",
    "acceptance_artifact_hash",
    "scanner_artifact_hash",
}


def _hash(value: Any) -> str:
    if isinstance(value, dict):
        value = {key: item for key, item in value.items() if key != "artifact_hash"}
    return canonical_hash(value)


def _digest(value: Any, code: str) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise ValueError(code)
    try:
        int(value, 16)
    except ValueError as exc:
        raise ValueError(code) from exc
    return value


def _integer(value: Any, minimum: int, maximum: int, code: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(code)
    return value


def build_report(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != {
        "schema",
        "proofs",
        "minimum_aggregate_improvement_bps",
        "scenarios",
        "artifact_hash",
    }:
        raise ValueError("PHASE4EX_INPUT_FIELDS_INVALID")
    if payload.get("schema") != INPUT_SCHEMA or payload.get("artifact_hash") != _hash(payload):
        raise ValueError("PHASE4EX_INPUT_SCHEMA_OR_HASH_INVALID")
    proofs = payload["proofs"]
    if not isinstance(proofs, dict) or set(proofs) != PROOF_FIELDS:
        raise ValueError("PHASE4EX_PROOF_FIELDS_INVALID")
    for field in (
        "risk_paper_equivalence_certified",
        "airgapped_acceptance_passed",
        "mutation_scanner_advancement_allowed",
    ):
        if not isinstance(proofs[field], bool):
            raise ValueError("PHASE4EX_PROOF_STATUS_INVALID")
    proof_hashes = {
        "equivalence_artifact_hash": _digest(
            proofs["equivalence_artifact_hash"], "PHASE4EX_EQUIVALENCE_HASH_INVALID"
        ),
        "acceptance_artifact_hash": _digest(
            proofs["acceptance_artifact_hash"], "PHASE4EX_ACCEPTANCE_HASH_INVALID"
        ),
        "scanner_artifact_hash": _digest(
            proofs["scanner_artifact_hash"], "PHASE4EX_SCANNER_HASH_INVALID"
        ),
    }
    minimum_bps = _integer(
        payload["minimum_aggregate_improvement_bps"],
        0,
        10_000,
        "PHASE4EX_MINIMUM_IMPROVEMENT_INVALID",
    )
    scenarios = payload["scenarios"]
    if not isinstance(scenarios, list) or not scenarios:
        raise ValueError("PHASE4EX_SCENARIOS_INVALID")
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    baseline_total = 0
    optimized_total = 0
    for scenario in scenarios:
        if not isinstance(scenario, dict) or set(scenario) != {
            "scenario_id",
            "baseline_latency_us",
            "optimized_latency_us",
            "behavior_equivalent",
        }:
            raise ValueError("PHASE4EX_SCENARIO_FIELDS_INVALID")
        scenario_id = scenario["scenario_id"]
        if (
            not isinstance(scenario_id, str)
            or not scenario_id
            or scenario_id.strip() != scenario_id
            or scenario_id in seen
        ):
            raise ValueError("PHASE4EX_SCENARIO_ID_INVALID")
        seen.add(scenario_id)
        baseline = _integer(
            scenario["baseline_latency_us"], 1, 10**12, "PHASE4EX_BASELINE_LATENCY_INVALID"
        )
        optimized = _integer(
            scenario["optimized_latency_us"], 0, 10**12, "PHASE4EX_OPTIMIZED_LATENCY_INVALID"
        )
        if not isinstance(scenario["behavior_equivalent"], bool):
            raise ValueError("PHASE4EX_BEHAVIOR_EQUIVALENCE_INVALID")
        improvement = baseline - optimized
        improvement_bps = improvement * 10_000 // baseline
        reasons: list[str] = []
        if not scenario["behavior_equivalent"]:
            reasons.append("BEHAVIOR_NOT_EQUIVALENT")
        if optimized > baseline:
            reasons.append("LATENCY_REGRESSION")
        baseline_total += baseline
        optimized_total += optimized
        results.append(
            {
                "scenario_id": scenario_id,
                "baseline_latency_us": baseline,
                "optimized_latency_us": optimized,
                "improvement_us": improvement,
                "improvement_bps": improvement_bps,
                "behavior_equivalent": scenario["behavior_equivalent"],
                "passed": not reasons,
                "reason_codes": reasons,
            }
        )
    results.sort(key=lambda item: item["scenario_id"])
    aggregate_improvement = baseline_total - optimized_total
    aggregate_bps = aggregate_improvement * 10_000 // baseline_total
    gate_reasons: list[str] = []
    if not proofs["risk_paper_equivalence_certified"]:
        gate_reasons.append("RISK_PAPER_EQUIVALENCE_PROOF_FAILED")
    if not proofs["airgapped_acceptance_passed"]:
        gate_reasons.append("AIRGAPPED_ACCEPTANCE_PROOF_FAILED")
    if not proofs["mutation_scanner_advancement_allowed"]:
        gate_reasons.append("MUTATION_SCANNER_PROOF_FAILED")
    gate_reasons.extend(
        f"SCENARIO_{result['scenario_id']}_{reason}"
        for result in results
        for reason in result["reason_codes"]
    )
    if aggregate_bps < minimum_bps:
        gate_reasons.append("AGGREGATE_IMPROVEMENT_BELOW_THRESHOLD")
    certified = not gate_reasons
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "phase": "4EX",
        "input_hash": payload["artifact_hash"],
        "proof_artifact_hashes": proof_hashes,
        "measurement_unit": "integer_microseconds",
        "wall_clock_used_as_correctness_gate": False,
        "minimum_aggregate_improvement_bps": minimum_bps,
        "scenario_results": results,
        "scenario_count": len(results),
        "baseline_total_us": baseline_total,
        "optimized_total_us": optimized_total,
        "aggregate_improvement_us": aggregate_improvement,
        "aggregate_improvement_bps": aggregate_bps,
        "certification_reason_codes": gate_reasons,
        "paper_pipeline_performance_certified": certified,
        "paper_order_creation_enabled": False,
        "paper_order_creation_authorized": False,
        "paper_orders_created": 0,
        "production_database_mutated": False,
        "services_controlled": False,
        "exchange_requests_made": False,
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
