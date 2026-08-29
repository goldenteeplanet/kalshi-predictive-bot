"""Deterministic stateful sequence fuzzing for the offline replay pipeline."""

from __future__ import annotations

import copy
import hashlib
import json
import random

from scripts.local.phase4nj_reproducibility_bundle import create_bundle, verify_bundle
from scripts.local.phase4nk_portable_replay import canonicalize_artifact

SCHEMA = "phase4no.stateful-sequence.v1"
COMMANDS = (
    "CANONICALIZE",
    "BUNDLE",
    "VERIFY",
    "MUTATE",
    "UNSAFE_MUTATE",
    "REPLAY",
    "MINIMIZE",
    "ARCHIVE",
    "INTERRUPT",
    "RESTART",
    "PARTIAL_VERIFY",
    "STALE_VERIFY",
)
MAX_SEQUENCE_LENGTH = 24


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def generate_sequences(*, seed: int, count: int, maximum_length: int) -> dict[str, object]:
    if count < 1 or maximum_length < 1 or maximum_length > MAX_SEQUENCE_LENGTH:
        return {
            "verdict": "REFUSE",
            "errors": ["SEQUENCE_BOUND_INVALID"],
            "sequences": [],
            "safety": _safety(),
        }
    rng = random.Random(seed)
    templates = [
        ["CANONICALIZE", "BUNDLE", "VERIFY", "REPLAY", "ARCHIVE"],
        ["VERIFY"],
        ["CANONICALIZE", "BUNDLE", "MUTATE", "VERIFY", "MINIMIZE", "ARCHIVE"],
        ["CANONICALIZE", "BUNDLE", "UNSAFE_MUTATE", "VERIFY"],
        ["CANONICALIZE", "INTERRUPT", "RESTART", "BUNDLE", "VERIFY"],
        ["CANONICALIZE", "BUNDLE", "PARTIAL_VERIFY"],
        ["CANONICALIZE", "BUNDLE", "STALE_VERIFY"],
        ["CANONICALIZE", "BUNDLE", "MUTATE", "MUTATE"],
    ]
    sequences = []
    for ordinal in range(count):
        if ordinal < len(templates):
            commands = templates[ordinal][:maximum_length]
        else:
            length = rng.randint(1, maximum_length)
            commands = [rng.choice(COMMANDS) for _ in range(length)]
        body = {"seed": seed, "ordinal": ordinal, "commands": commands}
        sequences.append({**body, "sequence_sha256": _digest(body)})
    result = {
        "schema": SCHEMA,
        "verdict": "PASS",
        "errors": [],
        "seed": seed,
        "count": len(sequences),
        "maximum_length": maximum_length,
        "sequences": sequences,
        "safety": _safety(),
    }
    result["generation_sha256"] = _digest(result)
    return result


