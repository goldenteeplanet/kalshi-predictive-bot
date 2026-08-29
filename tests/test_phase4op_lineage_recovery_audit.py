from __future__ import annotations

import copy

from scripts.local.phase4op_lineage_recovery_audit import (
    create_replica,
    independently_audit_entries,
    recover_from_replicas,
)
from tests.test_phase4oo_baseline_lineage import _history

ALLOWED = {"replica-a", "replica-b", "replica-c"}


def _recover(replicas, history):
    return recover_from_replicas(
        replicas,
        allowed_replicas=ALLOWED,
        quorum=2,
        trusted_head_sha256=history[-1]["entry_sha256"],
        minimum_versions=2,
    )


def test_independent_audit_and_two_replica_recovery_pass_frozen() -> None:
    history = _history()
    audit = independently_audit_entries(
        history, trusted_head_sha256=history[-1]["entry_sha256"], minimum_versions=2
    )
    assert audit["verdict"] == "PASS"
    replicas = [create_replica(name, history) for name in ("replica-a", "replica-b")]
    result = _recover(replicas, history)
    assert result["verdict"] == "PASS"
    assert result["state"] == "FROZEN"
    assert result["capabilities_allowed"] is False
    assert result["independent_of_primary_verifier"] is True


def test_missing_prefix_interior_version_and_retention_loss_refuse() -> None:
    history = _history()
    candidates = [history[1:], [history[0]], [history[1]]]
    for candidate in candidates:
        audit = independently_audit_entries(
            candidate, trusted_head_sha256=history[-1]["entry_sha256"], minimum_versions=2
        )
        assert audit["verdict"] == "REFUSE"
        assert {"MISSING_PREFIX_OR_INTERIOR_VERSION", "INCOMPLETE_RETENTION"} & set(audit["errors"])


def test_single_damaged_replica_gets_repair_plan_from_matching_quorum() -> None:
    history = _history()
    replicas = [create_replica(name, history) for name in ALLOWED]
    replicas[2]["entries"][0]["baseline_sha256"] = "f" * 64
    result = _recover(replicas, history)
    assert result["verdict"] == "PASS"
    assert result["invalid_replica_ids"] == ["replica-c"]
    assert result["repair_plan"][0]["target_replica_id"] == "replica-c"
    assert result["repair_plan"][0]["replacement_entries"] == history


def test_ambiguous_or_insufficient_replica_quorum_refuses() -> None:
    history = _history()
    alternate = copy.deepcopy(history)
    alternate[1]["baseline_sha256"] = "f" * 64
    replicas = [
        create_replica("replica-a", history),
        create_replica("replica-b", alternate),
    ]
    result = _recover(replicas, history)
    assert result["verdict"] == "REFUSE"
    assert "UNIQUE_REPLICA_QUORUM_NOT_MET" in result["errors"]
    assert result["canonical_entries"] is None


def test_forgery_replay_unknown_replica_and_stale_head_refuse() -> None:
    history = _history()
    good = create_replica("replica-a", history)
    forged = copy.deepcopy(good)
    forged["replica_id"] = "replica-b"
    result = _recover([good, forged], history)
    assert result["verdict"] == "REFUSE"
    replay = _recover([good, good], history)
    assert "REPLICA_ID_REPLAY" in replay["errors"]
    unknown = create_replica("outsider", history)
    assert _recover([good, unknown], history)["verdict"] == "REFUSE"
    stale = recover_from_replicas(
        [create_replica("replica-a", history), create_replica("replica-b", history)],
        allowed_replicas=ALLOWED,
        quorum=2,
        trusted_head_sha256="e" * 64,
        minimum_versions=2,
    )
    assert stale["verdict"] == "REFUSE"


def test_rollback_masquerading_as_promotion_refuses() -> None:
    history = _history()
    disguised = copy.deepcopy(history)
    disguised[1]["performance_claim"] = False
    audit = independently_audit_entries(
        disguised, trusted_head_sha256=history[-1]["entry_sha256"], minimum_versions=2
    )
    assert "ROLLBACK_MASQUERADING_AS_PROMOTION" in audit["errors"]


def test_recovery_is_order_independent_input_preserving_and_execution_free() -> None:
    history = _history()
    replicas = [create_replica(name, history) for name in ("replica-b", "replica-a")]
    original = copy.deepcopy(replicas)
    forward = _recover(replicas, history)
    reverse = _recover(list(reversed(replicas)), history)
    assert forward == reverse
    assert replicas == original
    assert forward["safety"]["offline_only"] is True
    assert all(value is False for key, value in forward["safety"].items() if key != "offline_only")
