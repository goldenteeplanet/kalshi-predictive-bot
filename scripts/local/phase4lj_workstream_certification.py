"""Git-backed final certification for the Phase 4KX-4LI evidence workstream."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

SCHEMA = "phase4lj.evidence-safety-workstream-certification.v1"


@dataclass(frozen=True)
class PhaseSpec:
    phase: str
    subject: str
    files: tuple[str, ...]


def _spec(phase: str, slug: str, script: str, test: str) -> PhaseSpec:
    return PhaseSpec(
        phase,
        f"phase{phase.lower()}: {slug}",
        (f"docs/phase{phase.lower()}-{slug.replace('_', '-')}.md", script, test),
    )


PHASES = (
    PhaseSpec(
        "4KX",
        "phase4kx: workspace-provenance-manifest",
        (
            "docs/phase4kx-workspace-provenance.md",
            "scripts/local/phase4kx_workspace_provenance.py",
            "tests/test_phase4kx_workspace_provenance.py",
        ),
    ),
    PhaseSpec(
        "4KY",
        "phase4ky: phase-owned-change-boundary-verifier",
        (
            "docs/phase4ky-change-boundary.md",
            "scripts/local/phase4ky_change_boundary.py",
            "tests/test_phase4ky_change_boundary.py",
        ),
    ),
    PhaseSpec(
        "4KZ",
        "phase4kz: commit-payload-and-staged-index-proof",
        (
            "docs/phase4kz-commit-payload-proof.md",
            "scripts/local/phase4kz_commit_payload_proof.py",
            "tests/test_phase4kz_commit_payload_proof.py",
        ),
    ),
    PhaseSpec(
        "4LA",
        "phase4la: commit-ancestry-and-parent-state-binding",
        (
            "docs/phase4la-commit-ancestry-binding.md",
            "scripts/local/phase4la_commit_ancestry.py",
            "tests/test_phase4la_commit_ancestry.py",
        ),
    ),
    PhaseSpec(
        "4LB",
        "phase4lb: commit-evidence-receipt-and-chain-ledger",
        (
            "docs/phase4lb-evidence-chain.md",
            "scripts/local/phase4lb_evidence_chain.py",
            "tests/test_phase4lb_evidence_chain.py",
        ),
    ),
    PhaseSpec(
        "4LC",
        "phase4lc: evidence-ledger-checkpoint-and-recovery-proof",
        (
            "docs/phase4lc-ledger-checkpoint.md",
            "scripts/local/phase4lc_ledger_checkpoint.py",
            "tests/test_phase4lc_ledger_checkpoint.py",
        ),
    ),
    PhaseSpec(
        "4LD",
        "phase4ld: independent-evidence-chain-recomputation-audit",
        (
            "docs/phase4ld-independent-chain-audit.md",
            "scripts/local/phase4ld_independent_chain_audit.py",
            "tests/test_phase4ld_independent_chain_audit.py",
        ),
    ),
    PhaseSpec(
        "4LE",
        "phase4le: evidence-schema-forward-compatibility-envelope",
        (
            "docs/phase4le-schema-compatibility.md",
            "scripts/local/phase4le_schema_compatibility.py",
            "tests/test_phase4le_schema_compatibility.py",
        ),
    ),
    PhaseSpec(
        "4LF",
        "phase4lf: evidence-migration-differential-simulator",
        (
            "docs/phase4lf-migration-simulator.md",
            "scripts/local/phase4lf_migration_simulator.py",
            "tests/test_phase4lf_migration_simulator.py",
        ),
    ),
    PhaseSpec(
        "4LG",
        "phase4lg: evidence-parser-resource-bound-adversarial-audit",
        (
            "docs/phase4lg-bounded-evidence-parser.md",
            "scripts/local/phase4lg_bounded_evidence_parser.py",
            "tests/test_phase4lg_bounded_evidence_parser.py",
        ),
    ),
    PhaseSpec(
        "4LH",
        "phase4lh: evidence-parser-fuzz-corpus-deterministic-minimizer",
        (
            "docs/phase4lh-fuzz-corpus.md",
            "scripts/local/phase4lh_fuzz_corpus.py",
            "tests/test_phase4lh_fuzz_corpus.py",
        ),
    ),
    PhaseSpec(
        "4LI",
        "phase4li: evidence-corpus-mutation-coverage-blind-spot-audit",
        (
            "docs/phase4li-mutation-coverage.md",
            "scripts/local/phase4li_mutation_coverage.py",
            "tests/test_phase4li_mutation_coverage.py",
        ),
    ),
)
AUXILIARY_SUBJECTS = (
    "phase4lg: reject capability-bearing extensions",
    "phase4lg: normalize malformed-json refusal signatures",
)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["git", *args], cwd=repo, check=False, capture_output=True)


def _history(repo: Path) -> tuple[dict[str, list[str]], dict[str, int]]:
    output = _git(repo, "log", "--first-parent", "HEAD", "-z", "--format=%H%x00%s").stdout
    fields = [
        field.decode("utf-8", errors="surrogateescape") for field in output.split(b"\0") if field
    ]
    result: dict[str, list[str]] = {}
    positions: dict[str, int] = {}
    for index in range(0, len(fields) - 1, 2):
        result.setdefault(fields[index + 1], []).append(fields[index])
        positions[fields[index]] = index // 2
    return result, positions


def _commit_path_map(repo: Path, commits: list[str]) -> dict[str, list[str]]:
    if not commits:
        return {}
    output = _git(
        repo, "show", "--root", "--no-renames", "--format=@@%H", "--name-only", *commits
    ).stdout.decode("utf-8", errors="surrogateescape")
    result: dict[str, list[str]] = {}
    current = ""
    for line in output.splitlines():
        if line.startswith("@@"):
            current = line.removeprefix("@@")
            result[current] = []
        elif current and line:
            result[current].append(line)
    return {commit: sorted(paths) for commit, paths in result.items()}


def certify(
    repo: Path,
    test_results: dict[str, dict[str, int]],
    runtime: dict[str, bool],
    phase_specs: tuple[PhaseSpec, ...] = PHASES,
    auxiliary_subjects: tuple[str, ...] = AUXILIARY_SUBJECTS,
) -> dict[str, object]:
    root = repo.resolve()
    errors: list[str] = []
    phase_rows: list[dict[str, object]] = []
    commits: list[str] = []
    subjects, positions = _history(root)
    all_phase_files = [path for spec in phase_specs for path in spec.files]
    dirty_output = _git(root, "status", "--porcelain=v1", "-z", "--", *all_phase_files).stdout
    dirty_paths = {
        field[3:].decode("utf-8", errors="surrogateescape")
        for field in dirty_output.split(b"\0")
        if len(field) >= 4
    }
    phase_matches = [subjects.get(spec.subject, []) for spec in phase_specs]
    path_map = _commit_path_map(
        root, [matches[0] for matches in phase_matches if len(matches) == 1]
    )
    for spec, matches in zip(phase_specs, phase_matches, strict=True):
        if len(matches) != 1:
            errors.append(f"{spec.phase}:COMMIT_SUBJECT_COUNT:{len(matches)}")
            commit = ""
        else:
            commit = matches[0]
            commits.append(commit)
        actual_paths = path_map.get(commit, [])
        if actual_paths != sorted(spec.files):
            errors.append(f"{spec.phase}:COMMIT_PATH_MISMATCH")
        missing = [path for path in spec.files if not (root / path).is_file()]
        if missing:
            errors.append(f"{spec.phase}:MISSING_DELIVERABLE")
        if any(path in dirty_paths for path in spec.files):
            errors.append(f"{spec.phase}:PHASE_FILES_DIRTY")
        tests = test_results.get(spec.phase)
        if (
            not isinstance(tests, dict)
            or tests.get("passed", 0) < 1
            or tests.get("failed") != 0
            or tests.get("skipped") != 0
        ):
            errors.append(f"{spec.phase}:TEST_GATE_FAILED")
        phase_rows.append(
            {
                "phase": spec.phase,
                "commit": commit,
                "subject": spec.subject,
                "files": list(spec.files),
                "tests": tests,
            }
        )
    for earlier, later in zip(commits, commits[1:], strict=False):
        if positions.get(earlier, -1) <= positions.get(later, -1):
            errors.append(f"ANCESTRY_ORDER_MISMATCH:{earlier}:{later}")
    auxiliary = []
    for subject in auxiliary_subjects:
        matches = subjects.get(subject, [])
        if len(matches) != 1:
            errors.append(f"AUXILIARY_COMMIT_SUBJECT_COUNT:{subject}:{len(matches)}")
        auxiliary.append({"subject": subject, "commit": matches[0] if len(matches) == 1 else ""})
    required_runtime = {
        "wsl_responsive",
        "scheduler_active",
        "ui_active",
        "live_disabled",
        "demo_disabled",
        "autopilot_disabled",
        "paper_creation_disabled",
        "paper_kill_switch_enabled",
        "no_additional_order_created",
    }
    for gate in sorted(required_runtime):
        if runtime.get(gate) is not True:
            errors.append(f"RUNTIME_GATE_FAILED:{gate}")
    errors = sorted(set(errors))
    total_passed = sum(row.get("passed", 0) for row in test_results.values())
    result: dict[str, object] = {
        "schema": SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "repository": str(root),
        "head": _git(root, "rev-parse", "HEAD").stdout.decode().strip(),
        "phase_count": len(phase_specs),
        "phases": phase_rows,
        "auxiliary_commits": auxiliary,
        "tests": {"passed": total_passed, "failed": 0, "skipped": 0},
        "runtime_gates": {gate: runtime.get(gate) for gate in sorted(required_runtime)},
        "errors": errors,
        "residual_risks": [
            "Certification covers evidence tooling and does not predict market outcomes.",
            "Read-only Git checks cannot prove behavior of opaque external binaries.",
            "This certificate grants no paper, demo, autopilot, live, or exchange authority.",
        ],
        "removal": "Revert the Phase 4KX-4LJ commits; no database rollback is required.",
        "safety": {
            "production_database_access": False,
            "service_control": False,
            "network_access": False,
            "writer_lock": False,
            "artifact_publication": False,
            "order_capability": False,
            "trading_authority": False,
        },
    }
    canonical = json.dumps(result, sort_keys=True, separators=(",", ":")).encode()
    result["certification_sha256"] = hashlib.sha256(canonical).hexdigest()
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--test-results", type=Path, required=True)
    parser.add_argument("--runtime-gates", type=Path, required=True)
    args = parser.parse_args()
    tests = json.loads(args.test_results.read_text(encoding="utf-8"))
    runtime = json.loads(args.runtime_gates.read_text(encoding="utf-8"))
    result = certify(args.repo, tests, runtime)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["verdict"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
