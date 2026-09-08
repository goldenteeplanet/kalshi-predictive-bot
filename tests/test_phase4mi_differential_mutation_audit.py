from __future__ import annotations

import copy

from scripts.local.phase4mi_differential_mutation_audit import (
    MAX_MUTATIONS,
    minimize_suffix_counterexample,
    run_mutation_audit,
)
from tests.test_phase4me_nonce_consumption_ledger import _committed


def test_complete_bounded_corpus_detects_every_mutation_deterministically() -> None:
    first = run_mutation_audit(_committed(), evaluated_at="2026-08-28T20:10:00Z")
    second = run_mutation_audit(_committed(), evaluated_at="2026-08-28T20:10:00Z")
    assert first == second
    assert first["verdict"] == "PASS"
    assert first["mutation_count"] == first["detected_count"]
    assert first["silent_mutation_ids"] == []
    assert first["mutation_count"] <= MAX_MUTATIONS


def test_every_required_surface_has_coverage() -> None:
    coverage = run_mutation_audit(_committed(), evaluated_at="2026-08-28T20:10:00Z")[
        "surface_coverage"
    ]
    assert set(coverage) == {
        "authoritative_record",
        "authoritative_record_semantics",
        "migration_artifact_binding",
        "retained_suffix",
        "snapshot_binding",
    }
    assert all(count > 0 for count in coverage.values())


def test_minimizer_preserves_failure_signature_and_does_not_mutate_input() -> None:
    records = _committed()
    suffix = copy.deepcopy(records)
    suffix[0]["record_sha256"] = "0" * 64
    before = copy.deepcopy(suffix)
    result = minimize_suffix_counterexample(
        records,
        cut_point=0,
        supplied_suffix=suffix,
        evaluated_at="2026-08-28T20:10:00Z",
    )
    assert result["failure_signature"].startswith("RESTORATION:")
    assert result["minimized_count"] <= result["original_count"]
    assert suffix == before


def test_invalid_history_and_invalid_bound_fail_closed() -> None:
    bad = _committed()
    bad[0]["record_sha256"] = "0" * 64
    assert run_mutation_audit(bad, evaluated_at="2026-08-28T20:10:00Z")["verdict"] == "REFUSE"
    assert (
        run_mutation_audit(
            _committed(), evaluated_at="2026-08-28T20:10:00Z", max_mutations=MAX_MUTATIONS + 1
        )["verdict"]
        == "REFUSE"
    )


def test_auditor_has_no_write_runtime_or_order_capability() -> None:
    safety = run_mutation_audit(_committed(), evaluated_at="2026-08-28T20:10:00Z")["safety"]
    assert safety["bounded"] is True
    assert safety["read_only"] is True
    assert all(
        value is False for key, value in safety.items() if key not in {"bounded", "read_only"}
    )
