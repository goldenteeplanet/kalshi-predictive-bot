from __future__ import annotations

import copy

from scripts.local.phase4ow_checkpoint_redundancy import create_replica
from scripts.local.phase4ox_checkpoint_placement import (
    certify_interruption_matrix,
    place_checkpoint_replica,
)
from tests.test_phase4ov_renewal_resume import _fixture

ALLOWED = {f"replica-{index}" for index in range(1, 6)}


def _placements(checkpoints):
    return [
        place_checkpoint_replica(
            create_replica(f"replica-{index}", checkpoints),
            host=f"host-{index}",
            filesystem=f"fs-{index}",
            administrator=f"admin-{index}",
            power_domain=f"power-{index}",
            runtime_domain="wsl" if index == 1 else f"native-{index}",
            failure_domain=f"zone-{index}",
        )
        for index in range(1, 6)
    ]


def _scenarios():
    return [
        {"name": "PROCESS_CRASH", "losses": [], "survivable": True},
        {"name": "WSL_SHUTDOWN", "losses": [["runtime_domain", "wsl"]], "survivable": True},
        {"name": "HOST_RESTART", "losses": [["host", "host-2"]], "survivable": True},
        {"name": "FILESYSTEM_LOSS", "losses": [["filesystem", "fs-3"]], "survivable": True},
        {
            "name": "CORRELATED_POWER_ZONE",
            "losses": [["power_domain", "power-4"], ["failure_domain", "zone-5"]],
            "survivable": True,
        },
        {
            "name": "CATASTROPHIC_FOUR_DOMAIN",
            "losses": [
                ["host", "host-1"],
                ["host", "host-2"],
                ["host", "host-3"],
                ["host", "host-4"],
            ],
            "survivable": False,
        },
    ]


def _certify(placements=None, scenarios=None):
    records, _, checkpoints = _fixture()
    return certify_interruption_matrix(
        records,
        checkpoints,
        placements or _placements(checkpoints),
        scenarios or _scenarios(),
        allowed_replicas=ALLOWED,
        quorum=2,
    )


def test_all_prefixes_and_required_interruptions_certify() -> None:
    result = _certify()
    assert result["verdict"] == "PASS"
    assert result["prefix_count"] == 7
    assert result["scenario_count"] == 6
    assert result["case_count"] == 42
    assert result["converged_orchestration_sha256"] is not None


def test_survivable_cases_recover_and_catastrophic_cases_refuse() -> None:
    result = _certify()
    survivable = [row for row in result["results"] if row["expected_survivable"]]
    catastrophic = [row for row in result["results"] if not row["expected_survivable"]]
    assert all(row["recovery_verdict"] == "PASS" for row in survivable)
    assert all(row["recovery_verdict"] == "REFUSE" for row in catastrophic)
    assert {row["converged_orchestration_sha256"] for row in survivable} == {
        result["converged_orchestration_sha256"]
    }


def test_shared_placement_dimension_or_hidden_dependency_refuses() -> None:
    _, _, checkpoints = _fixture()
    placements = _placements(checkpoints)
    placements[1]["placement"]["power_domain"] = placements[0]["placement"]["power_domain"]
    result = _certify(placements=placements)
    assert "POWER_DOMAIN_NOT_DIVERSE" in result["errors"]
    placements = _placements(checkpoints)
    placements[1]["dependency_sha256"] = placements[0]["dependency_sha256"]
    result = _certify(placements=placements)
    assert "HIDDEN_DEPENDENCY_COLLISION" in result["errors"]


def test_mislabeled_survivability_claim_refuses() -> None:
    scenarios = _scenarios()
    scenarios[-1]["survivable"] = True
    result = _certify(scenarios=scenarios)
    assert result["verdict"] == "REFUSE"
    assert "SURVIVABLE_SCENARIO_FAILED" in result["errors"]


def test_placement_tamper_and_drift_refuse() -> None:
    _, _, checkpoints = _fixture()
    placements = _placements(checkpoints)
    placements[0]["placement"]["host"] = "forged"
    result = _certify(placements=placements)
    assert "PLACED_REPLICA_HASH_MISMATCH" in result["errors"]
    assert result["recovery_claim_allowed"] is False


def test_matrix_is_deterministic_input_preserving_and_execution_free() -> None:
    records, _, checkpoints = _fixture()
    placements, scenarios = _placements(checkpoints), _scenarios()
    original = copy.deepcopy((records, checkpoints, placements, scenarios))
    kwargs = {"allowed_replicas": ALLOWED, "quorum": 2}
    first = certify_interruption_matrix(records, checkpoints, placements, scenarios, **kwargs)
    second = certify_interruption_matrix(records, checkpoints, placements, scenarios, **kwargs)
    assert first == second
    assert (records, checkpoints, placements, scenarios) == original
    assert first["settlement_record_unchanged"] is True
    assert first["executable"] is False
    assert all(value is False for key, value in first["safety"].items() if key != "offline_only")
