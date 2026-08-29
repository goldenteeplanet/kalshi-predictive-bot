"""Aggregate release-candidate gate for offline adversarial validation phases."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

SCHEMA = "phase4oa.aggregate-release-gate.v1"
PHASES = ("MZ",) + tuple(f"N{letter}" for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ")
CHECK_CATEGORIES = (
    "golden",
    "differential",
    "metamorphic",
    "stateful",
    "long_horizon",
    "checkpoint",
    "quorum",
    "byzantine",
    "placement",
    "migration",
    "authorization",
    "ceremony",
    "independent_certification",
)
BLOCKED_ON_SETTLEMENT = (
    "The active KXRAINAUSM-26AUG-1 paper position cannot receive final settlement reconciliation, "
    "post-settlement scoring, or settlement-dependent promotion evidence until the authoritative "
    "September 1, 2026 settlement is available."
)


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def inventory_phases(repository_root: str | Path) -> dict[str, object]:
    root = Path(repository_root)
    records = []
    errors = []
    for phase in PHASES:
        token = f"phase4{phase.lower()}"
        matches = {
            "script": sorted((root / "scripts" / "local").glob(f"{token}_*.py")),
            "test": sorted((root / "tests").glob(f"test_{token}_*.py")),
            "documentation": sorted((root / "docs").glob(f"{token}-*.md")),
        }
        phase_errors = []
        for kind, paths in matches.items():
            if len(paths) != 1:
                phase_errors.append(f"{kind.upper()}_COUNT_{len(paths)}")
        files = {
            kind: {
                "path": path.relative_to(root).as_posix(),
                "sha256": _file_sha(path),
                "bytes": path.stat().st_size,
            }
            for kind, paths in matches.items()
            for path in paths[:1]
        }
        schema = None
        if matches["script"]:
            source = matches["script"][0].read_text(encoding="utf-8")
            found = re.search(r'^SCHEMA\s*=\s*["\']([^"\']+)["\']', source, re.MULTILINE)
            schema = found.group(1) if found else None
            if schema is None:
                phase_errors.append("SCHEMA_MISSING")
        if matches["documentation"]:
            document = matches["documentation"][0].read_text(encoding="utf-8")
            if "Remove" not in document and "remove" not in document:
                phase_errors.append("ROLLBACK_INSTRUCTION_MISSING")
        body = {
            "phase": f"4{phase}",
            "schema": schema,
            "files": files,
            "verdict": "PASS" if not phase_errors else "REFUSE",
            "errors": phase_errors,
        }
        records.append({**body, "phase_sha256": _digest(body)})
        errors.extend(f"4{phase}:{error}" for error in phase_errors)
    result = {
        "schema": SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": errors,
        "phase_count": len(records),
        "phases": records,
    }
    result["inventory_sha256"] = _digest(result)
    return result


def build_release_candidate(
    repository_root: str | Path,
    check_results: dict[str, dict[str, object]],
) -> dict[str, object]:
    inventory = inventory_phases(repository_root)
    errors = list(inventory["errors"])
    missing_checks = sorted(set(CHECK_CATEGORIES) - set(check_results))
    extra_checks = sorted(set(check_results) - set(CHECK_CATEGORIES))
    if missing_checks:
        errors.append("REPRESENTATIVE_CHECK_MISSING")
    if extra_checks:
        errors.append("UNKNOWN_REPRESENTATIVE_CHECK")
    normalized_checks = []
    refusal_classes = set()
    for category in CHECK_CATEGORIES:
        check = check_results.get(category)
        if not isinstance(check, dict):
            continue
        check_errors = []
        if check.get("verdict") != "PASS":
            check_errors.append("CONTRADICTORY_OR_FAILED_VERDICT")
        if check.get("deterministic") is not True or not check.get("proof_sha256"):
            check_errors.append("NONDETERMINISTIC_OR_UNBOUND_PROOF")
        safety = check.get("safety")
        if (
            not isinstance(safety, dict)
            or safety.get("offline_only") is not True
            or any(
                value is not False for key, value in (safety or {}).items() if key != "offline_only"
            )
        ):
            check_errors.append("EXECUTION_CAPABILITY_EXPOSURE")
        tested = check.get("refusal_classes_tested")
        if not isinstance(tested, list) or not tested:
            check_errors.append("UNTESTED_REFUSAL_CLASS")
        else:
            refusal_classes.update(tested)
        if check_errors:
            errors.extend(f"{category}:{error}" for error in check_errors)
        body = {
            "category": category,
            "verdict": check.get("verdict"),
            "proof_sha256": check.get("proof_sha256"),
            "deterministic": check.get("deterministic"),
            "refusal_classes_tested": tested,
            "safety": safety,
            "errors": check_errors,
        }
        normalized_checks.append({**body, "check_sha256": _digest(body)})
    dependency_graph = [
        {"from": f"4{left}", "to": f"4{right}"}
        for left, right in zip(PHASES, PHASES[1:], strict=False)
    ]
    aggregate = {
        "schema": SCHEMA,
        "release_candidate": "phase4oa-adversarial-validation-rc1",
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": sorted(set(errors)),
        "inventory": inventory,
        "representative_checks": normalized_checks,
        "refusal_classes_tested": sorted(refusal_classes),
        "dependency_graph": dependency_graph,
        "residual_risks": [
            "Offline simulation cannot prove future exchange availability or "
            "settlement correctness.",
            "Witness diversity remains a deployment requirement rather than a local-runtime fact.",
            "No evidence in this candidate authorizes trading execution.",
        ],
        "blocked_on_september_1_settlement": BLOCKED_ON_SETTLEMENT,
        "rollback": (
            "Remove the Phase 4OA files; all aggregated source phases retain their own "
            "rollback instructions."
        ),
        "safety": _safety(),
    }
    aggregate["manifest_sha256"] = _digest(aggregate)
    return aggregate


def verify_release_candidate(candidate: object, repository_root: str | Path) -> dict[str, object]:
    errors = []
    if not isinstance(candidate, dict) or candidate.get("schema") != SCHEMA:
        return _verification(["RELEASE_SCHEMA_INVALID"], None)
    unsigned = {key: value for key, value in candidate.items() if key != "manifest_sha256"}
    if candidate.get("manifest_sha256") != _digest(unsigned):
        errors.append("AGGREGATE_MANIFEST_HASH_MISMATCH")
    current = inventory_phases(repository_root)
    if current != candidate.get("inventory"):
        errors.append("STALE_OR_ALTERED_PHASE_EVIDENCE")
    if candidate.get("verdict") != "PASS" or candidate.get("errors"):
        errors.append("RELEASE_CANDIDATE_NOT_PASSING")
    if candidate.get("blocked_on_september_1_settlement") != BLOCKED_ON_SETTLEMENT:
        errors.append("SETTLEMENT_BLOCKER_STATEMENT_DRIFT")
    checks = candidate.get("representative_checks", [])
    if {row.get("category") for row in checks if isinstance(row, dict)} != set(CHECK_CATEGORIES):
        errors.append("REPRESENTATIVE_CHECK_MISSING")
    if len(candidate.get("dependency_graph", [])) != len(PHASES) - 1:
        errors.append("DEPENDENCY_GRAPH_INCOMPLETE")
    if candidate.get("safety") != _safety():
        errors.append("AGGREGATE_SAFETY_INVARIANT_VIOLATION")
    return _verification(sorted(set(errors)), candidate.get("manifest_sha256"))


def _verification(errors, manifest):
    result = {
        "schema": SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": errors,
        "manifest_sha256": manifest,
        "safety": _safety(),
    }
    result["certification_sha256"] = _digest(result)
    return result


def _safety():
    return {
        "offline_only": True,
        "infrastructure_mutation": False,
        "persistence": False,
        "network_access": False,
        "runtime_write": False,
        "paper_order_creation": False,
        "demo_execution": False,
        "live_execution": False,
        "autopilot": False,
    }
