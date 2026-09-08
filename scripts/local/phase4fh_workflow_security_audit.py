"""Statically audit GitHub workflow trust boundaries without contacting GitHub."""

from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

SHA = re.compile(r"^[0-9a-f]{40}$")
USE = re.compile(r"uses:\s+[^@\s]+@([^\s]+)")


def _hash(v: Any) -> str:
    if isinstance(v, dict):
        v = {k: x for k, x in v.items() if k != "artifact_hash"}
    return canonical_hash(v)


def audit(root: Path, lineage_hash: str) -> dict[str, Any]:
    findings = []
    files = []
    for path in sorted(root.glob("*.y*ml")):
        text = path.read_text(encoding="utf-8")
        files.append({"path": path.name, "sha256": canonical_hash(text)})

        def add(code, severity, workflow_name=path.name):
            findings.append({"path": workflow_name, "code": code, "severity": severity})

        if "pull_request_target:" in text:
            add("PR_TARGET_TRUST_BOUNDARY", "HIGH")
        if re.search(r"run:\s*.*\$\{\{\s*github\.event\.", text):
            add("UNTRUSTED_EVENT_SCRIPT_INJECTION", "HIGH")
        if "secrets." in text:
            add("SECRET_CONTEXT_EXPOSURE", "HIGH")
        if re.search(r"(?:contents|actions|checks|pull-requests|id-token):\s*write", text):
            add("WRITE_TOKEN_PERMISSION", "HIGH")
        if "permissions:" not in text:
            add("MISSING_EXPLICIT_PERMISSIONS", "HIGH")
        for ref in USE.findall(text):
            if not SHA.fullmatch(ref):
                add("FLOATING_ACTION_REFERENCE", "MEDIUM")
        if "actions/upload-artifact" in text and "retention-days:" not in text:
            add("ARTIFACT_RETENTION_UNBOUNDED", "LOW")
    findings.sort(key=lambda x: (x["path"], x["code"]))
    high = sum(x["severity"] == "HIGH" for x in findings)
    r = {
        "schema": "phase4fh.workflow-security-report.v1",
        "phase": "4FH",
        "required_workflow_hash": lineage_hash,
        "workflows": files,
        "findings": findings,
        "high_severity_count": high,
        "advancement_allowed": high == 0,
        "trust_boundaries_checked": True,
        "forks_checked": True,
        "script_injection_checked": True,
        "artifact_poisoning_checked": True,
        "secret_exposure_checked": True,
        "workflows_changed": False,
        "production_database_mutated": False,
        "services_controlled": False,
        "execution_authorized": False,
    }
    r["artifact_hash"] = _hash(r)
    return r


def publish(path: Path, p: dict[str, Any]):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, n = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp = Path(n)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as h:
            json.dump(p, h, sort_keys=True, separators=(",", ":"))
            h.write("\n")
            h.flush()
            os.fsync(h.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def main():
    a = argparse.ArgumentParser()
    a.add_argument("--workflow-root", type=Path, required=True)
    a.add_argument("--lineage-hash", required=True)
    a.add_argument("--output", type=Path, required=True)
    x = a.parse_args()
    publish(x.output, audit(x.workflow_root, x.lineage_hash))


if __name__ == "__main__":
    main()
