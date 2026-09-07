"""Phase 4AS repository scanner for unexplained settlement mutation surfaces."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

SCHEMA = "phase4as.mutation-surface-inventory.v1"
MANIFEST_SCHEMA = "phase4as.mutation-surface-verdict.v1"
ALLOWLIST_SCHEMA = "phase4as.mutation-surface-allowlist.v1"
ALLOWED_SCOPES = {"TEST", "DISPOSABLE_SIMULATION", "ISOLATED_RESEARCH"}
EXCLUDED_PARTS = {".git", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".venv", "tmp"}
MAX_FILES = 20_000
MAX_FILE_BYTES = 2_000_000


def _hash(payload: dict[str, Any], field: str = "artifact_hash") -> str:
    return canonical_hash({key: value for key, value in payload.items() if key != field})


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _call_name(node: ast.Call) -> str:
    value = node.func
    parts: list[str] = []
    while isinstance(value, ast.Attribute):
        parts.append(value.attr)
        value = value.value
    if isinstance(value, ast.Name):
        parts.append(value.id)
    return ".".join(reversed(parts))


def _finding(path: str, capability: str, line: int, evidence: str) -> dict[str, Any]:
    row = {
        "path": path,
        "capability": capability,
        "line": line,
        "evidence_kind": evidence,
    }
    row["finding_id"] = canonical_hash(row)
    return row


def _python_findings(root: Path, path: Path) -> list[dict[str, Any]]:
    relative = path.relative_to(root).as_posix()
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=relative)
    except (OSError, UnicodeError, SyntaxError) as exc:
        raise ValueError(f"PHASE4AS_SOURCE_INVALID:{relative}") from exc
    findings: list[dict[str, Any]] = []
    mutating = False
    settlement_context = "settlement" in source.lower()
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = _call_name(node)
            if name == "sqlite3.connect" and "mode=ro" not in ast.unparse(node):
                findings.append(
                    _finding(relative, "WRITABLE_SQLITE_CONNECTION", node.lineno, "AST_CALL")
                )
            if settlement_context and name in {
                "subprocess.run",
                "subprocess.Popen",
                "os.system",
                "os.popen",
            }:
                findings.append(
                    _finding(relative, "SUBPROCESS_MUTATION_COMMAND", node.lineno, "AST_CALL")
                )
            if settlement_context and name in {"fcntl.flock", "portalocker.lock"}:
                findings.append(
                    _finding(relative, "WRITER_LOCK_ACQUISITION", node.lineno, "AST_CALL")
                )
            method = name.rsplit(".", 1)[-1]
            receiver = name.rsplit(".", 1)[0].lower() if "." in name else ""
            orm_session_mutation = method in {
                "add",
                "add_all",
                "bulk_insert_mappings",
                "bulk_update_mappings",
                "delete",
                "merge",
            } and "session" in receiver
            dynamic_execute = method == "execute" and any(
                token in receiver for token in ("connection", "cursor", "engine", "session")
            )
            if settlement_context and (orm_session_mutation or dynamic_execute):
                if (
                    name.endswith("execute")
                    and node.args
                    and isinstance(node.args[0], ast.Constant)
                ):
                    pass
                else:
                    findings.append(
                        _finding(relative, "ORM_OR_DYNAMIC_MUTATION", node.lineno, "AST_CALL")
                    )
            if settlement_context and name.startswith("op.") and name not in {"op.get_bind"}:
                findings.append(_finding(relative, "MIGRATION_PATH", node.lineno, "ALEMBIC_CALL"))
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            lowered = " ".join(node.value.lower().split())
            sql_evidence_context = isinstance(
                parents.get(node), (ast.Assign, ast.AnnAssign, ast.Call)
            )
            for prefix, capability in (
                ("update settlements", "SETTLEMENT_UPDATE_SQL"),
                ("insert into settlements", "SETTLEMENT_INSERT_SQL"),
                ("delete from settlements", "SETTLEMENT_DELETE_SQL"),
            ):
                if lowered.startswith(prefix) and sql_evidence_context:
                    findings.append(_finding(relative, capability, node.lineno, "SQL_LITERAL"))
                    mutating = True
    if mutating and any(
        isinstance(node, ast.If)
        and isinstance(node.test, ast.Compare)
        and "__name__" in ast.unparse(node.test)
        for node in ast.walk(tree)
    ):
        findings.append(_finding(relative, "MUTATION_CAPABLE_ENTRYPOINT", 0, "MODULE_MAIN_GUARD"))
    unique = {row["finding_id"]: row for row in findings}
    return sorted(unique.values(), key=lambda row: (row["path"], row["line"], row["capability"]))


def _text_findings(root: Path, path: Path) -> list[dict[str, Any]]:
    relative = path.relative_to(root).as_posix()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"PHASE4AS_SOURCE_INVALID:{relative}") from exc
    findings: list[dict[str, Any]] = []
    for number, line in enumerate(lines, 1):
        lowered = line.lower()
        settlement_context = "settlement" in lowered or "sqlite" in lowered
        if settlement_context and ("systemctl" in lowered or " service " in lowered):
            findings.append(_finding(relative, "SERVICE_SCRIPT_CONTROL", number, "TEXT_LINE"))
        if "sqlite3" in lowered and any(word in lowered for word in ("update", "insert", "delete")):
            findings.append(_finding(relative, "SUBPROCESS_MUTATION_COMMAND", number, "TEXT_LINE"))
        if settlement_context and "flock" in lowered:
            findings.append(_finding(relative, "WRITER_LOCK_ACQUISITION", number, "TEXT_LINE"))
    return findings


def _load_allowlist(
    path: Path,
) -> tuple[dict[tuple[str, str], dict[str, Any]], list[str], str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("PHASE4AS_ALLOWLIST_UNREADABLE") from exc
    if payload.get("schema") != ALLOWLIST_SCHEMA or payload.get("artifact_hash") != _hash(payload):
        raise ValueError("PHASE4AS_ALLOWLIST_SCHEMA_OR_HASH_INVALID")
    entries = payload.get("entries")
    include_globs = payload.get("include_globs", ["**/*"])
    if not isinstance(entries, list):
        raise ValueError("PHASE4AS_ALLOWLIST_ENTRIES_INVALID")
    if (
        not isinstance(include_globs, list)
        or not include_globs
        or any(
            not isinstance(value, str)
            or not value
            or Path(value).is_absolute()
            or ".." in Path(value).parts
            for value in include_globs
        )
    ):
        raise ValueError("PHASE4AS_ALLOWLIST_INCLUDE_GLOBS_INVALID")
    mapped: dict[tuple[str, str], dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("PHASE4AS_ALLOWLIST_ENTRY_INVALID")
        key = (entry.get("path"), entry.get("capability"))
        if (
            not all(isinstance(value, str) and value for value in key)
            or key in mapped
            or entry.get("scope") not in ALLOWED_SCOPES
            or not isinstance(entry.get("justification"), str)
            or not entry["justification"].strip()
        ):
            raise ValueError("PHASE4AS_ALLOWLIST_ENTRY_INVALID_OR_DUPLICATED")
        mapped[key] = entry
    return mapped, sorted(set(include_globs)), payload["artifact_hash"]


def build(
    root: Path, allowlist_path: Path, *, now: datetime
) -> tuple[dict[str, Any], dict[str, Any]]:
    if now.tzinfo is None:
        raise ValueError("PHASE4AS_EVALUATION_TIMEZONE_MISSING")
    root = root.resolve(strict=True)
    allowlist, include_globs, allowlist_hash = _load_allowlist(allowlist_path)
    candidates = {path for glob in include_globs for path in root.glob(glob)}
    paths = sorted(
        path
        for path in candidates
        if path.is_file()
        and not EXCLUDED_PARTS.intersection(path.relative_to(root).parts)
        and path.suffix.lower() in {".py", ".sh", ".service", ".timer"}
    )
    if len(paths) > MAX_FILES:
        raise ValueError("PHASE4AS_FILE_LIMIT_EXCEEDED")
    findings: list[dict[str, Any]] = []
    file_hashes: list[dict[str, str]] = []
    for path in paths:
        if path.stat().st_size > MAX_FILE_BYTES:
            raise ValueError("PHASE4AS_FILE_SIZE_LIMIT_EXCEEDED")
        relative = path.relative_to(root).as_posix()
        file_hashes.append({"path": relative, "sha256": _sha(path)})
        findings.extend(
            _python_findings(root, path)
            if path.suffix.lower() == ".py"
            else _text_findings(root, path)
        )
    findings.sort(key=lambda row: (row["path"], row["line"], row["capability"]))
    classified: list[dict[str, Any]] = []
    used: set[tuple[str, str]] = set()
    for finding in findings:
        key = (finding["path"], finding["capability"])
        entry = allowlist.get(key)
        built_in_test_scope = finding["path"].startswith("tests/")
        row = dict(finding)
        row["classification"] = (
            "ALLOWLISTED_SAFE_SCOPE"
            if entry
            else "ALLOWLISTED_TEST_PATH"
            if built_in_test_scope
            else "UNEXPLAINED"
        )
        row["allowlist_scope"] = (
            "TEST" if built_in_test_scope else None if entry is None else entry["scope"]
        )
        row["justification_hash"] = (
            None if entry is None else canonical_hash(entry["justification"])
        )
        classified.append(row)
        if entry:
            used.add(key)
    stale_allowlist = [
        {"path": path, "capability": capability}
        for path, capability in sorted(set(allowlist) - used)
    ]
    unexplained = [row for row in classified if row["classification"] == "UNEXPLAINED"]
    inventory: dict[str, Any] = {
        "schema": SCHEMA,
        "phase": "4AS",
        "evaluated_at": now.astimezone(UTC).isoformat(),
        "root_identity": canonical_hash(str(root)),
        "allowlist_hash": allowlist_hash,
        "include_globs": include_globs,
        "scanned_file_count": len(paths),
        "scanned_files_hash": canonical_hash(file_hashes),
        "findings": classified,
        "findings_hash": canonical_hash(classified),
        "production_database_mutated": False,
        "research_database_mutated": False,
        "database_mutation_performed": False,
        "production_lock_acquired": False,
        "services_controlled": False,
        "exchange_requests_made": False,
        "orders_created": False,
        "execution_authorized": False,
    }
    pair_id = canonical_hash(
        {"files": inventory["scanned_files_hash"], "findings": inventory["findings_hash"]}
    )
    inventory["publication_pair_id"] = pair_id
    inventory["artifact_hash"] = _hash(inventory)
    verdict: dict[str, Any] = {
        "schema": MANIFEST_SCHEMA,
        "phase": "4AS",
        "publication_pair_id": pair_id,
        "inventory_hash": inventory["artifact_hash"],
        "unexplained_findings": unexplained,
        "unexplained_findings_hash": canonical_hash(unexplained),
        "stale_allowlist_entries": stale_allowlist,
        "stale_allowlist_hash": canonical_hash(stale_allowlist),
        "verdict": "MUTATION_SURFACES_EXPLAINED"
        if not unexplained and not stale_allowlist
        else "UNEXPLAINED_MUTATION_SURFACE",
        "advancement_allowed": not unexplained and not stale_allowlist,
        "production_execution_authorized": False,
    }
    verdict["manifest_hash"] = _hash(verdict, "manifest_hash")
    return inventory, verdict


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository-root", type=Path, required=True)
    parser.add_argument("--allowlist", type=Path, required=True)
    parser.add_argument("--evaluation-time", required=True)
    parser.add_argument("--inventory-output", type=Path, required=True)
    parser.add_argument("--verdict-output", type=Path, required=True)
    args = parser.parse_args()
    try:
        now = datetime.fromisoformat(args.evaluation_time.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("PHASE4AS_EVALUATION_TIME_INVALID") from exc
    inventory, verdict = build(args.repository_root, args.allowlist, now=now)
    from phase4al_offline_protocol_simulation import publish_pair

    publish_pair(args.inventory_output, args.verdict_output, inventory, verdict)
    print(json.dumps(verdict, sort_keys=True))


if __name__ == "__main__":
    main()
