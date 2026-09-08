"""Declarative transition oracle and semantic comparison for Phase 4NO."""

from __future__ import annotations

import hashlib
import json

from scripts.local.phase4no_stateful_sequence_fuzz import COMMANDS, execute_sequence

SCHEMA = "phase4nq.transition-oracle.v1"
STATES = {
    "EMPTY",
    "CANONICAL",
    "BUNDLED",
    "MUTATED",
    "VERIFIED",
    "REFUSED",
    "INTERRUPTED",
    "ARCHIVED",
}
RULES = (
    {
        "id": "canonicalize",
        "command": "CANONICALIZE",
        "from": ("EMPTY", "CANONICAL"),
        "to": "CANONICAL",
    },
    {"id": "bundle", "command": "BUNDLE", "from": ("CANONICAL",), "to": "BUNDLED"},
    {"id": "verify-clean", "command": "VERIFY", "from": ("BUNDLED",), "to": "VERIFIED"},
    {"id": "verify-mutated", "command": "VERIFY", "from": ("MUTATED",), "to": "REFUSED"},
    {"id": "mutate", "command": "MUTATE", "from": ("BUNDLED", "VERIFIED"), "to": "MUTATED"},
    {
        "id": "unsafe-mutate",
        "command": "UNSAFE_MUTATE",
        "from": ("BUNDLED", "VERIFIED"),
        "to": "MUTATED",
    },
    {"id": "replay", "command": "REPLAY", "from": ("VERIFIED",), "to": "VERIFIED"},
    {"id": "minimize", "command": "MINIMIZE", "from": ("REFUSED",), "to": "REFUSED"},
    {"id": "archive", "command": "ARCHIVE", "from": ("VERIFIED", "REFUSED"), "to": "ARCHIVED"},
    {
        "id": "interrupt",
        "command": "INTERRUPT",
        "from": ("EMPTY", "CANONICAL", "BUNDLED", "MUTATED", "VERIFIED", "INTERRUPTED"),
        "to": "INTERRUPTED",
    },
    {"id": "restart", "command": "RESTART", "from": ("INTERRUPTED",), "to": "$CHECKPOINT"},
    {"id": "partial", "command": "PARTIAL_VERIFY", "from": ("BUNDLED",), "to": "REFUSED"},
    {"id": "stale", "command": "STALE_VERIFY", "from": ("BUNDLED",), "to": "REFUSED"},
)


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def audit_rules(rules: tuple[dict[str, object], ...] = RULES) -> dict[str, object]:
    errors = []
    ids = [rule.get("id") for rule in rules]
    if len(ids) != len(set(ids)):
        errors.append("DUPLICATE_ORACLE_RULE")
    if set(COMMANDS) - {rule.get("command") for rule in rules}:
        errors.append("MISSING_ORACLE_RULE")
    occupied = set()
    for rule in rules:
        sources = set(rule.get("from", ()))
        if not sources or not sources.issubset(STATES):
            errors.append("UNREACHABLE_ORACLE_RULE")
        target = rule.get("to")
        if target not in STATES | {"$CHECKPOINT"}:
            errors.append("UNREACHABLE_ORACLE_RULE")
        for state in sources:
            key = (rule.get("command"), state)
            if key in occupied:
                errors.append("AMBIGUOUS_ORACLE_GUARD")
            occupied.add(key)
    result = {"verdict": "PASS" if not errors else "REFUSE", "errors": sorted(set(errors))}
    result["rules_sha256"] = _digest(rules)
    return result


