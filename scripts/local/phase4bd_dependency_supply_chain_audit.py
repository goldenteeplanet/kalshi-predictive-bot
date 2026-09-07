"""Phase 4BD local-only dependency and supply-chain integrity audit."""

from __future__ import annotations

import argparse
import ast
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

INPUT_SCHEMA = "phase4bd.dependency-audit-input.v1"
SBOM_SCHEMA = "phase4bd.local-sbom.v1"
RISK_SCHEMA = "phase4bd.dependency-risk-report.v1"


def _hash(payload: dict[str, Any], field: str = "artifact_hash") -> str:
    return canonical_hash({key: value for key, value in payload.items() if key != field})


def _load(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("PHASE4BD_INPUT_UNREADABLE") from exc
    if payload.get("schema") != INPUT_SCHEMA or payload.get("artifact_hash") != _hash(payload):
        raise ValueError("PHASE4BD_INPUT_SCHEMA_OR_HASH_INVALID")
    return payload


def _inside(root: Path, relative: str, label: str) -> Path:
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        raise ValueError(f"PHASE4BD_{label}_PATH_INVALID")
    path = root / relative
    if path.is_symlink():
        raise ValueError(f"PHASE4BD_{label}_SYMLINK_REFUSED")
    resolved = path.resolve(strict=True)
    try:
        resolved.relative_to(root.resolve(strict=True))
    except ValueError as exc:
        raise ValueError(f"PHASE4BD_{label}_OUTSIDE_ROOT") from exc
    if not resolved.is_file():
        raise ValueError(f"PHASE4BD_{label}_NOT_FILE")
    return resolved


def _imports(root: Path, sources: list[str]) -> list[str]:
    names: set[str] = set()
    for source in sources:
        path = _inside(root, source, "SOURCE")
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=source)
        except (UnicodeDecodeError, SyntaxError) as exc:
            raise ValueError("PHASE4BD_SOURCE_PARSE_FAILED") from exc
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                names.add(node.module.split(".")[0])
    return sorted(names)


def build(root: Path, input_path: Path, *, now: datetime) -> tuple[dict[str, Any], dict[str, Any]]:
    if now.tzinfo is None:
        raise ValueError("PHASE4BD_EVALUATION_TIMEZONE_MISSING")
    source = _load(input_path)
    source_paths = source.get("source_paths")
    dependencies = source.get("dependencies")
    if not isinstance(source_paths, list) or not source_paths or not isinstance(dependencies, list):
        raise ValueError("PHASE4BD_SOURCES_OR_DEPENDENCIES_INVALID")
    imported = _imports(root, source_paths)
    rows: list[dict[str, Any]] = []
    names: set[str] = set()
    for dependency in dependencies:
        if not isinstance(dependency, dict):
            raise ValueError("PHASE4BD_DEPENDENCY_INVALID")
        name, module = dependency.get("name"), dependency.get("import_name")
        version = dependency.get("locked_version")
        if not all(isinstance(value, str) and value for value in (name, module, version)):
            raise ValueError("PHASE4BD_DEPENDENCY_IDENTITY_INVALID")
        if name in names:
            raise ValueError("PHASE4BD_DUPLICATE_DEPENDENCY")
        names.add(name)
        lock = _inside(root, dependency.get("lock_source"), "LOCK")
        provenance = _inside(root, dependency.get("local_provenance"), "PROVENANCE")
        lock_hash = canonical_hash(lock.read_bytes().hex())
        provenance_payload = json.loads(provenance.read_text(encoding="utf-8"))
        expected_provenance_hash = dependency.get("expected_provenance_hash")
        reasons: list[str] = []
        if lock_hash != dependency.get("expected_lock_hash"):
            reasons.append("DEPENDENCY_LOCK_DRIFT")
        if canonical_hash(provenance_payload) != expected_provenance_hash:
            reasons.append("DEPENDENCY_PROVENANCE_DRIFT")
        if provenance_payload.get("name") != name or provenance_payload.get("version") != version:
            reasons.append("DEPENDENCY_VERSION_DRIFT")
        hooks = dependency.get("executable_hooks")
        generated = dependency.get("generated_files")
        capabilities = dependency.get("capabilities")
        if (
            not isinstance(hooks, list)
            or not isinstance(generated, list)
            or not isinstance(capabilities, list)
        ):
            raise ValueError("PHASE4BD_DEPENDENCY_METADATA_INVALID")
        if hooks:
            reasons.append("UNEXPECTED_EXECUTABLE_HOOK")
        if any(
            capability not in {"READ_ONLY", "SERIALIZATION", "HASHING"}
            for capability in capabilities
        ):
            reasons.append("MUTATION_OR_NETWORK_CAPABLE_DEPENDENCY")
        if module not in imported:
            reasons.append("DEPENDENCY_NOT_USED_BY_GUARDED_SOURCES")
        row = {
            "name": name,
            "import_name": module,
            "locked_version": version,
            "lock_source": dependency["lock_source"],
            "lock_hash": lock_hash,
            "local_provenance": dependency["local_provenance"],
            "provenance_hash": canonical_hash(provenance_payload),
            "executable_hooks": hooks,
            "generated_files": generated,
            "capabilities": sorted(capabilities),
            "reason_codes": sorted(reasons),
            "integrity_verified": not reasons,
        }
        row["component_hash"] = canonical_hash(row)
        rows.append(row)
    unexplained_imports = sorted(
        name
        for name in imported
        if name not in set(source.get("stdlib_imports", []))
        and name not in {dependency["import_name"] for dependency in dependencies}
    )
    if unexplained_imports:
        raise ValueError("PHASE4BD_UNEXPLAINED_IMPORTS")
    rows.sort(key=lambda row: row["name"])
    evaluated_at = now.astimezone(UTC).isoformat()
    sbom: dict[str, Any] = {
        "schema": SBOM_SCHEMA,
        "phase": "4BD",
        "evaluated_at": evaluated_at,
        "input_hash": source["artifact_hash"],
        "guarded_source_imports": imported,
        "components": rows,
        "component_count": len(rows),
        "installed_or_upgraded_dependencies": False,
        "network_access_performed": False,
        "execution_authorized": False,
    }
    sbom["artifact_hash"] = _hash(sbom)
    findings = [
        {"component": row["name"], "reason": reason}
        for row in rows
        for reason in row["reason_codes"]
    ]
    risk: dict[str, Any] = {
        "schema": RISK_SCHEMA,
        "phase": "4BD",
        "evaluated_at": evaluated_at,
        "sbom_hash": sbom["artifact_hash"],
        "findings": findings,
        "finding_count": len(findings),
        "dependency_integrity_verified": not findings,
        "database_mutation_performed": False,
        "network_access_performed": False,
        "installed_or_upgraded_dependencies": False,
        "execution_authorized": False,
    }
    risk["artifact_hash"] = _hash(risk)
    return sbom, risk


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository-root", type=Path, required=True)
    parser.add_argument("--audit-input", type=Path, required=True)
    parser.add_argument("--evaluation-time", required=True)
    parser.add_argument("--sbom-output", type=Path, required=True)
    parser.add_argument("--risk-output", type=Path, required=True)
    args = parser.parse_args()
    now = datetime.fromisoformat(args.evaluation_time.replace("Z", "+00:00"))
    sbom, risk = build(args.repository_root, args.audit_input, now=now)
    from phase4al_offline_protocol_simulation import publish_pair

    publish_pair(args.sbom_output, args.risk_output, sbom, risk)
    print(json.dumps(risk, sort_keys=True))


if __name__ == "__main__":
    main()
