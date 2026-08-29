"""Offline replica placement, correlated-failure, and capacity simulation."""

from __future__ import annotations

import copy
import hashlib
import json

SCHEMA = "phase4my.replica-placement-plan.v1"
DIMENSIONS = ("provider", "region", "zone", "power_domain", "hidden_dependency")


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def plan_placements(jobs: object, nodes: object) -> dict[str, object]:
    errors: list[str] = []
    if not isinstance(jobs, list) or not isinstance(nodes, list):
        return _result(["INPUT_INVALID"], [], [])
    working = copy.deepcopy(nodes)
    initial_generations = {
        node.get("node_id"): node.get("reservation_generation") for node in working
    }
    source_hash = _digest({"jobs": jobs, "nodes": nodes})
    placements: list[dict[str, object]] = []
    ordered_jobs = sorted(
        jobs, key=lambda row: (-int(row.get("priority", 0)), str(row.get("archive_id")))
    )
    for job in ordered_jobs:
        size = job.get("shard_size")
        if type(size) is not int or size <= 0:
            errors.append(f"JOB_{job.get('archive_id')}_SIZE_INVALID")
            continue
        eligible = sorted(
            [
                node
                for node in working
                if node.get("maintenance") is not True
                and type(node.get("capacity")) is int
                and type(node.get("used")) is int
                and node["capacity"] - node["used"] >= size
                and initial_generations.get(node.get("node_id"))
                == job.get("expected_reservation_generation")
            ],
            key=lambda row: (row["used"] / row["capacity"], str(row.get("node_id"))),
        )
        chosen: list[dict[str, object]] = []
        for node in eligible:
            if all(
                node.get(dimension) not in {item.get(dimension) for item in chosen}
                for dimension in DIMENSIONS
            ):
                chosen.append(node)
            if len(chosen) == 3:
                break
        if len(chosen) != 3:
            errors.append(f"JOB_{job.get('archive_id')}_SURVIVABLE_CAPACITY_UNAVAILABLE")
            continue
        for shard_index, node in enumerate(chosen):
            node["used"] += size
            node["reservation_generation"] += 1
            placements.append(
                {
                    "archive_id": job.get("archive_id"),
                    "shard_generation": job.get("shard_generation"),
                    "custody_anchor_sha256": job.get("custody_anchor_sha256"),
                    "priority": job.get("priority"),
                    "shard_index": shard_index,
                    "node_id": node.get("node_id"),
                    **{dimension: node.get(dimension) for dimension in DIMENSIONS},
                    "reserved_capacity": size,
                    "reservation_generation_after": node["reservation_generation"],
                    "durability": "DURABLE",
                }
            )
    result = _result(sorted(set(errors)), placements, working)
    result["source_unchanged"] = _digest({"jobs": jobs, "nodes": nodes}) == source_hash
    result["fairness_order"] = [row.get("archive_id") for row in ordered_jobs]
    result["plan_sha256"] = _digest(result)
    return result


def audit_survivability(placements: object) -> dict[str, object]:
    errors: list[str] = []
    records = []
    if not isinstance(placements, list):
        placements = []
        errors.append("PLACEMENTS_INVALID")
    by_archive: dict[object, list[dict[str, object]]] = {}
    for row in placements:
        if isinstance(row, dict):
            by_archive.setdefault(row.get("archive_id"), []).append(row)
    for archive_id, rows in sorted(by_archive.items(), key=lambda item: str(item[0])):
        if len(rows) != 3 or len({row.get("shard_index") for row in rows}) != 3:
            errors.append(f"ARCHIVE_{archive_id}_SHARD_SET_INVALID")
        scenarios = []
        for dimension in DIMENSIONS:
            values = {row.get(dimension) for row in rows}
            if len(values) != 3:
                errors.append(f"ARCHIVE_{archive_id}_{dimension.upper()}_CORRELATED")
            for value in sorted(values, key=str):
                survivors = sum(row.get(dimension) != value for row in rows)
                scenarios.append(
                    {"dimension": dimension, "failed_value": value, "survivors": survivors}
                )
                if survivors < 2:
                    errors.append(f"ARCHIVE_{archive_id}_TWO_OF_THREE_NOT_SURVIVABLE")
        records.append({"archive_id": archive_id, "scenarios": scenarios})
    if not records:
        errors.append("NO_ARCHIVES_AUDITED")
    result = {
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": sorted(set(errors)),
        "records": records,
        "safety": _safety(),
    }
    result["survivability_sha256"] = _digest(result)
    return result


def simulate_allocation_crash(plan: dict[str, object], *, durable_count: int) -> dict[str, object]:
    placements = copy.deepcopy(plan.get("placements", []))
    durable = placements[:durable_count]
    prepared = placements[durable_count:]
    result = {
        "verdict": "PASS",
        "durable_placements": durable,
        "prepared_discarded": len(prepared),
        "durable_lost": 0,
        "resume_archive_ids": sorted({row["archive_id"] for row in prepared}),
        "safety": _safety(),
    }
    result["recovery_sha256"] = _digest(result)
    return result


def detect_reservation_race(proposals: object, *, current_generation: int) -> dict[str, object]:
    outcomes = []
    generation = current_generation
    for proposal in proposals if isinstance(proposals, list) else []:
        if proposal.get("expected_generation") == generation:
            outcomes.append("ACCEPTED")
            generation += 1
        else:
            outcomes.append("REFUSED_STALE_RESERVATION")
    result = {
        "verdict": "PASS",
        "outcomes": outcomes,
        "accepted_count": outcomes.count("ACCEPTED"),
        "final_generation": generation,
        "safety": _safety(),
    }
    result["race_sha256"] = _digest(result)
    return result


def _result(
    errors: list[str], placements: list[dict[str, object]], nodes: list[dict[str, object]]
) -> dict[str, object]:
    return {
        "schema": SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": errors,
        "placements": placements,
        "resulting_nodes": nodes,
        "infrastructure_changes": False,
        "safety": _safety(),
    }


def _safety() -> dict[str, bool]:
    return {
        "simulation_only": True,
        "infrastructure_write": False,
        "filesystem_write": False,
        "network_access": False,
        "runtime_write": False,
        "service_control": False,
        "order_capability": False,
    }
