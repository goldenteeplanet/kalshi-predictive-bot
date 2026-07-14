from __future__ import annotations

import ast
import configparser
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ALEMBIC_AT_HEAD = "AT_HEAD"
ALEMBIC_UPGRADE_REQUIRED = "UPGRADE_REQUIRED"
ALEMBIC_MULTIPLE_HEADS = "MULTIPLE_HEADS"
ALEMBIC_ORPHANED_DATABASE_REVISION = "ORPHANED_DATABASE_REVISION"
ALEMBIC_CONFIG_MISMATCH = "CONFIG_MISMATCH"
ALEMBIC_UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class RevisionNode:
    revision: str
    down_revisions: tuple[str, ...]
    path: str


def alembic_graph_diagnostics(
    current_revisions: list[str] | tuple[str, ...] | None,
    *,
    root: Path | None = None,
    script_location: Path | str | None = None,
) -> dict[str, Any]:
    repo_root = root or Path.cwd()
    script_dir = _resolve_script_location(repo_root, script_location)
    graph = load_revision_graph(script_dir)
    if not graph:
        return {
            "status": ALEMBIC_CONFIG_MISMATCH,
            "current_revisions": list(current_revisions or []),
            "head_revisions": [],
            "script_location": str(script_dir),
            "message": "No Alembic revisions were found.",
        }
    heads = sorted(_head_revisions(graph))
    current = sorted(revision for revision in current_revisions or [] if revision)
    status = classify_revisions(current, graph)
    return {
        "status": status,
        "current_revisions": current,
        "head_revisions": heads,
        "script_location": str(script_dir),
        "message": _status_message(status, current, heads),
        "revision_count": len(graph),
        "graph": {
            revision: {
                "down_revisions": list(node.down_revisions),
                "path": node.path,
            }
            for revision, node in sorted(graph.items())
        },
    }


def classify_revisions(
    current_revisions: list[str] | tuple[str, ...],
    graph: dict[str, RevisionNode],
) -> str:
    if not graph:
        return ALEMBIC_CONFIG_MISMATCH
    heads = sorted(_head_revisions(graph))
    if len(heads) > 1:
        return ALEMBIC_MULTIPLE_HEADS
    current = [revision for revision in current_revisions if revision]
    if not current:
        return ALEMBIC_UPGRADE_REQUIRED
    if any(revision not in graph for revision in current):
        return ALEMBIC_ORPHANED_DATABASE_REVISION
    if sorted(current) == heads:
        return ALEMBIC_AT_HEAD
    head = heads[0]
    if all(_is_ancestor_or_self(revision, head, graph) for revision in current):
        return ALEMBIC_UPGRADE_REQUIRED
    return ALEMBIC_ORPHANED_DATABASE_REVISION


def load_revision_graph(script_dir: Path | str) -> dict[str, RevisionNode]:
    path = Path(script_dir)
    versions_dir = path / "versions" if (path / "versions").exists() else path
    graph: dict[str, RevisionNode] = {}
    if not versions_dir.exists():
        return graph
    for file_path in sorted(versions_dir.glob("*.py")):
        node = _parse_revision_file(file_path)
        if node is not None:
            graph[node.revision] = node
    return graph


def latest_head_revision(*, root: Path | None = None) -> str:
    graph = load_revision_graph(_resolve_script_location(root or Path.cwd(), None))
    heads = sorted(_head_revisions(graph))
    if len(heads) == 1:
        return heads[0]
    if heads:
        return ",".join(heads)
    return "UNAVAILABLE"


def _resolve_script_location(root: Path, script_location: Path | str | None) -> Path:
    if script_location is not None:
        resolved = Path(script_location)
        return resolved if resolved.is_absolute() else root / resolved
    config_path = root / "alembic.ini"
    if not config_path.exists():
        return root / "alembic"
    parser = configparser.ConfigParser()
    parser.read(config_path)
    configured = parser.get("alembic", "script_location", fallback="alembic")
    path = Path(configured)
    return path if path.is_absolute() else root / path


def _parse_revision_file(path: Path) -> RevisionNode | None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    revision: str | None = None
    down_revisions: tuple[str, ...] = ()
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        target_names = [target.id for target in node.targets if isinstance(target, ast.Name)]
        if "revision" in target_names:
            value = ast.literal_eval(node.value)
            revision = str(value)
        if "down_revision" in target_names:
            down_revisions = _literal_down_revisions(node.value)
    if revision is None:
        return None
    return RevisionNode(revision=revision, down_revisions=down_revisions, path=str(path))


def _literal_down_revisions(node: ast.AST) -> tuple[str, ...]:
    value = ast.literal_eval(node)
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, (list, tuple)):
        return tuple(str(item) for item in value if item is not None)
    return (str(value),)


def _head_revisions(graph: dict[str, RevisionNode]) -> set[str]:
    referenced = {
        down_revision
        for node in graph.values()
        for down_revision in node.down_revisions
        if down_revision in graph
    }
    return set(graph) - referenced


def _is_ancestor_or_self(
    revision: str,
    possible_descendant: str,
    graph: dict[str, RevisionNode],
) -> bool:
    if revision == possible_descendant:
        return True
    seen: set[str] = set()
    stack = [possible_descendant]
    while stack:
        candidate = stack.pop()
        if candidate in seen:
            continue
        seen.add(candidate)
        node = graph.get(candidate)
        if node is None:
            continue
        if revision in node.down_revisions:
            return True
        stack.extend(node.down_revisions)
    return False


def _status_message(status: str, current: list[str], heads: list[str]) -> str:
    current_text = ", ".join(current) if current else "none"
    head_text = ", ".join(heads) if heads else "none"
    if status == ALEMBIC_AT_HEAD:
        return f"Database revision {current_text} is at Alembic head {head_text}."
    if status == ALEMBIC_UPGRADE_REQUIRED:
        return f"Database revision {current_text} is behind Alembic head {head_text}."
    if status == ALEMBIC_MULTIPLE_HEADS:
        return f"Alembic has multiple heads: {head_text}."
    if status == ALEMBIC_ORPHANED_DATABASE_REVISION:
        return f"Database revision {current_text} is not on the configured Alembic ancestry."
    if status == ALEMBIC_CONFIG_MISMATCH:
        return "Alembic configuration or revision files could not be resolved."
    return "Alembic migration state is unknown."
