"""Exercise the synthetic paper-eligibility pipeline under a strict air-gap contract."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

INPUT_SCHEMA = "phase4ew.acceptance-input.v1"
REPORT_SCHEMA = "phase4ew.acceptance-report.v1"
GATES = (
    "market_data_fresh",
    "forecast_valid",
    "ranking_eligible",
    "risk_eligible",
    "conflict_free",
    "operator_approved",
    "intent_unexpired",
    "intent_unique",
    "routing_simulation_passed",
)
REFUSAL_CODES = {
    "market_data_fresh": "STALE_MARKET_DATA",
    "forecast_valid": "FORECAST_INVALID",
    "ranking_eligible": "RANKING_INELIGIBLE",
    "risk_eligible": "RISK_BLOCKED",
    "conflict_free": "CANDIDATE_CONFLICT",
    "operator_approved": "OPERATOR_APPROVAL_MISSING",
    "intent_unexpired": "INTENT_EXPIRED",
    "intent_unique": "DUPLICATE_INTENT",
    "routing_simulation_passed": "ROUTING_SIMULATION_REFUSED",
}
REQUIRED_SCENARIOS = ("SUCCESS", *tuple(REFUSAL_CODES.values()))


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


def _evaluate(gates: dict[str, bool]) -> dict[str, Any]:
    for position, name in enumerate(GATES, start=1):
        if not gates[name]:
            return {
                "outcome": "REFUSED",
                "reason_codes": [REFUSAL_CODES[name]],
                "stopped_at_gate": name,
                "gates_passed": position - 1,
                "creation_boundary_reached": False,
            }
    return {
        "outcome": "CREATION_BOUNDARY_REACHED_NOT_CROSSED",
        "reason_codes": [],
        "stopped_at_gate": None,
        "gates_passed": len(GATES),
        "creation_boundary_reached": True,
    }


def build_report(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != {
        "schema",
        "air_gap",
        "scenarios",
        "artifact_hash",
    }:
        raise ValueError("PHASE4EW_INPUT_FIELDS_INVALID")
    if payload.get("schema") != INPUT_SCHEMA or payload.get("artifact_hash") != _hash(payload):
        raise ValueError("PHASE4EW_INPUT_SCHEMA_OR_HASH_INVALID")
    air_gap = payload["air_gap"]
    if not isinstance(air_gap, dict) or set(air_gap) != {
        "network_enabled",
        "services_available",
        "credentials_present",
        "order_creation_enabled",
        "database_mode",
        "database_identity_hash",
    }:
        raise ValueError("PHASE4EW_AIR_GAP_FIELDS_INVALID")
    for field in (
        "network_enabled",
        "services_available",
        "credentials_present",
        "order_creation_enabled",
    ):
        if not isinstance(air_gap[field], bool):
            raise ValueError("PHASE4EW_AIR_GAP_STATUS_INVALID")
    if any(
        air_gap[field]
        for field in (
            "network_enabled",
            "services_available",
            "credentials_present",
            "order_creation_enabled",
        )
    ):
        raise ValueError("PHASE4EW_AIR_GAP_NOT_ENFORCED")
    if air_gap["database_mode"] != "DISPOSABLE_SYNTHETIC":
        raise ValueError("PHASE4EW_DATABASE_MODE_INVALID")
    database_hash = _digest(air_gap["database_identity_hash"], "PHASE4EW_DATABASE_IDENTITY_INVALID")

    scenarios = payload["scenarios"]
    if not isinstance(scenarios, list) or len(scenarios) != len(REQUIRED_SCENARIOS):
        raise ValueError("PHASE4EW_SCENARIO_SET_INVALID")
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    for scenario in scenarios:
        if not isinstance(scenario, dict) or set(scenario) != {
            "scenario_id",
            "gates",
            "expected",
            "fixture_hash",
        }:
            raise ValueError("PHASE4EW_SCENARIO_FIELDS_INVALID")
        scenario_id = scenario["scenario_id"]
        if scenario_id not in REQUIRED_SCENARIOS or scenario_id in seen:
            raise ValueError("PHASE4EW_SCENARIO_ID_INVALID")
        seen.add(scenario_id)
        gates = scenario["gates"]
        if (
            not isinstance(gates, dict)
            or set(gates) != set(GATES)
            or any(not isinstance(value, bool) for value in gates.values())
        ):
            raise ValueError("PHASE4EW_GATES_INVALID")
        fixture_body = {
            "scenario_id": scenario_id,
            "gates": gates,
            "expected": scenario["expected"],
        }
        if scenario["fixture_hash"] != canonical_hash(fixture_body):
            raise ValueError("PHASE4EW_FIXTURE_HASH_INVALID")
        false_gates = [name for name in GATES if not gates[name]]
        if scenario_id == "SUCCESS":
            if false_gates:
                raise ValueError("PHASE4EW_SUCCESS_FIXTURE_INVALID")
        else:
            expected_gate = next(
                name for name, code in REFUSAL_CODES.items() if code == scenario_id
            )
            if false_gates != [expected_gate]:
                raise ValueError("PHASE4EW_REFUSAL_FIXTURE_NOT_ISOLATED")
        expected = scenario["expected"]
        if not isinstance(expected, dict) or set(expected) != {"outcome", "reason_codes"}:
            raise ValueError("PHASE4EW_EXPECTED_FIELDS_INVALID")
        if not isinstance(expected["reason_codes"], list):
            raise ValueError("PHASE4EW_EXPECTED_REASONS_INVALID")
        actual = _evaluate(gates)
        matched = expected == {"outcome": actual["outcome"], "reason_codes": actual["reason_codes"]}
        results.append(
            {
                "scenario_id": scenario_id,
                "fixture_hash": scenario["fixture_hash"],
                "actual": actual,
                "expected_matched": matched,
                "acceptance_reason_codes": [] if matched else ["EXPECTED_OUTCOME_MISMATCH"],
            }
        )
    if seen != set(REQUIRED_SCENARIOS):
        raise ValueError("PHASE4EW_SCENARIO_SET_INVALID")
    results.sort(key=lambda item: REQUIRED_SCENARIOS.index(item["scenario_id"]))
    accepted = all(result["expected_matched"] for result in results)
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "phase": "4EW",
        "input_hash": payload["artifact_hash"],
        "database_identity_hash": database_hash,
        "air_gap_enforced": True,
        "required_scenarios": list(REQUIRED_SCENARIOS),
        "scenario_results": results,
        "scenario_count": len(results),
        "all_refusal_classes_covered": seen == set(REQUIRED_SCENARIOS),
        "acceptance_passed": accepted,
        "paper_order_creation_boundary_crossed": False,
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
