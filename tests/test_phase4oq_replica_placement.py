from __future__ import annotations

import copy

from scripts.local.phase4oq_replica_placement import audit_placement, pair_losses, place_replica

HISTORY = "a" * 64


def _placements():
    return [
        place_replica(
            replica_id=f"replica-{index}",
            history_sha256=HISTORY,
            host=f"host-{index}",
            filesystem=f"fs-{index}",
            administrator=f"admin-{index}",
            failure_domain=f"zone-{index}",
        )
        for index in range(1, 6)
    ]


def _pairs():
    return pair_losses(
        ("failure_domain", "zone-1"),
        ("failure_domain", "zone-2"),
        ("administrator", "admin-3"),
    )


def test_diverse_five_replica_placement_survives_required_losses() -> None:
    result = audit_placement(
        _placements(), trusted_history_sha256=HISTORY, quorum=2, required_multi_losses=_pairs()
    )
    assert result["verdict"] == "PASS"
    assert result["placement_count"] == 5
    assert result["minimum_survivors"] >= 3
    assert all(row["recovery_claim_allowed"] for row in result["results"])


def test_shared_host_filesystem_admin_domain_and_hidden_dependency_refuse() -> None:
    for field in ("host", "filesystem", "administrator", "failure_domain"):
        placements = _placements()
        placements[1][field] = placements[0][field]
        result = audit_placement(
            placements, trusted_history_sha256=HISTORY, quorum=2, required_multi_losses=[]
        )
        assert result["verdict"] == "REFUSE"
        assert f"{field.upper()}_NOT_DIVERSE" in result["errors"]
    placements = _placements()
    placements[1]["dependency_sha256"] = placements[0]["dependency_sha256"]
    result = audit_placement(
        placements, trusted_history_sha256=HISTORY, quorum=2, required_multi_losses=[]
    )
    assert "HIDDEN_DEPENDENCY_COLLISION" in result["errors"]


def test_wrong_history_incomplete_tampered_and_duplicate_identity_refuse() -> None:
    mutations = []
    wrong = _placements()
    wrong[0]["history_sha256"] = "b" * 64
    mutations.append((wrong, "REPLICA_INCOMPLETE_OR_WRONG_HISTORY"))
    incomplete = _placements()
    incomplete[0]["complete"] = False
    mutations.append((incomplete, "REPLICA_INCOMPLETE_OR_WRONG_HISTORY"))
    tampered = _placements()
    tampered[0]["host"] = "forged"
    mutations.append((tampered, "PLACEMENT_HASH_MISMATCH"))
    duplicate = _placements()
    duplicate[1]["replica_id"] = duplicate[0]["replica_id"]
    mutations.append((duplicate, "REPLICA_ID_REUSED"))
    for placements, expected in mutations:
        result = audit_placement(
            placements, trusted_history_sha256=HISTORY, quorum=2, required_multi_losses=[]
        )
        assert result["verdict"] == "REFUSE"
        assert expected in result["errors"]


def test_correlated_loss_below_quorum_denies_recovery_claim() -> None:
    placements = _placements()[:3]
    losses = [
        (("failure_domain", "zone-1"), ("failure_domain", "zone-2")),
    ]
    result = audit_placement(
        placements, trusted_history_sha256=HISTORY, quorum=2, required_multi_losses=losses
    )
    assert result["verdict"] == "REFUSE"
    assert "LOSS_SCENARIO_BREAKS_QUORUM" in result["errors"]
    assert result["recovery_claim_allowed"] is False


def test_duplicate_loss_scenario_and_invalid_quorum_refuse() -> None:
    loss = (("host", "host-1"),)
    result = audit_placement(
        _placements(),
        trusted_history_sha256=HISTORY,
        quorum=6,
        required_multi_losses=[loss, loss],
    )
    assert "PLACEMENT_QUORUM_INVALID" in result["errors"]
    assert "LOSS_SCENARIO_DUPLICATED" in result["errors"]


def test_audit_is_deterministic_input_preserving_and_execution_free() -> None:
    placements = _placements()
    original = copy.deepcopy(placements)
    kwargs = {
        "trusted_history_sha256": HISTORY,
        "quorum": 2,
        "required_multi_losses": _pairs(),
    }
    first = audit_placement(placements, **kwargs)
    assert first == audit_placement(placements, **kwargs)
    assert placements == original
    assert first["safety"]["offline_only"] is True
    assert all(value is False for key, value in first["safety"].items() if key != "offline_only")
