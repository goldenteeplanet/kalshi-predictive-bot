from __future__ import annotations

import copy

from scripts.local.phase4my_replica_placement import (
    audit_survivability,
    detect_reservation_race,
    plan_placements,
    simulate_allocation_crash,
)


def _nodes():
    return [
        {
            "node_id": "n-a",
            "provider": "p-a",
            "region": "r-a",
            "zone": "z-a",
            "power_domain": "power-a",
            "hidden_dependency": "backbone-a",
            "capacity": 100,
            "used": 0,
            "maintenance": False,
            "reservation_generation": 1,
        },
        {
            "node_id": "n-b",
            "provider": "p-b",
            "region": "r-b",
            "zone": "z-b",
            "power_domain": "power-b",
            "hidden_dependency": "backbone-b",
            "capacity": 100,
            "used": 0,
            "maintenance": False,
            "reservation_generation": 1,
        },
        {
            "node_id": "n-c",
            "provider": "p-c",
            "region": "r-c",
            "zone": "z-c",
            "power_domain": "power-c",
            "hidden_dependency": "backbone-c",
            "capacity": 100,
            "used": 0,
            "maintenance": False,
            "reservation_generation": 1,
        },
        {
            "node_id": "n-d",
            "provider": "p-d",
            "region": "r-d",
            "zone": "z-d",
            "power_domain": "power-d",
            "hidden_dependency": "backbone-d",
            "capacity": 100,
            "used": 60,
            "maintenance": False,
            "reservation_generation": 1,
        },
    ]


def _job(name="archive-a", priority=10, size=20):
    return {
        "archive_id": name,
        "priority": priority,
        "shard_size": size,
        "shard_generation": 4,
        "custody_anchor_sha256": "a" * 64,
        "expected_reservation_generation": 1,
    }


def test_deterministic_plan_is_independent_and_single_failure_survivable() -> None:
    first = plan_placements([_job()], _nodes())
    assert first == plan_placements([_job()], _nodes())
    assert first["verdict"] == "PASS"
    assert len(first["placements"]) == 3
    audit = audit_survivability(first["placements"])
    assert audit["verdict"] == "PASS"
    assert all(item["survivors"] >= 2 for item in audit["records"][0]["scenarios"])


def test_hidden_shared_dependency_and_correlated_domains_refuse() -> None:
    nodes = _nodes()
    for node in nodes:
        node["hidden_dependency"] = "shared-backbone"
    result = plan_placements([_job()], nodes)
    assert result["verdict"] == "REFUSE"
    assert "SURVIVABLE_CAPACITY_UNAVAILABLE" in " ".join(result["errors"])
    placements = plan_placements([_job()], _nodes())["placements"]
    placements[1]["power_domain"] = placements[0]["power_domain"]
    assert "POWER_DOMAIN_CORRELATED" in " ".join(audit_survivability(placements)["errors"])


def test_insufficient_capacity_and_maintenance_drain_refuse() -> None:
    nodes = _nodes()
    nodes[0]["used"] = 95
    nodes[1]["maintenance"] = True
    assert plan_placements([_job(size=20)], nodes)["verdict"] == "REFUSE"


def test_stale_reservations_and_concurrent_races_linearize_once() -> None:
    nodes = _nodes()
    nodes[0]["reservation_generation"] = 2
    nodes[1]["reservation_generation"] = 2
    assert plan_placements([_job()], nodes)["verdict"] == "REFUSE"
    proposals = [{"expected_generation": 1}, {"expected_generation": 1}]
    race = detect_reservation_race(proposals, current_generation=1)
    assert race["outcomes"] == ["ACCEPTED", "REFUSED_STALE_RESERVATION"]


def test_partial_allocation_crash_preserves_durable_and_discards_prepared() -> None:
    plan = plan_placements([_job()], _nodes())
    result = simulate_allocation_crash(plan, durable_count=1)
    assert result["verdict"] == "PASS"
    assert len(result["durable_placements"]) == 1
    assert result["prepared_discarded"] == 2
    assert result["durable_lost"] == 0


def test_replanning_is_deterministic_after_capacity_change() -> None:
    nodes = _nodes()
    nodes[0]["used"] = 90
    first = plan_placements([_job()], nodes)
    second = plan_placements([_job()], copy.deepcopy(nodes))
    assert first == second
    assert {row["node_id"] for row in first["placements"]} == {"n-b", "n-c", "n-d"}


def test_priority_order_is_stable_and_high_priority_allocates_first() -> None:
    jobs = [_job("low", priority=1, size=30), _job("high", priority=10, size=30)]
    result = plan_placements(jobs, _nodes())
    assert result["fairness_order"] == ["high", "low"]
    assert result["verdict"] == "PASS"
    assert [row["archive_id"] for row in result["placements"][:3]] == ["high"] * 3


def test_equal_priority_fairness_uses_stable_archive_order() -> None:
    result = plan_placements([_job("b"), _job("a")], _nodes())
    assert result["fairness_order"] == ["a", "b"]
    assert result["source_unchanged"] is True


def test_shard_generation_and_custody_are_bound_to_every_placement() -> None:
    placements = plan_placements([_job()], _nodes())["placements"]
    assert all(row["shard_generation"] == 4 for row in placements)
    assert all(row["custody_anchor_sha256"] == "a" * 64 for row in placements)


def test_planner_has_no_infrastructure_or_operational_capability() -> None:
    result = plan_placements([_job()], _nodes())
    assert result["infrastructure_changes"] is False
    safety = result["safety"]
    assert safety["simulation_only"] is True
    assert all(value is False for key, value in safety.items() if key != "simulation_only")