def execute_sequence(
    sequence: dict[str, object], records: list[dict[str, object]]
) -> dict[str, object]:
    state = "EMPTY"
    context: dict[str, object] = {"records": copy.deepcopy(records)}
    events = []
    refusal_codes = []
    checkpoint = None
    stale_bundle = None
    for index, command in enumerate(sequence.get("commands", [])):
        before, before_hash = state, _digest(context)
        accepted = True
        code = None
        if command == "CANONICALIZE" and state in {"EMPTY", "CANONICAL"}:
            canonical = canonicalize_artifact(context["records"])
            context["canonical"] = canonical
            state = "CANONICAL" if canonical["verdict"] == "PASS" else "REFUSED"
        elif command == "BUNDLE" and state == "CANONICAL":
            context["bundle"] = create_bundle(context["canonical"]["normalized"], seed=1729)
            stale_bundle = copy.deepcopy(context["bundle"])
            state = "BUNDLED"
        elif command in {"MUTATE", "UNSAFE_MUTATE"} and state in {"BUNDLED", "VERIFIED"}:
            context["bundle"] = copy.deepcopy(context["bundle"])
            if command == "MUTATE":
                context["bundle"]["manifest"]["inputs"]["sha256"] = "0" * 64
            else:
                context["bundle"]["artifacts"]["safety"]["paper_order_creation"] = True
            state = "MUTATED"
        elif command == "VERIFY" and state in {"BUNDLED", "MUTATED"}:
            verification = verify_bundle(context["bundle"])
            context["verification"] = verification
            state = "VERIFIED" if verification["verdict"] == "PASS" else "REFUSED"
            refusal_codes.extend(verification["errors"])
        elif command == "REPLAY" and state == "VERIFIED":
            replay = verify_bundle(context["bundle"])
            context["replay"] = replay
            state = "VERIFIED" if replay["verdict"] == "PASS" else "REFUSED"
        elif command == "MINIMIZE" and state == "REFUSED":
            context["minimal_refusal"] = sorted(set(refusal_codes))[:1]
        elif command == "ARCHIVE" and state in {"VERIFIED", "REFUSED"}:
            context["archive"] = {
                "state": state,
                "context_sha256": _digest(context),
                "provenance": sequence.get("sequence_sha256"),
            }
            state = "ARCHIVED"
        elif command == "INTERRUPT" and state not in {"ARCHIVED", "REFUSED"}:
            checkpoint = {"state": state, "context": copy.deepcopy(context)}
            checkpoint["checkpoint_sha256"] = _digest(checkpoint)
            state = "INTERRUPTED"
        elif command == "RESTART" and state == "INTERRUPTED" and checkpoint is not None:
            expected = checkpoint["checkpoint_sha256"]
            unsigned = {
                key: value for key, value in checkpoint.items() if key != "checkpoint_sha256"
            }
            if expected != _digest(unsigned):
                code, state = "CHECKPOINT_HASH_MISMATCH", "REFUSED"
            else:
                state = checkpoint["state"]
                context = copy.deepcopy(checkpoint["context"])
        elif command == "PARTIAL_VERIFY" and state == "BUNDLED":
            partial = copy.deepcopy(context["bundle"])
            del partial["artifacts"]["inputs"]
            verification = verify_bundle(partial)
            refusal_codes.extend(verification["errors"])
            state = "REFUSED"
        elif command == "STALE_VERIFY" and state == "BUNDLED" and stale_bundle is not None:
            current_hash = context["bundle"]["bundle_sha256"]
            stale_bundle["bundle_sha256"] = "f" * 64
            verification = verify_bundle(stale_bundle)
            refusal_codes.extend(verification["errors"])
            context["stale_reference"] = current_hash
            state = "REFUSED"
        else:
            accepted = False
            code = "ILLEGAL_TRANSITION_REFUSED"
            refusal_codes.append(code)
            state = "REFUSED"
        after_hash = _digest(context)
        events.append(
            {
                "index": index,
                "command": command,
                "state_before": before,
                "state_after": state,
                "accepted": accepted,
                "refusal_code": code,
                "context_sha256_before": before_hash,
                "context_sha256_after": after_hash,
                "previous_event_sha256": events[-1]["event_sha256"] if events else None,
                "event_sha256": "",
            }
        )
        event_body = {key: value for key, value in events[-1].items() if key != "event_sha256"}
        events[-1]["event_sha256"] = _digest(event_body)
    invariant_errors = []
    for index, event in enumerate(events[1:], start=1):
        if event["previous_event_sha256"] != events[index - 1]["event_sha256"]:
            invariant_errors.append("HASH_DISCONTINUITY")
    bundle_safety = context.get("bundle", {}).get("artifacts", {}).get("safety")
    if bundle_safety is not None and bundle_safety != _bundle_safety() and state != "REFUSED":
        invariant_errors.append("UNSAFE_CAPABILITY_STATE_ACCEPTED")
    result = {
        "schema": SCHEMA,
        "verdict": "PASS" if not invariant_errors else "REFUSE",
        "errors": invariant_errors,
        "terminal_state": state,
        "events": events,
        "refusal_codes": sorted(set(refusal_codes)),
        "provenance": {
            "sequence_sha256": sequence.get("sequence_sha256"),
            "seed": sequence.get("seed"),
            "ordinal": sequence.get("ordinal"),
        },
        "safety": _safety(),
    }
    result["execution_sha256"] = _digest(result)
    return result


def fuzz_state_machine(
    records: list[dict[str, object]], *, seed: int, count: int, maximum_length: int
) -> dict[str, object]:
    generation = generate_sequences(seed=seed, count=count, maximum_length=maximum_length)
    if generation["verdict"] != "PASS":
        return _fuzz_result(generation["errors"], [], generation)
    first = [execute_sequence(row, records) for row in generation["sequences"]]
    second = [execute_sequence(row, records) for row in generation["sequences"]]
    errors = []
    if first != second:
        errors.append("NONDETERMINISTIC_SEQUENCE_REPLAY")
    if any(row["verdict"] != "PASS" for row in first):
        errors.append("STATE_MACHINE_INVARIANT_FAILED")
    return _fuzz_result(errors, first, generation)


def minimize_sequence(
    sequence: dict[str, object], records: list[dict[str, object]], target_refusal: str
) -> dict[str, object]:
    commands = list(sequence["commands"])
    changed = True
    while changed:
        changed = False
        for index in range(len(commands)):
            candidate = commands[:index] + commands[index + 1 :]
            body = {"seed": sequence["seed"], "ordinal": sequence["ordinal"], "commands": candidate}
            trial = {**body, "sequence_sha256": _digest(body)}
            if target_refusal in execute_sequence(trial, records)["refusal_codes"]:
                commands = candidate
                changed = True
                break
    result = {
        "target_refusal": target_refusal,
        "minimal_commands": commands,
        "minimal_length": len(commands),
        "original_sequence_sha256": sequence.get("sequence_sha256"),
        "safety": _safety(),
    }
    result["minimization_sha256"] = _digest(result)
    return result


def _fuzz_result(errors, executions, generation):
    result = {
        "schema": SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": errors,
        "sequence_count": len(executions),
        "executions": executions,
        "generation_sha256": generation.get("generation_sha256"),
        "safety": _safety(),
    }
    result["fuzz_sha256"] = _digest(result)
    return result


def _bundle_safety():
    return {
        "offline_only": True,
        "database_session": False,
        "persistence": False,
        "network_access": False,
        "runtime_write": False,
        "service_control": False,
        "paper_order_creation": False,
        "demo_execution": False,
        "live_execution": False,
        "autopilot": False,
    }


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
