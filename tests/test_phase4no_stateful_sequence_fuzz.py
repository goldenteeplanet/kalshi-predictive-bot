from __future__ import annotations

from scripts.local.phase4no_stateful_sequence_fuzz import (
    MAX_SEQUENCE_LENGTH,
    execute_sequence,
    fuzz_state_machine,
    generate_sequences,
    minimize_sequence,
)
from tests.test_phase4mz_adversarial_backtest import _records


def _sequence(commands):
    import hashlib
    import json

    body = {"seed": 7, "ordinal": 0, "commands": commands}
    return {
        **body,
        "sequence_sha256": hashlib.sha256(
            json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    }


def test_generation_is_seeded_bounded_deterministic_and_diverse() -> None:
    first = generate_sequences(seed=73, count=64, maximum_length=12)
    assert first == generate_sequences(seed=73, count=64, maximum_length=12)
    assert first != generate_sequences(seed=74, count=64, maximum_length=12)
    assert len(first["sequences"]) == 64
    assert max(len(row["commands"]) for row in first["sequences"]) <= 12


def test_invalid_generation_bounds_refuse() -> None:
    for maximum in (0, MAX_SEQUENCE_LENGTH + 1):
        result = generate_sequences(seed=1, count=1, maximum_length=maximum)
        assert result["verdict"] == "REFUSE"
        assert "SEQUENCE_BOUND_INVALID" in result["errors"]


def test_legal_verify_replay_archive_path_preserves_hash_chain_and_provenance() -> None:
    sequence = _sequence(["CANONICALIZE", "BUNDLE", "VERIFY", "REPLAY", "ARCHIVE"])
    result = execute_sequence(sequence, _records())
    assert result["verdict"] == "PASS"
    assert result["terminal_state"] == "ARCHIVED"
    assert result["provenance"]["sequence_sha256"] == sequence["sequence_sha256"]
    assert all(
        event["previous_event_sha256"] == result["events"][index - 1]["event_sha256"]
        for index, event in enumerate(result["events"])
        if index
    )


def test_invalid_order_partial_stale_duplicate_and_unsafe_sequences_fail_closed() -> None:
    cases = (
        (["VERIFY"], "ILLEGAL_TRANSITION_REFUSED"),
        (["CANONICALIZE", "BUNDLE", "PARTIAL_VERIFY"], "REQUIRED_ARTIFACT_MISSING"),
        (["CANONICALIZE", "BUNDLE", "STALE_VERIFY"], "BUNDLE_HASH_MISMATCH"),
        (["CANONICALIZE", "BUNDLE", "MUTATE", "MUTATE"], "ILLEGAL_TRANSITION_REFUSED"),
        (["CANONICALIZE", "BUNDLE", "UNSAFE_MUTATE", "VERIFY"], "SAFETY_INVARIANT_VIOLATION"),
    )
    for commands, refusal in cases:
        result = execute_sequence(_sequence(commands), _records())
        assert result["verdict"] == "PASS"
        assert result["terminal_state"] == "REFUSED"
        assert refusal in result["refusal_codes"]


def test_interruption_restart_restores_checkpoint_deterministically() -> None:
    sequence = _sequence(["CANONICALIZE", "INTERRUPT", "RESTART", "BUNDLE", "VERIFY"])
    first = execute_sequence(sequence, _records())
    assert first == execute_sequence(sequence, _records())
    assert first["terminal_state"] == "VERIFIED"


def test_seeded_fuzz_replays_twice_without_state_divergence() -> None:
    result = fuzz_state_machine(_records(), seed=991, count=100, maximum_length=16)
    assert result["verdict"] == "PASS"
    assert result["sequence_count"] == 100


def test_failure_minimization_retains_causal_refusal() -> None:
    sequence = _sequence(["CANONICALIZE", "CANONICALIZE", "BUNDLE", "MUTATE", "VERIFY", "MINIMIZE"])
    result = minimize_sequence(sequence, _records(), "ARTIFACT_HASH_MISMATCH")
    assert result["minimal_length"] < len(sequence["commands"])
    minimized = _sequence(result["minimal_commands"])
    assert "ARTIFACT_HASH_MISMATCH" in execute_sequence(minimized, _records())["refusal_codes"]


def test_state_machine_has_no_execution_capability() -> None:
    safety = fuzz_state_machine(_records(), seed=1, count=8, maximum_length=8)["safety"]
    assert safety["offline_only"] is True
    assert all(value is False for key, value in safety.items() if key != "offline_only")
