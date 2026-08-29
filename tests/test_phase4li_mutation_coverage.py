from __future__ import annotations

from scripts.local.phase4lh_fuzz_corpus import MUTATIONS
from scripts.local.phase4li_mutation_coverage import (
    JUSTIFIED_UNREACHABLE,
    _boundary_results,
    discover_refusal_literals,
    run_coverage_audit,
)


def test_complete_audit_passes_deterministically() -> None:
    first = run_coverage_audit()
    second = run_coverage_audit()
    assert first == second
    assert first["verdict"] == "PASS"


def test_all_mutation_kinds_are_covered() -> None:
    coverage = run_coverage_audit()["mutation_coverage"]
    assert coverage == {"covered": len(MUTATIONS), "required": len(MUTATIONS)}


def test_every_discovered_branch_is_covered_or_justified() -> None:
    report = run_coverage_audit()
    covered = {row["branch"] for row in report["probe_results"]}
    assert discover_refusal_literals() <= covered | set(JUSTIFIED_UNREACHABLE)


def test_every_probe_reaches_its_intended_signature() -> None:
    assert all(row["matched"] for row in run_coverage_audit()["probe_results"])


def test_every_exact_boundary_passes_then_refuses() -> None:
    assert all(
        row["at_limit"] == "PASS" and row["over_limit"] == "REFUSE" for row in _boundary_results()
    )


def test_minimizer_coverage_is_passing() -> None:
    assert all(row["verdict"] == "PASS" for row in run_coverage_audit()["minimizer_results"])


def test_corpus_seed_and_hash_are_stable() -> None:
    report = run_coverage_audit(4108)
    assert report["seed"] == 4108
    assert len(report["corpus_sha256"]) == 64


def test_unreachable_branches_have_specific_rationales() -> None:
    assert all(reason and len(reason) > 20 for reason in JUSTIFIED_UNREACHABLE.values())
