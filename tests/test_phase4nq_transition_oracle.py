from __future__ import annotations

import copy

from scripts.local.phase4no_stateful_sequence_fuzz import generate_sequences
from scripts.local.phase4np_transition_coverage import explore_coverage
from scripts.local.phase4nq_transition_oracle import (
    RULES,
    audit_rules,
    compare_sequence,
    run_oracle_proof,
)
from tests.test_phase4mz_adversarial_backtest import _records


def _sequences():
    coverage = explore_coverage(_records(), seed=4242)
    extra = generate_sequences(seed=8181, count=48, maximum_length=16)["sequences"]
    return coverage["corpus"] + extra


def test_declarative_rules_are_complete_unambiguous_and_reachable() -> None:
    audit = audit_rules()
    assert audit["verdict"] == "PASS"
    assert len(RULES) == 13


def test_oracle_agrees_with_minimized_corpus_and_generated_sequences() -> None:
    result = run_oracle_proof(_sequences(), _records())
    assert result["verdict"] == "PASS"
    assert result["sequence_count"] == 57
    assert all(row["verdict"] == "PASS" for row in result["comparisons"])


def test_checkpoint_refusal_terminal_and_provenance_semantics_agree() -> None:
    for sequence in _sequences():
        comparison = compare_sequence(sequence, _records())
        assert comparison["verdict"] == "PASS", sequence["commands"]
        assert comparison["sequence_sha256"] == sequence["sequence_sha256"]


def test_missing_overlapping_and_unreachable_rules_refuse() -> None:
    missing = tuple(rule for rule in RULES if rule["command"] != "ARCHIVE")
    assert "MISSING_ORACLE_RULE" in audit_rules(missing)["errors"]
    overlap = RULES + (
        {"id": "overlap", "command": "BUNDLE", "from": ("CANONICAL",), "to": "BUNDLED"},
    )
    assert "AMBIGUOUS_ORACLE_GUARD" in audit_rules(overlap)["errors"]
    unreachable = RULES + (
        {"id": "bad", "command": "VERIFY", "from": ("UNKNOWN",), "to": "VERIFIED"},
    )
    assert "UNREACHABLE_ORACLE_RULE" in audit_rules(unreachable)["errors"]


def test_oracle_transition_or_refusal_drift_is_detected() -> None:
    drifted = list(copy.deepcopy(RULES))
    rule = next(row for row in drifted if row["id"] == "verify-clean")
    rule["to"] = "REFUSED"
    result = run_oracle_proof(_sequences()[:12], _records(), rules=tuple(drifted))
    assert result["verdict"] == "REFUSE"
    assert "ORACLE_COMPARISON_FAILED" in result["errors"]


def test_oracle_proof_is_deterministic() -> None:
    sequences = _sequences()
    assert run_oracle_proof(sequences, _records()) == run_oracle_proof(sequences, _records())


def test_oracle_has_no_execution_capability() -> None:
    safety = run_oracle_proof(_sequences()[:4], _records())["safety"]
    assert safety["offline_only"] is True
    assert all(value is False for key, value in safety.items() if key != "offline_only")