def oracle_execute(
    sequence: dict[str, object], rules: tuple[dict[str, object], ...] = RULES
) -> dict[str, object]:
    audit = audit_rules(rules)
    if audit["verdict"] != "PASS":
        return {"verdict": "REFUSE", "errors": audit["errors"], "trace": []}
    state = "EMPTY"
    checkpoint_state = None
    mutation_kind = None
    trace = []
    refusals = []
    for index, command in enumerate(sequence.get("commands", [])):
        before = state
        matches = [rule for rule in rules if rule["command"] == command and before in rule["from"]]
        if len(matches) != 1:
            accepted, state, rule_id = False, "REFUSED", None
            refusals.append("ILLEGAL_TRANSITION_REFUSED")
        else:
            accepted, rule_id = True, matches[0]["id"]
            target = matches[0]["to"]
            if command == "INTERRUPT":
                checkpoint_state = before
            state = checkpoint_state if target == "$CHECKPOINT" else target
            if command in {"MUTATE", "UNSAFE_MUTATE"}:
                mutation_kind = command
            elif command == "VERIFY" and before == "MUTATED":
                refusals.extend(("ARTIFACT_HASH_MISMATCH", "BUNDLE_HASH_MISMATCH"))
                if mutation_kind == "UNSAFE_MUTATE":
                    refusals.append("SAFETY_INVARIANT_VIOLATION")
            elif command == "PARTIAL_VERIFY":
                refusals.extend(("REQUIRED_ARTIFACT_MISSING", "BUNDLE_HASH_MISMATCH"))
            elif command == "STALE_VERIFY":
                refusals.append("BUNDLE_HASH_MISMATCH")
        trace.append(
            {
                "index": index,
                "command": command,
                "state_before": before,
                "state_after": state,
                "accepted": accepted,
                "rule_id": rule_id,
            }
        )
    result = {
        "verdict": "PASS",
        "errors": [],
        "trace": trace,
        "terminal_state": state,
        "refusal_codes": sorted(set(refusals)),
        "provenance": {
            "sequence_sha256": sequence.get("sequence_sha256"),
            "seed": sequence.get("seed"),
            "ordinal": sequence.get("ordinal"),
        },
    }
    result["oracle_sha256"] = _digest(result)
    return result


def _implementation_trace(execution: dict[str, object]) -> list[dict[str, object]]:
    return [
        {
            "index": event["index"],
            "command": event["command"],
            "state_before": event["state_before"],
            "state_after": event["state_after"],
            "accepted": event["accepted"],
        }
        for event in execution["events"]
    ]


def compare_sequence(
    sequence: dict[str, object],
    records: list[dict[str, object]],
    *,
    rules: tuple[dict[str, object], ...] = RULES,
) -> dict[str, object]:
    implementation = execute_sequence(sequence, records)
    oracle = oracle_execute(sequence, rules)
    errors = []
    oracle_trace = [
        {key: value for key, value in row.items() if key != "rule_id"}
        for row in oracle.get("trace", [])
    ]
    if _implementation_trace(implementation) != oracle_trace:
        errors.append("IMPLEMENTATION_ORACLE_DIVERGENCE")
    if implementation["terminal_state"] != oracle.get("terminal_state"):
        errors.append("TERMINAL_STATE_MISMATCH")
    if implementation["refusal_codes"] != oracle.get("refusal_codes"):
        errors.append("REFUSAL_CODE_MISMATCH")
    if implementation["provenance"] != oracle.get("provenance"):
        errors.append("PROVENANCE_LINK_MISMATCH")
    for index, event in enumerate(implementation["events"]):
        if (
            index
            and event["previous_event_sha256"]
            != implementation["events"][index - 1]["event_sha256"]
        ):
            errors.append("HASH_CHAIN_DISCONTINUITY")
    if "UNSAFE_MUTATE" in sequence.get("commands", []) and implementation["terminal_state"] not in {
        "REFUSED",
        "MUTATED",
    }:
        errors.append("UNSAFE_STATE_ACCEPTED")
    result = {
        "schema": SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": sorted(set(errors)),
        "sequence_sha256": sequence.get("sequence_sha256"),
        "implementation_sha256": implementation["execution_sha256"],
        "oracle_sha256": oracle.get("oracle_sha256"),
        "safety": _safety(),
    }
    result["comparison_sha256"] = _digest(result)
    return result


def run_oracle_proof(
    sequences: list[dict[str, object]],
    records: list[dict[str, object]],
    *,
    rules: tuple[dict[str, object], ...] = RULES,
) -> dict[str, object]:
    audit = audit_rules(rules)
    comparisons = [compare_sequence(row, records, rules=rules) for row in sequences]
    errors = list(audit["errors"])
    if any(row["verdict"] != "PASS" for row in comparisons):
        errors.append("ORACLE_COMPARISON_FAILED")
    result = {
        "schema": SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": sorted(set(errors)),
        "sequence_count": len(sequences),
        "rules_sha256": audit["rules_sha256"],
        "comparisons": comparisons,
        "safety": _safety(),
    }
    result["proof_sha256"] = _digest(result)
    return result


def _safety():
    return {
        "offline_only": True,
        "persistence": False,
        "network_access": False,
        "runtime_write": False,
        "paper_order_creation": False,
        "demo_execution": False,
        "live_execution": False,
        "autopilot": False,
    }
