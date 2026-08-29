from __future__ import annotations

import copy

from scripts.local.phase4oq_replica_placement import place_replica
from scripts.local.phase4or_placement_migration import conduct_migration, detect_diversity_drift
from tests.test_phase4oq_replica_placement import HISTORY, _pairs, _placements

APPROVERS = {"reviewer-a", "reviewer-b", "reviewer-c"}


def _destination(**overrides):
    values = {
        "replica_id": "replica-6",
        "history_sha256": HISTORY,
        "host": "host-6",
        "filesystem": "fs-6",
        "administrator": "admin-6",
        "failure_domain": "zone-6",
    }
    values.update(overrides)
    return place_replica(**values)


def _ceremony(source=None, destination=None, **overrides):
    values = {
        "remove_replica_id": "replica-1",
        "trusted_history_sha256": HISTORY,
        "quorum": 2,
        "required_multi_losses": _pairs(),
        "approvers": {"reviewer-a", "reviewer-b"},
        "allowed_approvers": APPROVERS,
    }
    values.update(overrides)
    return conduct_migration(source or _placements(), destination or _destination(), **values)


def test_add_before_remove_migration_passes_every_stage() -> None:
    result = _ceremony()
    assert result["verdict"] == "PASS"
    assert [row["stage"] for row in result["stages"]] == [
        "SOURCE",
        "ADD_DESTINATION",
        "REMOVE_SOURCE",
    ]
    assert all(row["verdict"] == "PASS" for row in result["stages"])
    assert len(result["stages"][1]["placement_sha256s"]) == 6
    assert len(result["final_placements"]) == 5


def test_independent_approval_and_source_destination_binding_are_required() -> None:
    assert "INDEPENDENT_APPROVAL_MISSING" in _ceremony(approvers={"reviewer-a"})["errors"]
    assert "SOURCE_REPLICA_NOT_FOUND" in _ceremony(remove_replica_id="missing")["errors"]
    tampered = _destination()
    tampered["host"] = "forged"
    assert "DESTINATION_HASH_INVALID" in _ceremony(destination=tampered)["errors"]


def test_hidden_dependency_convergence_fails_intermediate_and_final_audits() -> None:
    destination = _destination(host="host-2")
    result = _ceremony(destination=destination)
    assert result["verdict"] == "REFUSE"
    assert "ADD_DESTINATION_DIVERSITY_OR_SURVIVABILITY_FAILED" in result["errors"]


def test_continuous_drift_check_passes_exact_certified_final_set() -> None:
    ceremony = _ceremony()
    result = detect_diversity_drift(
        ceremony,
        ceremony["final_placements"],
        trusted_history_sha256=HISTORY,
        quorum=2,
        required_multi_losses=_pairs(),
    )
    assert result["verdict"] == "PASS"
    assert result["recovery_claim_allowed"] is True
    assert result["state"] == "READY"


def test_post_migration_dimension_or_set_drift_revokes_claim_and_freezes() -> None:
    ceremony = _ceremony()
    changed = copy.deepcopy(ceremony["final_placements"])
    changed[0]["administrator"] = changed[1]["administrator"]
    result = detect_diversity_drift(
        ceremony,
        changed,
        trusted_history_sha256=HISTORY,
        quorum=2,
        required_multi_losses=_pairs(),
    )
    assert result["verdict"] == "REFUSE"
    assert result["recovery_claim_allowed"] is False
    assert result["state"] == "FROZEN"
    assert "PLACEMENT_SET_DRIFT" in result["errors"]
    assert "CONTINUOUS_DIVERSITY_AUDIT_FAILED" in result["errors"]


def test_tampered_ceremony_and_replica_removal_fail_closed() -> None:
    ceremony = _ceremony()
    tampered = copy.deepcopy(ceremony)
    tampered["remove_replica_id"] = "replica-2"
    current = ceremony["final_placements"][:-1]
    result = detect_diversity_drift(
        tampered,
        current,
        trusted_history_sha256=HISTORY,
        quorum=2,
        required_multi_losses=_pairs(),
    )
    assert "CEREMONY_HASH_MISMATCH" in result["errors"]
    assert result["state"] == "FROZEN"


def test_migration_and_drift_checks_are_deterministic_input_preserving_and_safe() -> None:
    source, destination = _placements(), _destination()
    original = copy.deepcopy((source, destination))
    first = _ceremony(source, destination)
    second = _ceremony(source, destination)
    assert first == second
    assert (source, destination) == original
    assert first["safety"]["offline_only"] is True
    assert all(value is False for key, value in first["safety"].items() if key != "offline_only")
