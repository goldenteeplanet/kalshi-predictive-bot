"""Evidence aging, renewal scheduling, and settlement handoff readiness."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

from scripts.local.phase4oa_aggregate_release_gate import BLOCKED_ON_SETTLEMENT

SCHEMA = "phase4ot.evidence-aging-handoff.v1"
DEPENDENCY_ORDER = (
    "aggregate_certificate",
    "freshness_proof",
    "trusted_time_quorum",
    "recovery_provenance",
    "chaos_tail_baseline",
    "placement_drift",
    "authoritative_settlement",
)
OFFLINE_REFRESHABLE = frozenset(DEPENDENCY_ORDER[:-1])


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(UTC)


def create_evidence_record(
    *,
    evidence_id: str,
    proof_sha256: str,
    observed_at: datetime,
    ttl: timedelta,
    dependencies: tuple[str, ...] = (),
) -> dict[str, object]:
    observed = _utc(observed_at)
    if evidence_id not in DEPENDENCY_ORDER or ttl <= timedelta(0):
        raise ValueError("evidence identity or ttl is invalid")
    if any(dependency not in DEPENDENCY_ORDER for dependency in dependencies):
        raise ValueError("unknown evidence dependency")
    body = {
        "schema": SCHEMA,
        "evidence_id": evidence_id,
        "proof_sha256": proof_sha256,
        "observed_at": observed.isoformat(),
        "expires_at": (observed + ttl).isoformat(),
        "ttl_seconds": int(ttl.total_seconds()),
        "dependencies": list(dependencies),
        "renewal_mode": "OFFLINE" if evidence_id in OFFLINE_REFRESHABLE else "SETTLEMENT_ONLY",
        "safety": _safety(),
    }
    return {**body, "record_sha256": _digest(body)}


def build_handoff_packet(
    records: list[dict[str, object]], *, evaluated_at: datetime
) -> dict[str, object]:
    now = _utc(evaluated_at)
    errors: list[str] = []
    by_id = {row.get("evidence_id"): row for row in records if isinstance(row, dict)}
    if list(by_id) != list(DEPENDENCY_ORDER) or len(records) != len(DEPENDENCY_ORDER):
        errors.append("EVIDENCE_ORDER_OR_COVERAGE_INVALID")
    status, renewal_schedule = [], []
    graph: dict[str, list[str]] = {}
    for evidence_id in DEPENDENCY_ORDER:
        row = by_id.get(evidence_id)
        if not isinstance(row, dict):
            continue
        unsigned = {key: value for key, value in row.items() if key != "record_sha256"}
        if row.get("record_sha256") != _digest(unsigned):
            errors.append(f"{evidence_id}:RECORD_HASH_MISMATCH")
        if row.get("schema") != SCHEMA or row.get("safety") != _safety():
            errors.append(f"{evidence_id}:SCHEMA_OR_SAFETY_INVALID")
        try:
            observed = datetime.fromisoformat(str(row["observed_at"])).astimezone(UTC)
            expires = datetime.fromisoformat(str(row["expires_at"])).astimezone(UTC)
            ttl = int(row["ttl_seconds"])
        except (KeyError, TypeError, ValueError):
            errors.append(f"{evidence_id}:TIMELINE_INVALID")
            continue
        if expires != observed + timedelta(seconds=ttl):
            errors.append(f"{evidence_id}:EXPIRATION_MISMATCH")
        dependencies = row.get("dependencies")
        if not isinstance(dependencies, list) or any(dep not in by_id for dep in dependencies):
            errors.append(f"{evidence_id}:DEPENDENCY_INVALID")
            dependencies = []
        graph[evidence_id] = dependencies
        state = "FRESH" if now < expires else "EXPIRED"
        settlement_dependent = evidence_id == "authoritative_settlement"
        status.append(
            {
                "evidence_id": evidence_id,
                "state": state,
                "expires_at": expires.isoformat(),
                "renewal_mode": row.get("renewal_mode"),
            }
        )
        if state == "EXPIRED":
            if settlement_dependent:
                errors.append("AUTHORITATIVE_SETTLEMENT_UNAVAILABLE")
            else:
                renewal_schedule.append(
                    {
                        "evidence_id": evidence_id,
                        "renew_after": now.isoformat(),
                        "dependencies": dependencies,
                    }
                )
    if _has_cycle(graph):
        errors.append("CIRCULAR_RENEWAL_DEPENDENCY")
    settlement = by_id.get("authoritative_settlement", {})
    settlement_ready = (
        settlement.get("proof_sha256") not in {None, "", "UNAVAILABLE"}
        and next(
            (row["state"] for row in status if row["evidence_id"] == "authoritative_settlement"),
            "EXPIRED",
        )
        == "FRESH"
    )
    if not settlement_ready:
        errors.append("SETTLEMENT_HANDOFF_NOT_READY")
    body = {
        "schema": SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": sorted(set(errors)),
        "evaluated_at": now.isoformat(),
        "status": status,
        "renewal_schedule": renewal_schedule,
        "offline_refreshable": sorted(OFFLINE_REFRESHABLE),
        "settlement_dependent": ["authoritative_settlement"],
        "settlement_ready": settlement_ready,
        "executable": False,
        "blocked_on_september_1_settlement": BLOCKED_ON_SETTLEMENT,
        "paper_position_preserved": "KXRAINAUSM-26AUG-1 contract 1; no additional order authorized",
        "safety": _safety(),
    }
    return {**body, "handoff_sha256": _digest(body)}


def _has_cycle(graph: dict[str, list[str]]) -> bool:
    visiting, visited = set(), set()

    def visit(node: str) -> bool:
        if node in visiting:
            return True
        if node in visited:
            return False
        visiting.add(node)
        if any(visit(dependency) for dependency in graph.get(node, [])):
            return True
        visiting.remove(node)
        visited.add(node)
        return False

    return any(visit(node) for node in graph)


def _safety() -> dict[str, bool]:
    return {
        "offline_only": True,
        "infrastructure_mutation": False,
        "persistence": False,
        "network_access": False,
        "runtime_write": False,
        "paper_order_creation": False,
        "demo_execution": False,
        "live_execution": False,
        "autopilot": False,
    }
