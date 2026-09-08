"""Derive content-bound CI cache identities and validate restored cache manifests."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

SCHEMA = "phase4fe.cache-input.v1"
COMPONENTS = ("lockfiles", "interpreter", "platform", "schemas", "test_configuration")


def _hash(v: Any) -> str:
    if isinstance(v, dict):
        v = {k: x for k, x in v.items() if k != "artifact_hash"}
    return canonical_hash(v)


def _digest(x: Any) -> str:
    if not isinstance(x, str) or len(x) != 64:
        raise ValueError("PHASE4FE_DIGEST_INVALID")
    try:
        int(x, 16)
    except ValueError as e:
        raise ValueError("PHASE4FE_DIGEST_INVALID") from e
    return x


def build_report(p: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(p, dict) or set(p) != {
        "schema",
        "partition_hash",
        "components",
        "restored_manifest",
        "artifact_hash",
    }:
        raise ValueError("PHASE4FE_FIELDS_INVALID")
    if p["schema"] != SCHEMA or p["artifact_hash"] != _hash(p):
        raise ValueError("PHASE4FE_HASH_INVALID")
    c = p["components"]
    if not isinstance(c, dict) or tuple(sorted(c)) != tuple(sorted(COMPONENTS)):
        raise ValueError("PHASE4FE_COMPONENTS_INVALID")
    c = {k: _digest(c[k]) for k in COMPONENTS}
    identity = canonical_hash(c)
    rest = p["restored_manifest"]
    status = "MISS"
    if rest is not None:
        if not isinstance(rest, dict) or set(rest) != {"cache_identity", "payload_hash"}:
            raise ValueError("PHASE4FE_MANIFEST_INVALID")
        _digest(rest["cache_identity"])
        _digest(rest["payload_hash"])
        status = "VALID" if rest["cache_identity"] == identity else "REJECTED_STALE_OR_POISONED"
    r = {
        "schema": "phase4fe.cache-report.v1",
        "phase": "4FE",
        "partition_hash": p["partition_hash"],
        "input_hash": p["artifact_hash"],
        "components": c,
        "cache_identity": identity,
        "restore_status": status,
        "cache_usable": status == "VALID",
        "fallback": "CLEAN_INSTALL_AND_FULL_VALIDATION",
        "cache_written": False,
        "ci_config_changed": False,
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
    a.add_argument("--input", type=Path, required=True)
    a.add_argument("--output", type=Path, required=True)
    x = a.parse_args()
    publish(x.output, build_report(json.loads(x.input.read_text())))


if __name__ == "__main__":
    main()
