from __future__ import annotations

import copy
from datetime import timedelta

from scripts.local.phase4ou_offline_renewal import orchestrate_renewal
from scripts.local.phase4ov_renewal_resume import build_checkpoint_chain, resume_from_checkpoint
from tests.test_phase4ot_evidence_aging_handoff import START, _records


def _fixture(records=None, minute=1):
    records = records or _records()
    orchestration = orchestrate_renewal(
        records,
        evaluated_at=START + timedelta(hours=7),
        renewed_at=START + timedelta(hours=7, minutes=minute),
    )
    return records, orchestration, build_checkpoint_chain(records, orchestration)


def test_every_interruption_point_converges_to_uninterrupted_hash() -> None:
    records, orchestration, checkpoints = _fixture()
    assert len(checkpoints) == 7
    for index in range(len(checkpoints)):
        result = resume_from_checkpoint(records, checkpoints[: index + 1])
        assert result["verdict"] == "PASS"
        assert result["converged"] is True
        assert (
            result["final_orchestration"]["orchestration_sha256"]
            == orchestration["orchestration_sha256"]
        )
        assert result["completed_count"] == index


def test_checkpoint_chain_is_hash_linked_and_prefix_exactly_once() -> None:
    _, _, checkpoints = _fixture()
    for index, checkpoint in enumerate(checkpoints):
        assert checkpoint["completed_count"] == index
        assert len(checkpoint["completed_step_sha256s"]) == index
        if index:
            assert (
                checkpoint["previous_checkpoint_sha256"]
                == checkpoints[index - 1]["checkpoint_sha256"]
            )


def test_forged_truncated_reordered_and_duplicate_checkpoint_chains_refuse() -> None:
    records, _, checkpoints = _fixture()
    variants = []
    forged = copy.deepcopy(checkpoints[:4])
    forged[-1]["completed_count"] = 2
    variants.append(forged)
    variants.append([checkpoints[0], checkpoints[2]])
    variants.append([checkpoints[1], checkpoints[0]])
    variants.append([checkpoints[0], checkpoints[1], checkpoints[1]])
    for chain in variants:
        result = resume_from_checkpoint(records, chain)
        assert result["verdict"] == "REFUSE"
        assert result["converged"] is False


def test_cross_run_stale_and_settlement_binding_mismatch_refuse() -> None:
    records, _, checkpoints = _fixture(minute=1)
    _, _, other = _fixture(minute=2)
    mixed = [*checkpoints[:2], other[2]]
    assert resume_from_checkpoint(records, mixed)["verdict"] == "REFUSE"
    changed_records = copy.deepcopy(records)
    changed_records[-1]["record_sha256"] = "f" * 64
    result = resume_from_checkpoint(changed_records, checkpoints[:3])
    assert result["verdict"] == "REFUSE"


def test_empty_chain_and_reordered_completed_steps_refuse() -> None:
    records, _, checkpoints = _fixture()
    assert "CHECKPOINT_CHAIN_EMPTY" in resume_from_checkpoint(records, [])["errors"]
    changed = copy.deepcopy(checkpoints[:4])
    changed[-1]["completed_evidence_ids"] = list(reversed(changed[-1]["completed_evidence_ids"]))
    assert resume_from_checkpoint(records, changed)["verdict"] == "REFUSE"


def test_resume_is_deterministic_input_preserving_and_execution_free() -> None:
    records, _, checkpoints = _fixture()
    original = copy.deepcopy((records, checkpoints))
    first = resume_from_checkpoint(records, checkpoints[:4])
    second = resume_from_checkpoint(records, checkpoints[:4])
    assert first == second
    assert (records, checkpoints) == original
    assert first["settlement_record_unchanged"] is True
    assert first["executable"] is False
    assert all(value is False for key, value in first["safety"].items() if key != "offline_only")
