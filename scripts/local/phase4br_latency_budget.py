"""Build deterministic stage-level latency budgets and compliance evidence."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

INPUT_SCHEMA = "phase4br.latency-budget-input.v1"
SCHEMA = "phase4br.stage-latency-budget.v1"
COMPLIANCE_SCHEMA = "phase4br.latency-budget-compliance.v1"
NODES = (
    "COLLECTION",
    "SNAPSHOT",
    "FORECAST",
    "RANKING",
    "POSITION_SIZING",
    "ADVANCED_RISK",
    "APPROVAL",
    "PAPER_ROUTING",
    "OBSERVABILITY",
)
BUDGET_CLASSES = ("HARD_DEADLINE", "SOFT_BUDGET", "EXPECTED_WAIT", "EXTERNAL_DELAY")
MAX_BUDGET_MS = 86_400_000


def _hash(payload: dict[str, Any]) -> str:
    return canonical_hash({key: value for key, value in payload.items() if key != "artifact_hash"})


def build(payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    if payload.get("schema") != INPUT_SCHEMA or payload.get("artifact_hash") != _hash(payload):
        raise ValueError("PHASE4BR_INPUT_SCHEMA_OR_HASH_INVALID")
    for field in ("baseline_hash", "dag_hash"):
        if not isinstance(payload.get(field), str) or len(payload[field]) != 64:
            raise ValueError("PHASE4BR_UPSTREAM_HASH_INVALID")
    rows = payload.get("budgets")
    if not isinstance(rows, list) or [
        row.get("node") for row in rows if isinstance(row, dict)
    ] != list(NODES):
        raise ValueError("PHASE4BR_BUDGET_COVERAGE_OR_ORDER_INVALID")
    normalized: list[dict[str, Any]] = []
    compliance: list[dict[str, Any]] = []
    for row in rows:
        if set(row) != {"node", "budget_class", "budget_ms", "observed_p95_ms", "rationale_hash"}:
            raise ValueError("PHASE4BR_BUDGET_FIELDS_INVALID")
        budget, observed = row["budget_ms"], row["observed_p95_ms"]
        if row["budget_class"] not in BUDGET_CLASSES:
            raise ValueError("PHASE4BR_BUDGET_CLASS_INVALID")
        if any(
            not isinstance(value, int) or isinstance(value, bool) for value in (budget, observed)
        ):
            raise ValueError("PHASE4BR_LATENCY_TYPE_INVALID")
        if not 0 < budget <= MAX_BUDGET_MS or not 0 <= observed <= MAX_BUDGET_MS:
            raise ValueError("PHASE4BR_LATENCY_BOUND_INVALID")
        if not isinstance(row["rationale_hash"], str) or len(row["rationale_hash"]) != 64:
            raise ValueError("PHASE4BR_RATIONALE_HASH_INVALID")
        normalized.append(dict(row))
        state = (
            "WITHIN_BUDGET"
            if observed <= budget
            else (
                "HARD_DEADLINE_BREACH"
                if row["budget_class"] == "HARD_DEADLINE"
                else "BUDGET_BREACH"
            )
        )
        compliance.append(
            {
                "node": row["node"],
                "budget_class": row["budget_class"],
                "budget_ms": budget,
                "observed_p95_ms": observed,
                "headroom_ms": budget - observed,
                "state": state,
            }
        )
    class_coverage = {
        name: sum(row["budget_class"] == name for row in rows) for name in BUDGET_CLASSES
    }
    if any(count == 0 for count in class_coverage.values()):
        raise ValueError("PHASE4BR_BUDGET_CLASS_COVERAGE_INVALID")
    budget_artifact: dict[str, Any] = {
        "schema": SCHEMA,
        "phase": "4BR",
        "baseline_hash": payload["baseline_hash"],
        "dag_hash": payload["dag_hash"],
        "budgets": normalized,
        "class_coverage": class_coverage,
        "total_budget_ms": sum(row["budget_ms"] for row in rows),
        "configuration_applied": False,
        "execution_authorized": False,
    }
    budget_artifact["artifact_hash"] = _hash(budget_artifact)
    breaches = [row for row in compliance if row["state"] != "WITHIN_BUDGET"]
    compliance_artifact: dict[str, Any] = {
        "schema": COMPLIANCE_SCHEMA,
        "phase": "4BR",
        "budget_hash": budget_artifact["artifact_hash"],
        "rows": compliance,
        "breach_count": len(breaches),
        "hard_deadline_breach_count": sum(
            row["state"] == "HARD_DEADLINE_BREACH" for row in compliance
        ),
        "advancement_allowed": not any(
            row["state"] == "HARD_DEADLINE_BREACH" for row in compliance
        ),
        "configuration_applied": False,
        "execution_authorized": False,
    }
    compliance_artifact["artifact_hash"] = _hash(compliance_artifact)
    return budget_artifact, compliance_artifact


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--budget-input", type=Path, required=True)
    parser.add_argument("--budget-output", type=Path, required=True)
    parser.add_argument("--compliance-output", type=Path, required=True)
    args = parser.parse_args()
    payload = json.loads(args.budget_input.read_text(encoding="utf-8"))
    budget, compliance = build(payload)
    from phase4al_offline_protocol_simulation import publish_pair

    publish_pair(args.budget_output, args.compliance_output, budget, compliance)
    print(json.dumps(budget, sort_keys=True))


if __name__ == "__main__":
    main()
