"""Scan every Workstream IV risk/paper file for direct and indirect mutation capability."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

MANIFEST_SCHEMA = "phase4ev.scan-manifest.v1"
REPORT_SCHEMA = "phase4ev.scan-report.v1"
DISCOVERY_GLOB = "phase4e[a-u]_*.py"
MAX_FILES = 64
MAX_FILE_BYTES = 1_000_000
DATABASE_METHODS = {
    "add",
    "add_all",
    "bulk_insert_mappings",
    "bulk_update_mappings",
    "commit",
    "delete",
    "execute",
    "executemany",
    "flush",
    "merge",
}
ORDER_METHODS = {
    "create_order",
    "place_order",
    "submit_order",
    "create_paper_order",
}
SERVICE_METHODS = {
    "start",
    "stop",
    "restart",
}
ADAPTER_TOKENS = {
    "exchange_adapter",
    "execution_adapter",
    "order_adapter",
    "order_writer",
    "database_writer",
    "paper_order_writer",
}
ENTRYPOINT_TOKENS = {
    "create_order",
    "place_order",
    "submit_order",
    "route_order",
    "execute_order",
    "write_database",
    "acquire_writer_lock",
}


def _hash(value: Any) -> str:
    if isinstance(value, dict):
        value = {key: item for key, item in value.items() if key != "artifact_hash"}
    return canonical_hash(value)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _digest(value: Any, code: str) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise ValueError(code)
    try:
        int(value, 16)
    except ValueError as exc:
        raise ValueError(code) from exc
    return value


def _call_name(node: ast.Call) -> str:
    value = node.func
    parts: list[str] = []
    while isinstance(value, ast.Attribute):
        parts.append(value.attr)
        value = value.value
    if isinstance(value, ast.Name):
        parts.append(value.id)
    return ".".join(reversed(parts))


def _finding(path: str, line: int, capability: str, evidence: str) -> dict[str, Any]:
    result = {"path": path, "line": line, "capability": capability, "evidence_kind": evidence}
    result["finding_id"] = canonical_hash(result)
    return result


def _scan_file(root: Path, path: Path) -> list[dict[str, Any]]:
    relative = path.relative_to(root).as_posix()
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=relative)
    except (OSError, UnicodeError, SyntaxError) as exc:
        raise ValueError(f"PHASE4EV_SOURCE_INVALID:{relative}") from exc
    findings: list[dict[str, Any]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = _call_name(node)
            method = name.rsplit(".", 1)[-1]
            receiver = name.rsplit(".", 1)[0].lower() if "." in name else ""
            database_receiver = any(
                token in receiver
                for token in ("session", "connection", "cursor", "engine", "database", "db")
            )
            service_receiver = any(
                token in receiver for token in ("service", "systemd", "systemctl")
            )
            if (
                method in ORDER_METHODS
                or (method in DATABASE_METHODS and database_receiver)
                or (method in SERVICE_METHODS and service_receiver)
            ):
                findings.append(_finding(relative, node.lineno, "DIRECT_MUTATION_CALL", name))
            if name == "sqlite3.connect" and "mode=ro" not in ast.unparse(node):
                findings.append(
                    _finding(relative, node.lineno, "WRITABLE_DATABASE_CONNECTION", name)
                )
            if name in {"subprocess.run", "subprocess.Popen", "os.system", "os.popen"}:
                findings.append(_finding(relative, node.lineno, "SUBPROCESS_CONTROL_SURFACE", name))
            if name in {"eval", "exec"}:
                findings.append(_finding(relative, node.lineno, "DYNAMIC_CODE_EXECUTION", name))
            if name in {"__import__", "importlib.import_module"}:
                findings.append(_finding(relative, node.lineno, "DYNAMIC_IMPORT", name))
            if isinstance(node.func, ast.Call) and _call_name(node.func) == "getattr":
                findings.append(
                    _finding(relative, node.lineno, "DYNAMIC_GETATTR_DISPATCH", "CALL_OF_GETATTR")
                )
            if isinstance(node.func, ast.Subscript):
                findings.append(
                    _finding(
                        relative, node.lineno, "REGISTRY_OR_MAPPING_DISPATCH", "SUBSCRIPT_CALL"
                    )
                )
        if isinstance(node, ast.Import | ast.ImportFrom):
            names = []
            if isinstance(node, ast.Import):
                names.extend(alias.name for alias in node.names)
            else:
                names.append(node.module or "")
                names.extend(alias.name for alias in node.names)
            if any(token in name.lower() for token in ADAPTER_TOKENS for name in names):
                findings.append(
                    _finding(
                        relative, node.lineno, "INDIRECT_MUTATION_ADAPTER_IMPORT", "AST_IMPORT"
                    )
                )
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and any(
            token in node.name.lower() for token in ENTRYPOINT_TOKENS
        ):
            findings.append(
                _finding(relative, node.lineno, "MUTATION_ENTRYPOINT_DEFINITION", node.name)
            )
        if isinstance(node, ast.Assign | ast.AnnAssign):
            value = node.value
            alias_receiver = (
                ast.unparse(value.value).lower() if isinstance(value, ast.Attribute) else ""
            )
            if isinstance(value, ast.Attribute) and (
                value.attr in ORDER_METHODS
                or (
                    value.attr in DATABASE_METHODS
                    and any(
                        token in alias_receiver
                        for token in ("session", "connection", "cursor", "engine", "database", "db")
                    )
                )
                or (
                    value.attr in SERVICE_METHODS
                    and any(
                        token in alias_receiver for token in ("service", "systemd", "systemctl")
                    )
                )
            ):
                findings.append(
                    _finding(relative, node.lineno, "MUTATION_CALLABLE_ALIAS", value.attr)
                )
    unique = {finding["finding_id"]: finding for finding in findings}
    return sorted(
        unique.values(), key=lambda item: (item["path"], item["line"], item["capability"])
    )


def build_report(root: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    root = root.resolve(strict=True)
    if not isinstance(manifest, dict) or set(manifest) != {"schema", "files", "artifact_hash"}:
        raise ValueError("PHASE4EV_MANIFEST_FIELDS_INVALID")
    if manifest.get("schema") != MANIFEST_SCHEMA or manifest.get("artifact_hash") != _hash(
        manifest
    ):
        raise ValueError("PHASE4EV_MANIFEST_SCHEMA_OR_HASH_INVALID")
    entries = manifest["files"]
    if not isinstance(entries, list) or not entries or len(entries) > MAX_FILES:
        raise ValueError("PHASE4EV_MANIFEST_FILES_INVALID")
    expected: dict[str, str] = {}
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"path", "sha256"}:
            raise ValueError("PHASE4EV_MANIFEST_ENTRY_FIELDS_INVALID")
        relative = entry["path"]
        if (
            not isinstance(relative, str)
            or not relative
            or Path(relative).is_absolute()
            or ".." in Path(relative).parts
            or relative in expected
        ):
            raise ValueError("PHASE4EV_MANIFEST_PATH_INVALID")
        expected[relative] = _digest(entry["sha256"], "PHASE4EV_MANIFEST_DIGEST_INVALID")

    discovered_paths = sorted((root / "scripts/local").glob(DISCOVERY_GLOB))
    discovered = [path.relative_to(root).as_posix() for path in discovered_paths if path.is_file()]
    if set(expected) != set(discovered):
        raise ValueError("PHASE4EV_MANIFEST_COVERAGE_MISMATCH")
    file_results: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    for relative in sorted(discovered):
        path = root / relative
        if path.stat().st_size > MAX_FILE_BYTES:
            raise ValueError("PHASE4EV_FILE_SIZE_LIMIT_EXCEEDED")
        actual_hash = _sha(path)
        if actual_hash != expected[relative]:
            raise ValueError(f"PHASE4EV_FILE_HASH_MISMATCH:{relative}")
        file_findings = _scan_file(root, path)
        findings.extend(file_findings)
        file_results.append(
            {"path": relative, "sha256": actual_hash, "finding_count": len(file_findings)}
        )
    findings.sort(key=lambda item: (item["path"], item["line"], item["capability"]))
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "phase": "4EV",
        "manifest_hash": manifest["artifact_hash"],
        "discovery_glob": f"scripts/local/{DISCOVERY_GLOB}",
        "expected_file_count": len(expected),
        "scanned_file_count": len(file_results),
        "files": file_results,
        "scanned_files_hash": canonical_hash(file_results),
        "findings": findings,
        "findings_hash": canonical_hash(findings),
        "dynamic_dispatch_checked": True,
        "indirect_adapters_checked": True,
        "all_mutation_surfaces_absent": not findings,
        "advancement_allowed": not findings,
        "production_database_mutated": False,
        "services_controlled": False,
        "exchange_requests_made": False,
        "paper_orders_created": 0,
        "execution_authorized": False,
    }
    report["artifact_hash"] = _hash(report)
    return report


def publish(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository-root", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    report = build_report(args.repository_root, manifest)
    publish(args.output, report)
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
