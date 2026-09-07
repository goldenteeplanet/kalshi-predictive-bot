"""Audit supplied source artifacts for deterministic algorithmic hotspot signals."""

from __future__ import annotations

import argparse
import ast
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

INPUT_SCHEMA = "phase4bx.source-inventory.v1"
REPORT_SCHEMA = "phase4bx.algorithmic-hotspot-audit.v1"
CATEGORIES = (
    "NESTED_ITERATION",
    "REPEATED_PARSING",
    "REPEATED_HASHING",
    "REDUNDANT_SORTING",
    "EXCESSIVE_SERIALIZATION",
)
CALL_CATEGORIES = {
    "json.loads": "REPEATED_PARSING",
    "datetime.fromisoformat": "REPEATED_PARSING",
    "canonical_hash": "REPEATED_HASHING",
    "sorted": "REDUNDANT_SORTING",
    "list.sort": "REDUNDANT_SORTING",
    "json.dumps": "EXCESSIVE_SERIALIZATION",
}


def _hash(payload: Any) -> str:
    if isinstance(payload, dict):
        payload = {key: value for key, value in payload.items() if key != "artifact_hash"}
    return canonical_hash(payload)


def _call_name(node: ast.Call) -> str:
    target = node.func
    if isinstance(target, ast.Name):
        return target.id
    if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name):
        return f"{target.value.id}.{target.attr}"
    return target.attr if isinstance(target, ast.Attribute) else ""


def _audit_source(source: str) -> tuple[list[dict[str, Any]], dict[str, int]]:
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise ValueError("PHASE4BX_SOURCE_SYNTAX_INVALID") from exc
    findings: list[dict[str, Any]] = []
    counts = {category: 0 for category in CATEGORIES}
    parents = {
        child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)
    }
    loops = (ast.For, ast.AsyncFor, ast.comprehension)
    for node in ast.walk(tree):
        if isinstance(node, loops):
            ancestor = parents.get(node)
            while ancestor is not None:
                if isinstance(ancestor, loops):
                    counts["NESTED_ITERATION"] += 1
                    findings.append(
                        {
                            "category": "NESTED_ITERATION",
                            "line": getattr(node, "lineno", 0),
                            "symbol": type(node).__name__,
                        }
                    )
                    break
                ancestor = parents.get(ancestor)
        if isinstance(node, ast.Call):
            name = _call_name(node)
            category = CALL_CATEGORIES.get(name)
            if category:
                counts[category] += 1
                findings.append({"category": category, "line": node.lineno, "symbol": name})
    ordered = sorted(findings, key=lambda row: (row["line"], row["category"], row["symbol"]))
    return ordered, counts


def build_report(inventory: dict[str, Any]) -> dict[str, Any]:
    if (
        inventory.get("schema") != INPUT_SCHEMA
        or inventory.get("artifact_hash") != _hash(inventory)
    ):
        raise ValueError("PHASE4BX_INPUT_SCHEMA_OR_HASH_INVALID")
    sources = inventory.get("sources")
    if not isinstance(sources, list) or not sources:
        raise ValueError("PHASE4BX_SOURCES_MISSING")
    names = [row.get("name") for row in sources if isinstance(row, dict)]
    if names != sorted(names) or len(names) != len(set(names)):
        raise ValueError("PHASE4BX_SOURCE_ORDER_OR_DUPLICATE_INVALID")
    reports = []
    totals = {category: 0 for category in CATEGORIES}
    for row in sources:
        if set(row) != {"name", "source", "source_hash"}:
            raise ValueError("PHASE4BX_SOURCE_FIELDS_INVALID")
        if not isinstance(row["name"], str) or not row["name"]:
            raise ValueError("PHASE4BX_SOURCE_NAME_INVALID")
        if (
            not isinstance(row["source"], str)
            or row["source_hash"] != canonical_hash(row["source"])
        ):
            raise ValueError("PHASE4BX_SOURCE_HASH_INVALID")
        findings, counts = _audit_source(row["source"])
        for category in CATEGORIES:
            totals[category] += counts[category]
        reports.append(
            {
                "name": row["name"],
                "source_hash": row["source_hash"],
                "counts": counts,
                "findings": findings,
            }
        )
    ranked = sorted(totals.items(), key=lambda item: (-item[1], CATEGORIES.index(item[0])))
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "phase": "4BX",
        "input_hash": inventory["artifact_hash"],
        "source_reports": reports,
        "category_totals": totals,
        "ranked_categories": [
            {"category": category, "finding_count": count} for category, count in ranked
        ],
        "finding_count": sum(totals.values()),
        "audit_only": True,
        "source_mutations_applied": 0,
        "execution_authorized": False,
    }
    report["artifact_hash"] = _hash(report)
    return report


def inventory_from_paths(paths: list[Path]) -> dict[str, Any]:
    resolved = sorted(paths, key=lambda path: path.as_posix())
    if not resolved or len({path.as_posix() for path in resolved}) != len(resolved):
        raise ValueError("PHASE4BX_PATHS_MISSING_OR_DUPLICATE")
    sources = []
    for path in resolved:
        source = path.read_text(encoding="utf-8")
        sources.append(
            {"name": path.as_posix(), "source": source, "source_hash": canonical_hash(source)}
        )
    inventory: dict[str, Any] = {"schema": INPUT_SCHEMA, "sources": sources}
    inventory["artifact_hash"] = _hash(inventory)
    return inventory


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
    parser.add_argument("--source", action="append", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = build_report(inventory_from_paths(args.source))
    publish(args.output, report)
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
