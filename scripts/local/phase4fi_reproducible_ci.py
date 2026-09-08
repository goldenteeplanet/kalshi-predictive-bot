"""Audit pinned CI inputs and derive a reproducible logical environment identity."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

PIN = re.compile(r"^[A-Za-z0-9_.-]+(?:\[[A-Za-z0-9_,.-]+\])?==[^\s]+$")


def _hash(v: Any) -> str:
    return canonical_hash(v)


def audit(root: Path, security_hash: str) -> dict[str, Any]:
    paths = [
        Path("requirements-ci.lock"),
        Path("pyproject.toml"),
        Path(".github/workflows/phase4-required-safety.yml"),
    ]
    files = []
    for rel in paths:
        path = root / rel
        if not path.is_file():
            raise ValueError("PHASE4FI_REQUIRED_INPUT_MISSING")
        files.append({"path": rel.as_posix(), "sha256": _hash(path.read_bytes().hex())})
    lines = [
        x.strip()
        for x in (root / paths[0]).read_text().splitlines()
        if x.strip() and not x.lstrip().startswith("#")
    ]
    if (
        len(lines) != len(set(x.lower() for x in lines))
        or not lines
        or any(not PIN.fullmatch(x) for x in lines)
    ):
        raise ValueError("PHASE4FI_DEPENDENCY_NOT_EXACTLY_PINNED")
    workflow = (root / paths[2]).read_text()
    if 'python-version: "3.11.9"' not in workflow or "-c requirements-ci.lock" not in workflow:
        raise ValueError("PHASE4FI_WORKFLOW_NOT_BOUND")
    if any(
        not re.fullmatch(r"[0-9a-f]{40}", x)
        for x in re.findall(r"uses:\s+[^@\s]+@([^\s]+)", workflow)
    ):
        raise ValueError("PHASE4FI_ACTION_NOT_PINNED")
    identity = _hash(
        {"python": "3.11.9", "files": files, "dependencies": sorted(lines, key=str.lower)}
    )
    r = {
        "schema": "phase4fi.reproducible-ci-report.v1",
        "phase": "4FI",
        "security_audit_hash": security_hash,
        "python": "3.11.9",
        "files": files,
        "direct_dependencies": sorted(lines, key=str.lower),
        "environment_identity": identity,
        "logical_equivalence_required": True,
        "remote_ci_executed": False,
        "dependencies_installed": False,
        "files_changed_by_audit": False,
        "production_database_mutated": False,
        "services_controlled": False,
        "execution_authorized": False,
    }
    r["artifact_hash"] = _hash(r)
    return r


def main():
    a = argparse.ArgumentParser()
    a.add_argument("--root", type=Path, required=True)
    a.add_argument("--security-hash", required=True)
    a.add_argument("--output", type=Path, required=True)
    x = a.parse_args()
    x.output.write_text(
        json.dumps(audit(x.root, x.security_hash), sort_keys=True, separators=(",", ":")) + "\n"
    )


if __name__ == "__main__":
    main()
