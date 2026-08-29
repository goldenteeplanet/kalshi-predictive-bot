"""Coverage-guided exploration and saturation proof for Phase 4NO."""

from __future__ import annotations

import hashlib
import json
import random

from scripts.local.phase4no_stateful_sequence_fuzz import (
    COMMANDS,
    MAX_SEQUENCE_LENGTH,
    execute_sequence,
    generate_sequences,
)

SCHEMA = "phase4np.transition-coverage.v1"
REQUIRED_STATES = {
    "EMPTY",
    "CANONICAL",
    "BUNDLED",
    "MUTATED",
    "VERIFIED",
    "REFUSED",
    "INTERRUPTED",
    "ARCHIVED",
}
REQUIRED_REFUSALS = {
    "ILLEGAL_TRANSITION_REFUSED",
    "REQUIRED_ARTIFACT_MISSING",
    "BUNDLE_HASH_MISMATCH",
    "ARTIFACT_HASH_MISMATCH",
    "SAFETY_INVARIANT_VIOLATION",
}
REQUIRED_TRANSITIONS = {
    "EMPTY|CANONICALIZE|CANONICAL",
    "CANONICAL|BUNDLE|BUNDLED",
    "BUNDLED|VERIFY|VERIFIED",
    "VERIFIED|REPLAY|VERIFIED",
    "VERIFIED|ARCHIVE|ARCHIVED",
    "BUNDLED|MUTATE|MUTATED",
    "MUTATED|VERIFY|REFUSED",
    "REFUSED|MINIMIZE|REFUSED",
    "REFUSED|ARCHIVE|ARCHIVED",
    "BUNDLED|UNSAFE_MUTATE|MUTATED",
    "CANONICAL|INTERRUPT|INTERRUPTED",
    "INTERRUPTED|RESTART|CANONICAL",
    "BUNDLED|PARTIAL_VERIFY|REFUSED",
    "BUNDLED|STALE_VERIFY|REFUSED",
    "MUTATED|MUTATE|REFUSED",
    "EMPTY|VERIFY|REFUSED",
}


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def coverage_of(executions: list[dict[str, object]]) -> dict[str, object]:
    states, commands, transitions, pairs, refusals, terminals = (
        set(),
        set(),
        set(),
        set(),
        set(),
        set(),
    )
    checkpoint_restart = False
    unsafe_accepted = []
    for execution in executions:
        terminals.add(execution["terminal_state"])
        refusals.update(execution["refusal_codes"])
        local = []
        for event in execution["events"]:
            states.update((event["state_before"], event["state_after"]))
            commands.add(event["command"])
            transition = f"{event['state_before']}|{event['command']}|{event['state_after']}"
            transitions.add(transition)
            local.append(transition)
            if event["command"] == "RESTART" and event["state_before"] == "INTERRUPTED":
                checkpoint_restart = True
        pairs.update(f"{left}>{right}" for left, right in zip(local, local[1:], strict=False))
        if (
            "SAFETY_INVARIANT_VIOLATION" in execution["refusal_codes"]
            and execution["terminal_state"] != "REFUSED"
        ):
            unsafe_accepted.append(execution["execution_sha256"])
    result = {
        "states": sorted(states),
        "commands": sorted(commands),
        "transitions": sorted(transitions),
        "transition_pairs": sorted(pairs),
        "refusal_classes": sorted(refusals),
        "terminal_states": sorted(terminals),
        "checkpoint_restart_covered": checkpoint_restart,
        "unsafe_accepted": unsafe_accepted,
    }
    result["coverage_sha256"] = _digest(result)
    return result


def _tokens(coverage: dict[str, object]) -> set[str]:
    output = set()
    for field in (
        "states",
        "commands",
        "transitions",
        "transition_pairs",
        "refusal_classes",
        "terminal_states",
    ):
        output.update(f"{field}:{value}" for value in coverage[field])
    if coverage["checkpoint_restart_covered"]:
        output.add("checkpoint:restart")
    return output


def _guide_tokens(coverage: dict[str, object]) -> set[str]:
    """Declared finite frontier; incidental transitions and pairs remain measured."""
    output = {f"states:{value}" for value in set(coverage["states"]) & REQUIRED_STATES}
    output.update(f"commands:{value}" for value in coverage["commands"] if value in COMMANDS)
    output.update(
        f"transitions:{value}" for value in set(coverage["transitions"]) & REQUIRED_TRANSITIONS
    )
    output.update(
        f"refusal_classes:{value}" for value in set(coverage["refusal_classes"]) & REQUIRED_REFUSALS
    )
    output.update(
        f"terminal_states:{value}"
        for value in set(coverage["terminal_states"])
        & {"ARCHIVED", "VERIFIED", "REFUSED", "CANONICAL", "INTERRUPTED"}
    )
    if coverage["checkpoint_restart_covered"]:
        output.add("checkpoint:restart")
    return output


