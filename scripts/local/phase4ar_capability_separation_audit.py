"""Phase 4AR static capability graph and separation audit."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

SCHEMA = "phase4ar.capability-graph.v1"
MANIFEST_SCHEMA = "phase4ar.separation-verdict.v1"
ROLES = {
    "REVIEW",
    "READINESS",
    "SIMULATION",
    "AUTHORIZATION_VALIDATION",
    "HYPOTHETICAL_EXECUTION",
}
MUTATION_ROLES = {"SIMULATION", "HYPOTHETICAL_EXECUTION"}
FORBIDDEN_CAPABILITIES = {
    "ENVIRONMENT_SETTING_MUTATION",
    "EXCHANGE_CLIENT",
    "PRODUCTION_WRITER_LOCK",
    "SERIALIZED_CALLBACK",
    "SERVICE_CONTROL",
    "SHELL_OR_SUBPROCESS",
}


def _hash(payload: dict[str, Any], field: str = "artifact_hash") -> str:
    return canonical_hash({key: value for key, value in payload.items() if key != field})


def _file_hash(path: Path) -> str:
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


def _string_values(tree: ast.AST) -> list[tuple[int, str]]:
    return sorted(
        (node.lineno, node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    )


def inspect_source(role: str, path: Path) -> dict[str, Any]:
    if role not in ROLES:
        raise ValueError("PHASE4AR_ROLE_INVALID")
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
    except (OSError, UnicodeError, SyntaxError) as exc:
        raise ValueError("PHASE4AR_SOURCE_UNREADABLE_OR_INVALID") from exc
    imports: set[str] = set()
    calls: list[tuple[int, str]] = []
    cli_options: set[str] = set()
    capabilities: dict[str, set[int]] = {}

    def add(capability: str, line: int) -> None:
        capabilities.setdefault(capability, set()).add(line)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.add(node.module or "")
        elif isinstance(node, ast.Call):
            name = _call_name(node)
            calls.append((node.lineno, name))
            if name.endswith("add_argument"):
                cli_options.update(
                    value.value
                    for value in node.args
                    if isinstance(value, ast.Constant)
                    and isinstance(value.value, str)
                    and value.value.startswith("--")
                )
            if name in {"subprocess.run", "subprocess.Popen", "os.system", "os.popen"}:
                add("SHELL_OR_SUBPROCESS", node.lineno)
            if name in {"os.putenv", "os.unsetenv"}:
                add("ENVIRONMENT_SETTING_MUTATION", node.lineno)
            if name in {"fcntl.flock", "portalocker.lock"}:
                add("PRODUCTION_WRITER_LOCK", node.lineno)
            if name in {"eval", "exec", "pickle.loads", "marshal.loads", "cloudpickle.loads"}:
                add("SERIALIZED_CALLBACK", node.lineno)
            if name.endswith("connect") and (
                name == "sqlite3.connect" or name.endswith(".connect")
            ):
                rendered = ast.unparse(node)
                add(
                    "DATABASE_OPEN_READ_ONLY"
                    if "mode=ro" in rendered
                    else "DATABASE_OPEN_WRITABLE",
                    node.lineno,
                )
        elif isinstance(node, ast.Assign | ast.AnnAssign | ast.AugAssign):
            rendered = ast.unparse(node)
            if "os.environ[" in rendered:
                add("ENVIRONMENT_SETTING_MUTATION", node.lineno)
    for line, value in _string_values(tree):
        lowered = value.lower()
        if any(token in lowered for token in ("systemctl ", "service ", "sc.exe ")):
            add("SERVICE_CONTROL", line)
        if any(token in lowered for token in ("create_order", "place_order", "/portfolio/orders")):
            add("EXCHANGE_CLIENT", line)
        if any(
            token in lowered
            for token in (
                "update settlements",
                "insert into settlements",
                "delete from settlements",
            )
        ):
            add("SETTLEMENT_MUTATION_SQL", line)
        if "begin exclusive" in lowered or "writer.lock" in lowered:
            add("PRODUCTION_WRITER_LOCK", line)
    for imported in imports:
        root = imported.split(".")[0]
        if root in {"requests", "httpx", "aiohttp", "websockets", "ccxt"}:
            add("NETWORK_CAPABLE_DEPENDENCY", 0)
        if root == "subprocess":
            add("SHELL_OR_SUBPROCESS", 0)
    rows = [
        {"capability": capability, "line_numbers": sorted(lines)}
        for capability, lines in sorted(capabilities.items())
    ]
    return {
        "role": role,
        "path": path.as_posix(),
        "source_sha256": _file_hash(path),
        "imports": sorted(imports),
        "cli_options": sorted(cli_options),
        "capabilities": rows,
        "capabilities_hash": canonical_hash(rows),
    }


def build(
    sources: list[tuple[str, Path]], *, now: datetime
) -> tuple[dict[str, Any], dict[str, Any]]:
    if now.tzinfo is None:
        raise ValueError("PHASE4AR_EVALUATION_TIMEZONE_MISSING")
    normalized = [(role, path.resolve(strict=True)) for role, path in sources]
    keys = [(role, str(path)) for role, path in normalized]
    if not normalized or len(keys) != len(set(keys)):
        raise ValueError("PHASE4AR_SOURCES_EMPTY_OR_DUPLICATED")
    nodes = [
        inspect_source(role, path)
        for role, path in sorted(normalized, key=lambda row: (row[0], str(row[1])))
    ]
    path_to_role = {Path(node["path"]).stem: node["role"] for node in nodes}
    edges: list[dict[str, str]] = []
    for node in nodes:
        for imported in node["imports"]:
            imported_stem = imported.rsplit(".", 1)[-1]
            if imported_stem in path_to_role:
                edges.append(
                    {
                        "from_role": node["role"],
                        "to_role": path_to_role[imported_stem],
                        "dependency": imported,
                    }
                )
    edges.sort(key=lambda row: (row["from_role"], row["to_role"], row["dependency"]))
    violations: list[dict[str, Any]] = []
    for node in nodes:
        for capability in node["capabilities"]:
            name = capability["capability"]
            if name in FORBIDDEN_CAPABILITIES or (
                name in {"DATABASE_OPEN_WRITABLE", "SETTLEMENT_MUTATION_SQL"}
                and node["role"] not in MUTATION_ROLES
            ):
                violations.append(
                    {
                        "role": node["role"],
                        "path": node["path"],
                        "capability": name,
                        "line_numbers": capability["line_numbers"],
                    }
                )
    violations.sort(key=lambda row: (row["role"], row["path"], row["capability"]))
    graph: dict[str, Any] = {
        "schema": SCHEMA,
        "phase": "4AR",
        "evaluated_at": now.astimezone(UTC).isoformat(),
        "nodes": nodes,
        "nodes_hash": canonical_hash(nodes),
        "edges": edges,
        "edges_hash": canonical_hash(edges),
        "review_readiness_authorization_are_non_mutating": not any(
            violation["role"] not in MUTATION_ROLES for violation in violations
        ),
        "production_database_mutated": False,
        "research_database_mutated": False,
        "database_mutation_performed": False,
        "production_lock_acquired": False,
        "services_controlled": False,
        "exchange_requests_made": False,
        "orders_created": False,
        "execution_authorized": False,
    }
    pair_id = canonical_hash({"nodes": graph["nodes_hash"], "edges": graph["edges_hash"]})
    graph["publication_pair_id"] = pair_id
    graph["artifact_hash"] = _hash(graph)
    verdict: dict[str, Any] = {
        "schema": MANIFEST_SCHEMA,
        "phase": "4AR",
        "publication_pair_id": pair_id,
        "capability_graph_hash": graph["artifact_hash"],
        "violations": violations,
        "violations_hash": canonical_hash(violations),
        "separation_verdict": "CAPABILITIES_SEPARATED" if not violations else "SEPARATION_FAILED",
        "advancement_allowed": not violations,
        "production_execution_authorized": False,
    }
    verdict["manifest_hash"] = _hash(verdict, "manifest_hash")
    return graph, verdict


def _source(value: str) -> tuple[str, Path]:
    try:
        role, path = value.split("=", 1)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected ROLE=PATH") from exc
    if role not in ROLES:
        raise argparse.ArgumentTypeError("invalid role")
    return role, Path(path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", action="append", type=_source, required=True)
    parser.add_argument("--evaluation-time", required=True)
    parser.add_argument("--graph-output", type=Path, required=True)
    parser.add_argument("--verdict-output", type=Path, required=True)
    args = parser.parse_args()
    try:
        now = datetime.fromisoformat(args.evaluation_time.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("PHASE4AR_EVALUATION_TIME_INVALID") from exc
    graph, verdict = build(args.source, now=now)
    from phase4al_offline_protocol_simulation import publish_pair

    publish_pair(args.graph_output, args.verdict_output, graph, verdict)
    print(json.dumps(verdict, sort_keys=True))


if __name__ == "__main__":
    main()
