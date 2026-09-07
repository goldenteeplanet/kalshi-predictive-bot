"""Fail-closed certification gate for the complete 100-phase acceleration program."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

SCHEMA = "phase4fk.certification-input.v1"
TERMINAL = "PAPER_ONLY_TIME_TO_TRADE_ACCELERATION_CERTIFIED"


def required_phases() -> tuple[str, ...]:
    values = []
    for first in "BCDEF":
        start = "P" if first == "B" else "A"
        end = "J" if first == "F" else "Z"
        values.extend(f"4{first}{chr(i)}" for i in range(ord(start), ord(end) + 1))
    return tuple(values)


REQUIRED = required_phases() + ("4FK",)


def _hash(v: Any) -> str:
    if isinstance(v, dict):
        v = {k: x for k, x in v.items() if k != "artifact_hash"}
    return canonical_hash(v)


def _digest(x: Any, code: str) -> str:
    if not isinstance(x, str) or len(x) != 64:
        raise ValueError(code)
    try:
        int(x, 16)
    except ValueError as e:
        raise ValueError(code) from e
    return x


def build_report(p: dict[str, Any]) -> dict[str, Any]:
    fields = {
        "schema",
        "phases",
        "proofs",
        "integrations",
        "residual_risks",
        "focused_tests_passed",
        "cumulative_tests_passed",
        "expected_skips",
        "artifact_hash",
    }
    if not isinstance(p, dict) or set(p) != fields:
        raise ValueError("PHASE4FK_FIELDS_INVALID")
    if p["schema"] != SCHEMA or p["artifact_hash"] != _hash(p):
        raise ValueError("PHASE4FK_HASH_INVALID")
    phases = p["phases"]
    if not isinstance(phases, list) or [
        x.get("phase") for x in phases if isinstance(x, dict)
    ] != list(REQUIRED):
        raise ValueError("PHASE4FK_PHASE_SET_OR_ORDER_INVALID")
    previous = None
    for row in phases:
        if set(row) != {"phase", "artifact_hash", "previous_artifact_hash"}:
            raise ValueError("PHASE4FK_PHASE_FIELDS_INVALID")
        digest = _digest(row["artifact_hash"], "PHASE4FK_PHASE_HASH_INVALID")
        if row["previous_artifact_hash"] != previous:
            raise ValueError("PHASE4FK_LINEAGE_BROKEN")
        previous = digest
    proofs = p["proofs"]
    required_proofs = {
        "no_unexplained_mutation",
        "no_high_threat",
        "reproducible_build_ci",
        "rollback_proof",
        "replay_proof",
        "airgapped_acceptance",
        "readiness_passed",
        "runtime_invariants_preserved",
    }
    if (
        not isinstance(proofs, dict)
        or set(proofs) != required_proofs
        or any(v is not True for v in proofs.values())
    ):
        raise ValueError("PHASE4FK_PROOF_FAILED_OR_MISSING")
    integrations = p["integrations"]
    if (
        not isinstance(integrations, list)
        or not integrations
        or any(
            x not in {"SAFE_CONFIGURED_WITH_EXPLICIT_APPROVAL", "RECOMMENDATION_ONLY"}
            for x in integrations
        )
    ):
        raise ValueError("PHASE4FK_INTEGRATION_DISPOSITION_INVALID")
    risks = p["residual_risks"]
    if not isinstance(risks, list):
        raise ValueError("PHASE4FK_RISKS_INVALID")
    normalized = []
    for x in risks:
        if (
            not isinstance(x, dict)
            or set(x) != {"id", "severity", "status", "mitigation"}
            or x["severity"] not in {"LOW", "MEDIUM", "HIGH"}
            or x["status"] not in {"ACCEPTED", "DEFERRED", "RESOLVED"}
        ):
            raise ValueError("PHASE4FK_RISK_INVALID")
        if x["severity"] == "HIGH" and x["status"] != "RESOLVED":
            raise ValueError("PHASE4FK_UNRESOLVED_HIGH_RISK")
        normalized.append(x)
    for field in ("focused_tests_passed", "cumulative_tests_passed", "expected_skips"):
        if isinstance(p[field], bool) or not isinstance(p[field], int) or p[field] < 0:
            raise ValueError("PHASE4FK_TEST_COUNT_INVALID")
    if p["focused_tests_passed"] < 1 or p["cumulative_tests_passed"] < 1:
        raise ValueError("PHASE4FK_TESTS_NOT_PASSING")
    r = {
        "schema": "phase4fk.final-certification.v1",
        "phase": "4FK",
        "input_hash": p["artifact_hash"],
        "phase_count": len(phases),
        "phase_index": [x["phase"] for x in phases],
        "lineage_head_hash": previous,
        "proofs": proofs,
        "integrations": integrations,
        "residual_risks": sorted(normalized, key=lambda x: x["id"]),
        "validation": {
            "focused_tests_passed": p["focused_tests_passed"],
            "cumulative_tests_passed": p["cumulative_tests_passed"],
            "expected_skips": p["expected_skips"],
        },
        "offline_and_paper_only_acceleration_certified": True,
        "live_trading_authorized": False,
        "paper_order_creation_enabled": False,
        "exchange_enabled": False,
        "production_database_mutated": False,
        "services_controlled": False,
        "terminal_state": TERMINAL,
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
    r = build_report(json.loads(x.input.read_text()))
    publish(x.output, r)
    print(r["terminal_state"])


if __name__ == "__main__":
    main()
