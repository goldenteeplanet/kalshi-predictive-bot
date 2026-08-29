"""Aggregate certification for the Phase 4OA-4OR pre-settlement program."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from scripts.local.phase4oa_aggregate_release_gate import BLOCKED_ON_SETTLEMENT

SCHEMA = "phase4os.program-certification.v1"
PHASES = tuple(f"4O{letter}" for letter in "ABCDEFGHIJKLMNOPQR")
CATEGORIES = (
    "aggregate_gate",
    "gate_mutation",
    "freshness",
    "renewal_race",
    "time_quorum",
    "membership_rotation",
    "emergency_freeze",
    "freeze_durability",
    "dual_copy_recovery",
    "anchor_provenance",
    "provenance_mutation",
    "chaos_rto",
    "tail_soak",
    "tail_regression",
    "baseline_lineage",
    "lineage_recovery",
    "placement_diversity",
    "placement_migration",
)


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _file_record(path: Path, root: Path) -> dict[str, object]:
    data = path.read_bytes()
    return {
        "path": path.relative_to(root).as_posix(),
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def inventory_program(repository_root: str | Path) -> dict[str, object]:
    root = Path(repository_root)
    errors, records = [], []
    for phase in PHASES:
        token = f"phase{phase.lower()}"
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
            kind: _file_record(paths[0], root) for kind, paths in matches.items() if len(paths) == 1
        }
        schema = None
        if len(matches["script"]) == 1:
            source = matches["script"][0].read_text(encoding="utf-8")
            found = re.search(r'^SCHEMA\s*=\s*["\']([^"\']+)["\']', source, re.MULTILINE)
            schema = found.group(1) if found else None
            required_safety = (
                '"paper_order_creation": False',
                '"demo_execution": False',
                '"live_execution": False',
                '"autopilot": False',
            )
            if schema is None:
                phase_errors.append("SCHEMA_MISSING")
            if not all(text in source for text in required_safety):
                phase_errors.append("EXECUTION_SAFETY_DECLARATION_MISSING")
        if len(matches["documentation"]) == 1:
            document = matches["documentation"][0].read_text(encoding="utf-8")
            if "Remove" not in document:
                phase_errors.append("ROLLBACK_INSTRUCTION_MISSING")
        body = {
            "phase": phase,
            "schema": schema,
            "files": files,
            "verdict": "PASS" if not phase_errors else "REFUSE",
            "errors": phase_errors,
        }
        records.append({**body, "phase_sha256": _digest(body)})
        errors.extend(f"{phase}:{error}" for error in phase_errors)
    body = {
        "schema": SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": errors,
        "phase_order": list(PHASES),
        "phase_count": len(records),
        "phases": records,
    }
    return {**body, "inventory_sha256": _digest(body)}


def certify_program(
    repository_root: str | Path, regression_evidence: dict[str, object]
) -> dict[str, object]:
    inventory = inventory_program(repository_root)
    errors = list(inventory["errors"])
    if regression_evidence.get("verdict") != "PASS":
        errors.append("AGGREGATE_REGRESSION_NOT_PASSING")
    if regression_evidence.get("categories") != list(CATEGORIES):
        errors.append("REPRESENTATIVE_CATEGORY_COVERAGE_INVALID")
    if not isinstance(regression_evidence.get("passed_tests"), int) or regression_evidence.get(
        "passed_tests", 0
    ) < len(PHASES):
        errors.append("AGGREGATE_TEST_COUNT_INSUFFICIENT")
    if not regression_evidence.get("proof_sha256"):
        errors.append("REGRESSION_PROOF_MISSING")
    dependency_graph = [
        {"from": left, "to": right} for left, right in zip(PHASES, PHASES[1:], strict=False)
    ]
    body = {
        "schema": SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": sorted(set(errors)),
        "program": "pre-settlement-adversarial-engineering-4oa-4or",
        "inventory": inventory,
        "regression_evidence": regression_evidence,
        "dependency_graph": dependency_graph,
        "category_order": list(CATEGORIES),
        "blocked_on_september_1_settlement": BLOCKED_ON_SETTLEMENT,
        "paper_position_preserved": "KXRAINAUSM-26AUG-1 contract 1; no additional order authorized",
        "rollback": (
            "Remove only the Phase 4OS files; all source phases remain independently intact."
        ),
        "residual_risks": [
            "Offline evidence cannot establish the future authoritative settlement outcome.",
            "Logical recovery simulations do not prove physical infrastructure independence.",
            "This certificate authorizes no paper, demo, live, or autopilot execution.",
        ],
        "safety": _safety(),
    }
    return {**body, "certificate_sha256": _digest(body)}


def independently_verify_certificate(
    certificate: object, *, trusted_inventory_sha256: str, trusted_regression_sha256: str
) -> dict[str, object]:
    errors = []
    if not isinstance(certificate, dict):
        return _verification(["CERTIFICATE_NOT_OBJECT"], None)
    unsigned = {key: value for key, value in certificate.items() if key != "certificate_sha256"}
    if certificate.get("certificate_sha256") != _digest(unsigned):
        errors.append("CERTIFICATE_HASH_MISMATCH")
    if certificate.get("schema") != SCHEMA or certificate.get("verdict") != "PASS":
        errors.append("CERTIFICATE_NOT_PASSING")
    inventory = certificate.get("inventory", {})
    if inventory.get("inventory_sha256") != trusted_inventory_sha256:
        errors.append("TRUSTED_INVENTORY_MISMATCH")
    inventory_unsigned = {
        key: value for key, value in inventory.items() if key != "inventory_sha256"
    }
    if inventory.get("inventory_sha256") != _digest(inventory_unsigned):
        errors.append("INVENTORY_HASH_MISMATCH")
    if inventory.get("phase_order") != list(PHASES) or inventory.get("phase_count") != len(PHASES):
        errors.append("PHASE_ORDER_OR_COUNT_INVALID")
    regression = certificate.get("regression_evidence", {})
    if regression.get("proof_sha256") != trusted_regression_sha256:
        errors.append("TRUSTED_REGRESSION_MISMATCH")
    if regression.get("categories") != list(CATEGORIES) or regression.get("verdict") != "PASS":
        errors.append("REGRESSION_COVERAGE_INVALID")
    expected_edges = [
        {"from": left, "to": right} for left, right in zip(PHASES, PHASES[1:], strict=False)
    ]
    if certificate.get("dependency_graph") != expected_edges:
        errors.append("DEPENDENCY_GRAPH_INVALID")
    if certificate.get("blocked_on_september_1_settlement") != BLOCKED_ON_SETTLEMENT:
        errors.append("SETTLEMENT_BLOCKER_DRIFT")
    if certificate.get("safety") != _safety():
        errors.append("SAFETY_INVARIANT_VIOLATION")
    if len(certificate.get("residual_risks", [])) != 3 or "Remove" not in certificate.get(
        "rollback", ""
    ):
        errors.append("RISK_OR_ROLLBACK_DISCLOSURE_INVALID")
    return _verification(sorted(set(errors)), certificate.get("certificate_sha256"))


def _verification(errors, subject):
    body = {
        "schema": SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": errors,
        "subject_sha256": subject,
        "safety": _safety(),
    }
    return {**body, "verification_sha256": _digest(body)}


def _safety() -> dict[str, bool]:
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
