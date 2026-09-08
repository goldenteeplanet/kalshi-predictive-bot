from __future__ import annotations

import copy

from scripts.local.phase4no_stateful_sequence_fuzz import COMMANDS
from scripts.local.phase4np_transition_coverage import (
    REQUIRED_REFUSALS,
    REQUIRED_STATES,
    REQUIRED_TRANSITIONS,
    explore_coverage,
    verify_coverage_corpus,
)
from tests.test_phase4mz_adversarial_backtest import _records


def _proof(**kwargs):
    return explore_coverage(_records(), seed=4242, **kwargs)


def test_exploration_is_deterministic_saturated_and_bounded() -> None:
    first = _proof()
    assert first == _proof()
    assert first["verdict"] == "PASS"
    assert first["saturated"] is True
    assert first["corpus_count"] <= 256


def test_required_state_command_transition_refusal_and_checkpoint_coverage() -> None:
    coverage = _proof()["coverage"]
    assert REQUIRED_STATES.issubset(set(coverage["states"]))
    assert set(COMMANDS).issubset(set(coverage["commands"]))
    assert REQUIRED_TRANSITIONS.issubset(set(coverage["transitions"]))
    assert REQUIRED_REFUSALS.issubset(set(coverage["refusal_classes"]))
    assert coverage["checkpoint_restart_covered"] is True
    assert coverage["unsafe_accepted"] == []


def test_retained_corpus_is_locally_minimal_for_coverage_tokens() -> None:
    result = _proof()
    full = result["coverage"]
    for index in range(len(result["corpus"])):
        damaged = copy.deepcopy(result)
        damaged["corpus"].pop(index)
        verification = verify_coverage_corpus(damaged, _records())
        assert verification["verdict"] == "REFUSE"
        assert "COVERAGE_ACCOUNTING_INVALID" in verification["errors"]
    assert full["transition_pairs"]


def test_coverage_regression_and_corpus_bound_fail_closed() -> None:
    regression = _proof(expected_minimum={"transitions": 10_000})
    assert "COVERAGE_REGRESSION" in regression["errors"]
    bounded = _proof(maximum_corpus=1)
    assert "CORPUS_BOUND_EXCEEDED" in bounded["errors"]


def test_invalid_bounds_refuse() -> None:
    assert "EXPLORATION_BOUND_INVALID" in _proof(maximum_rounds=0)["errors"]
    assert "EXPLORATION_BOUND_INVALID" in _proof(maximum_corpus=0)["errors"]


def test_accounting_and_proof_hash_tampering_are_detected() -> None:
    result = _proof()
    assert verify_coverage_corpus(result, _records())["verdict"] == "PASS"
    accounting = copy.deepcopy(result)
    accounting["coverage"]["states"].pop()
    assert "COVERAGE_ACCOUNTING_INVALID" in verify_coverage_corpus(accounting, _records())["errors"]
    envelope = copy.deepcopy(result)
    envelope["proof_sha256"] = "0" * 64
    assert "COVERAGE_CORPUS_HASH_MISMATCH" in verify_coverage_corpus(envelope, _records())["errors"]


def test_transition_coverage_has_no_execution_capability() -> None:
    safety = _proof()["safety"]
    assert safety["offline_only"] is True
    assert all(value is False for key, value in safety.items() if key != "offline_only")
