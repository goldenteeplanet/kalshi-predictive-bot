from __future__ import annotations

import copy
import hashlib
import json

from scripts.local.phase4nr_long_horizon_soak import generate_workload, run_soak
from scripts.local.phase4nt_recovery_quorum import (
    attest_checkpoint,
    default_registry,
    run_quorum_matrix,
    select_quorum_checkpoint,
)
from tests.test_phase4mz_adversarial_backtest import _records


def _fixture():
    workload = generate_workload(seed=20260829, epochs=60, maximum_length=24, minimum_commands=500)
    checkpoints = run_soak(
        workload, _records(), checkpoint_interval=20, maximum_seconds=120, maximum_bytes=10_000_000
    )["checkpoints"]
    return workload, checkpoints, default_registry()


def _attest(checkpoint, witness, registry, version="v1"):
    return attest_checkpoint(
        checkpoint,
        witness_id=witness,
        key_version=version,
        registry=registry,
        issued_epoch=checkpoint["next_epoch"],
    )


def _select(workload, checkpoints, attestations, registry, **kwargs):
    return select_quorum_checkpoint(
        workload,
        checkpoints,
        attestations,
        _records(),
        registry=registry,
        threshold=2,
        maximum_staleness_epochs=kwargs.get("staleness", 60),
        maximum_rollback_epochs=kwargs.get("rollback", 60),
    )


def test_two_of_three_independent_witnesses_select_newest_checkpoint() -> None:
    workload, checkpoints, registry = _fixture()
    latest = checkpoints[-1]
    result = _select(
        workload,
        checkpoints,
        [_attest(latest, "witness-a", registry), _attest(latest, "witness-c", registry, "v2")],
        registry,
    )
    assert result["verdict"] == "PASS"
    assert result["selected_epoch"] == 60
    assert result["recovery_final_sha256"]


def test_insufficient_stale_unknown_revoked_and_old_key_votes_refuse() -> None:
    workload, checkpoints, registry = _fixture()
    latest, old = checkpoints[-1], checkpoints[0]
    cases = [
        [_attest(latest, "witness-a", registry)],
        [_attest(old, "witness-a", registry), _attest(old, "witness-b", registry)],
        [_attest(latest, "witness-a", registry), _attest(latest, "witness-revoked", registry)],
        [_attest(latest, "witness-a", registry), _attest(latest, "witness-c", registry, "v1")],
    ]
    assert _select(workload, checkpoints, cases[0], registry)["verdict"] == "REFUSE"
    assert (
        "STALE_QUORUM" in _select(workload, checkpoints, cases[1], registry, staleness=10)["errors"]
    )
    assert all(
        _select(workload, checkpoints, case, registry)["verdict"] == "REFUSE" for case in cases[2:]
    )
    unknown = copy.deepcopy(cases[0][0])
    unknown["witness_id"] = "unknown"
    assert (
        "UNKNOWN_WITNESS"
        in _select(workload, checkpoints, [unknown], registry)["rejected_attestations"][0]["errors"]
    )


def test_duplicate_forged_mixed_workload_and_safety_attestations_refuse() -> None:
    workload, checkpoints, registry = _fixture()
    latest = checkpoints[-1]
    a, b = _attest(latest, "witness-a", registry), _attest(latest, "witness-b", registry)
    duplicate = _select(workload, checkpoints, [a, a], registry)
    assert "DUPLICATE_SIGNER_VOTE" in duplicate["errors"]
    forged = copy.deepcopy(a)
    forged["signature_sha256"] = "0" * 64
    assert _select(workload, checkpoints, [forged, b], registry)["verdict"] == "REFUSE"
    mixed = copy.deepcopy(a)
    mixed["workload_sha256"] = "f" * 64
    assert _select(workload, checkpoints, [mixed, b], registry)["verdict"] == "REFUSE"
    unsafe = copy.deepcopy(a)
    unsafe["safety_sha256"] = "0" * 64
    assert _select(workload, checkpoints, [unsafe, b], registry)["verdict"] == "REFUSE"


def test_equivocation_and_split_brain_are_detected() -> None:
    workload, checkpoints, registry = _fixture()
    latest = checkpoints[-1]
    conflict = copy.deepcopy(latest)
    conflict["cumulative_sha256"] = "a" * 64
    body = {k: v for k, v in conflict.items() if k != "checkpoint_sha256"}
    conflict["checkpoint_sha256"] = hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    candidates = checkpoints + [conflict]
    votes = [
        _attest(latest, "witness-a", registry),
        _attest(latest, "witness-b", registry),
        _attest(conflict, "witness-a", registry),
        _attest(conflict, "witness-c", registry, "v2"),
    ]
    result = _select(workload, candidates, votes, registry)
    assert "WITNESS_EQUIVOCATION" in result["errors"]


def test_key_rotation_restores_quorum_and_recovery_matches_uninterrupted_proof() -> None:
    workload, checkpoints, registry = _fixture()
    latest = checkpoints[-1]
    old = [_attest(latest, "witness-a", registry), _attest(latest, "witness-c", registry, "v1")]
    new = [_attest(latest, "witness-a", registry), _attest(latest, "witness-c", registry, "v2")]
    assert _select(workload, checkpoints, old, registry)["verdict"] == "REFUSE"
    restored = _select(workload, checkpoints, new, registry)
    assert restored["verdict"] == "PASS"
    assert all(
        restored[field]
        for field in (
            "recovery_final_sha256",
            "recovery_coverage_sha256",
            "recovery_refusal_classes",
            "recovery_provenance_sha256",
        )
    )


def test_quorum_matrix_is_deterministic_and_exercises_partition_recovery() -> None:
    workload, checkpoints, registry = _fixture()
    first = run_quorum_matrix(workload, checkpoints, _records(), registry=registry)
    assert first == run_quorum_matrix(workload, checkpoints, _records(), registry=registry)
    assert first["verdict"] == "PASS"
    assert first["case_count"] == 7


def test_quorum_layer_has_no_execution_capability() -> None:
    workload, checkpoints, registry = _fixture()
    latest = checkpoints[-1]
    result = _select(workload, checkpoints, [_attest(latest, "witness-a", registry)], registry)
    safety = result["safety"]
    assert safety["offline_only"] is True
    assert all(value is False for key, value in safety.items() if key != "offline_only")
