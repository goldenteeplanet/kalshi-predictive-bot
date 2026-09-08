"""Certify Workstream I from hash-valid latency and optimization artifacts."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

INPUT_SCHEMA = "phase4bz.certification-bundle.v1"
REPORT_SCHEMA = "phase4bz.workstream-i-certification.v1"
EVIDENCE_SCHEMAS = (
    "phase4bp.time-to-trade-baseline.v1",
    "phase4bq.critical-path-dag.v1",
    "phase4br.stage-latency-budget.v1",
    "phase4bw.read-only-profile.v1",
    "phase4bx.algorithmic-hotspot-audit.v1",
    "phase4by.optimization-equivalence-proof.v1",
)
BENCHMARK_NAMES = ("small", "medium", "large", "sparse", "burst")


def _hash(payload: Any) -> str:
    if isinstance(payload, dict):
        payload = {key: value for key, value in payload.items() if key != "artifact_hash"}
    return canonical_hash(payload)


def build_certification(bundle: dict[str, Any]) -> dict[str, Any]:
    if bundle.get("schema") != INPUT_SCHEMA or bundle.get("artifact_hash") != _hash(bundle):
        raise ValueError("PHASE4BZ_INPUT_SCHEMA_OR_HASH_INVALID")
    evidence = bundle.get("evidence")
    benchmarks = bundle.get("benchmarks")
    uncertainty = bundle.get("residual_uncertainty")
    if not isinstance(evidence, list) or not isinstance(benchmarks, list):
        raise ValueError("PHASE4BZ_EVIDENCE_OR_BENCHMARKS_INVALID")
    if [row.get("schema") for row in evidence if isinstance(row, dict)] != list(EVIDENCE_SCHEMAS):
        raise ValueError("PHASE4BZ_EVIDENCE_COVERAGE_OR_ORDER_INVALID")
    evidence_hashes = []
    for artifact in evidence:
        if artifact.get("artifact_hash") != _hash(artifact):
            raise ValueError("PHASE4BZ_EVIDENCE_HASH_INVALID")
        if artifact.get("execution_authorized") is not False:
            raise ValueError("PHASE4BZ_EVIDENCE_AUTHORITY_INVALID")
        evidence_hashes.append(artifact["artifact_hash"])
    by_schema = {artifact["schema"]: artifact for artifact in evidence}
    profile = by_schema["phase4bw.read-only-profile.v1"]
    proof = by_schema["phase4by.optimization-equivalence-proof.v1"]
    if profile.get("all_envelopes_satisfied") is not True:
        raise ValueError("PHASE4BZ_PROFILE_ENVELOPE_FAILURE")
    if (
        proof.get("all_outputs_byte_identical") is not True
        or proof.get("refusal_order_changed") is not False
    ):
        raise ValueError("PHASE4BZ_EQUIVALENCE_PROOF_FAILURE")
    if [row.get("name") for row in benchmarks if isinstance(row, dict)] != list(BENCHMARK_NAMES):
        raise ValueError("PHASE4BZ_BENCHMARK_COVERAGE_OR_ORDER_INVALID")
    normalized_benchmarks = []
    for row in benchmarks:
        if set(row) != {"name", "before_elapsed_ns", "after_elapsed_ns", "output_hash"}:
            raise ValueError("PHASE4BZ_BENCHMARK_FIELDS_INVALID")
        before, after = row["before_elapsed_ns"], row["after_elapsed_ns"]
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in (before, after)
        ):
            raise ValueError("PHASE4BZ_BENCHMARK_VALUE_INVALID")
        if not isinstance(row["output_hash"], str) or len(row["output_hash"]) != 64:
            raise ValueError("PHASE4BZ_BENCHMARK_OUTPUT_HASH_INVALID")
        normalized_benchmarks.append(
            {
                **row,
                "delta_elapsed_ns": after - before,
                "non_regressing": after <= before,
            }
        )
    if not isinstance(uncertainty, list) or not uncertainty:
        raise ValueError("PHASE4BZ_RESIDUAL_UNCERTAINTY_MISSING")
    if any(not isinstance(item, str) or not item.strip() for item in uncertainty):
        raise ValueError("PHASE4BZ_RESIDUAL_UNCERTAINTY_INVALID")
    certified = all(row["non_regressing"] for row in normalized_benchmarks)
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "phase": "4BZ",
        "bundle_hash": bundle["artifact_hash"],
        "evidence_hashes": evidence_hashes,
        "benchmarks": normalized_benchmarks,
        "residual_uncertainty": uncertainty,
        "certification_state": (
            "WORKSTREAM_I_CERTIFIED" if certified else "WORKSTREAM_I_BENCHMARK_REGRESSION"
        ),
        "certified": certified,
        "production_records_created": 0,
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
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = build_certification(json.loads(args.bundle.read_text(encoding="utf-8")))
    publish(args.output, report)
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