def _mutate(sequence: dict[str, object], *, seed: int, round_index: int) -> dict[str, object]:
    rng = random.Random(seed + sequence["ordinal"] * 101 + round_index * 1009)
    commands = list(sequence["commands"])
    operation = rng.choice(("append", "replace", "duplicate"))
    if operation == "append" and len(commands) < MAX_SEQUENCE_LENGTH:
        commands.append(rng.choice(COMMANDS))
    elif operation == "replace" and commands:
        commands[rng.randrange(len(commands))] = rng.choice(COMMANDS)
    elif commands and len(commands) < MAX_SEQUENCE_LENGTH:
        index = rng.randrange(len(commands))
        commands.insert(index, commands[index])
    body = {"seed": seed, "ordinal": sequence["ordinal"], "commands": commands}
    return {**body, "sequence_sha256": _digest(body), "parent_sha256": sequence["sequence_sha256"]}


def explore_coverage(
    records: list[dict[str, object]],
    *,
    seed: int,
    initial_count: int = 64,
    maximum_length: int = 16,
    maximum_rounds: int = 6,
    maximum_corpus: int = 256,
    expected_minimum: dict[str, int] | None = None,
) -> dict[str, object]:
    generated = generate_sequences(seed=seed, count=initial_count, maximum_length=maximum_length)
    if generated["verdict"] != "PASS" or maximum_corpus < 1 or maximum_rounds < 1:
        return _result(["EXPLORATION_BOUND_INVALID"], [], {}, 0, False)
    candidates = list(generated["sequences"])
    retained, executions, known = [], [], set()
    stagnant = 0
    rounds = 0
    while rounds < maximum_rounds and stagnant < 2:
        additions = 0
        for sequence in candidates:
            execution = execute_sequence(sequence, records)
            contribution = _guide_tokens(coverage_of([execution])) - known
            if contribution:
                retained.append({**sequence, "contribution": sorted(contribution)})
                executions.append(execution)
                known.update(contribution)
                additions += 1
                if len(retained) > maximum_corpus:
                    return _result(
                        ["CORPUS_BOUND_EXCEEDED"], retained, coverage_of(executions), rounds, False
                    )
        stagnant = stagnant + 1 if additions == 0 else 0
        rounds += 1
        candidates = [_mutate(row, seed=seed, round_index=rounds) for row in retained]
    saturated = stagnant >= 2
    coverage = coverage_of(executions)
    retained, executions = _prune(retained, executions)
    coverage = coverage_of(executions)
    errors = []
    if not saturated:
        errors.append("SATURATION_NOT_REACHED")
    if REQUIRED_STATES - set(coverage["states"]):
        errors.append("REQUIRED_STATE_UNREACHABLE")
    if set(COMMANDS) - set(coverage["commands"]):
        errors.append("REQUIRED_COMMAND_UNCOVERED")
    if REQUIRED_TRANSITIONS - set(coverage["transitions"]):
        errors.append("REQUIRED_TRANSITION_UNREACHABLE")
    if REQUIRED_REFUSALS - set(coverage["refusal_classes"]):
        errors.append("REQUIRED_REFUSAL_MISSING")
    if not coverage["checkpoint_restart_covered"]:
        errors.append("CHECKPOINT_RESTART_UNCOVERED")
    if coverage["unsafe_accepted"]:
        errors.append("UNSAFE_STATE_ACCEPTED")
    for field, minimum in (expected_minimum or {}).items():
        value = len(coverage.get(field, []))
        if value < minimum:
            errors.append("COVERAGE_REGRESSION")
    return _result(sorted(set(errors)), retained, coverage, rounds, saturated)


def _prune(sequences, executions):
    index = len(sequences) - 1
    while index >= 0:
        candidate_executions = executions[:index] + executions[index + 1 :]
        if _tokens(coverage_of(candidate_executions)) == _tokens(coverage_of(executions)):
            sequences.pop(index)
            executions.pop(index)
        index -= 1
    return sequences, executions


def verify_coverage_corpus(result: object, records: list[dict[str, object]]) -> dict[str, object]:
    errors = []
    if not isinstance(result, dict) or result.get("schema") != SCHEMA:
        return {"verdict": "REFUSE", "errors": ["CORPUS_SHAPE_INVALID"], "safety": _safety()}
    executions = [execute_sequence(row, records) for row in result.get("corpus", [])]
    coverage = coverage_of(executions)
    if coverage != result.get("coverage"):
        errors.append("COVERAGE_ACCOUNTING_INVALID")
    unsigned = {key: value for key, value in result.items() if key != "proof_sha256"}
    if result.get("proof_sha256") != _digest(unsigned):
        errors.append("COVERAGE_CORPUS_HASH_MISMATCH")
    return {"verdict": "PASS" if not errors else "REFUSE", "errors": errors, "safety": _safety()}


def _result(errors, corpus, coverage, rounds, saturated):
    result = {
        "schema": SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": errors,
        "rounds": rounds,
        "saturated": saturated,
        "corpus_count": len(corpus),
        "corpus": corpus,
        "coverage": coverage,
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
