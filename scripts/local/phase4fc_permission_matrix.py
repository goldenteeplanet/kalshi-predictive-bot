"""Validate the least-privilege GitHub permission matrix for guarded CI."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

SCHEMA = "phase4fc.permission-matrix.v1"
ROLES = ("ci", "code_scanning", "dependency_updates", "notifications", "codex_review")
LEVELS = {"none": 0, "read": 1, "write": 2}
MAXIMUM = {
    "ci": {"contents": "read", "checks": "write"},
    "code_scanning": {"contents": "read", "security-events": "write"},
    "dependency_updates": {"contents": "write", "pull-requests": "write"},
    "notifications": {"actions": "read", "checks": "read", "contents": "read"},
    "codex_review": {"contents": "read", "pull-requests": "write"},
}
FORBIDDEN = {
    "administration",
    "organization-administration",
    "members",
    "organization-secrets",
    "actions-secrets",
    "environments",
}


def _hash(v: Any) -> str:
    if isinstance(v, dict):
        v = {k: x for k, x in v.items() if k != "artifact_hash"}
    return canonical_hash(v)


def build_report(p: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(p, dict) or set(p) != {
        "schema",
        "recommendation_hash",
        "roles",
        "artifact_hash",
    }:
        raise ValueError("PHASE4FC_FIELDS_INVALID")
    if p["schema"] != SCHEMA or p["artifact_hash"] != _hash(p):
        raise ValueError("PHASE4FC_HASH_INVALID")
    if not isinstance(p["recommendation_hash"], str) or len(p["recommendation_hash"]) != 64:
        raise ValueError("PHASE4FC_LINEAGE_INVALID")
    roles = p["roles"]
    if not isinstance(roles, dict) or set(roles) != set(ROLES):
        raise ValueError("PHASE4FC_ROLES_INVALID")
    normalized = {}
    for role in ROLES:
        grants = roles[role]
        if not isinstance(grants, dict) or not grants:
            raise ValueError("PHASE4FC_GRANTS_INVALID")
        for permission, level in grants.items():
            if permission in FORBIDDEN:
                raise ValueError("PHASE4FC_BROAD_PERMISSION_REFUSED")
            if permission not in MAXIMUM[role] or level not in LEVELS:
                raise ValueError("PHASE4FC_PERMISSION_INVALID")
            if LEVELS[level] > LEVELS[MAXIMUM[role][permission]]:
                raise ValueError("PHASE4FC_EXCESS_PERMISSION_REFUSED")
        normalized[role] = dict(sorted(grants.items()))
    report = {
        "schema": "phase4fc.permission-report.v1",
        "phase": "4FC",
        "recommendation_hash": p["recommendation_hash"],
        "input_hash": p["artifact_hash"],
        "roles": normalized,
        "organization_scope_granted": False,
        "administration_granted": False,
        "settings_changed": False,
        "apps_authorized": False,
        "production_database_mutated": False,
        "services_controlled": False,
        "exchange_requests_made": False,
        "execution_authorized": False,
    }
    report["artifact_hash"] = _hash(report)
    return report


def publish(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as h:
            json.dump(payload, h, sort_keys=True, separators=(",", ":"))
            h.write("\n")
            h.flush()
            os.fsync(h.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    a = ap.parse_args()
    publish(a.output, build_report(json.loads(a.input.read_text(encoding="utf-8"))))


if __name__ == "__main__":
    main()
