from __future__ import annotations

import copy
from datetime import timedelta

from scripts.local.phase4ot_evidence_aging_handoff import DEPENDENCY_ORDER
from scripts.local.phase4ou_offline_renewal import orchestrate_renewal, verify_orchestration
from tests.test_phase4ot_evidence_aging_handoff import START, _records


def _orchestrate(records=None):
    return orchestrate_renewal(
        records or _records(),
        evaluated_at=START + timedelta(hours=7),
        renewed_at=START + timedelta(hours=7, minutes=1),
    )


def test_all_due_offline_evidence_renews_in_dependency_order() -> None:
    result = _orchestrate()
    assert result["verdict"] == "PASS"
    assert result["due_order"] == list(DEPENDENCY_ORDER[:-1])
    assert [row["evidence_id"] for row in result["steps"]] == list(DEPENDENCY_ORDER[:-1])
    assert [row["sequence"] for row in result["steps"]] == list(range(1, 7))


def test_each_renewal_binds_predecessor_and_refreshed_dependency() -> None:
    records = _records()
    result = _orchestrate(records)
    by_id = {row["evidence_id"]: row for row in result["renewed_records"]}
    for index, step in enumerate(result["steps"]):
        original = records[index]
        assert step["predecessor_record_sha256"] == original["record_sha256"]
        if index:
            dependency = DEPENDENCY_ORDER[index - 1]
            assert step["dependency_proof_sha256s"][dependency] == by_id[dependency]["proof_sha256"]


def test_settlement_record_is_unchanged_and_post_handoff_non_executable() -> None:
    records = _records()
    result = _orchestrate(records)
    assert result["settlement_record_unchanged"] is True
    assert result["renewed_records"][-1] == records[-1]
    assert result["post_handoff"]["settlement_ready"] is False
    assert result["post_handoff"]["executable"] is False
    assert result["executable"] is False


def test_clock_rollback_circular_input_and_settlement_injection_refuse() -> None:
    rollback = orchestrate_renewal(
        _records(),
        evaluated_at=START + timedelta(hours=7),
        renewed_at=START + timedelta(hours=6),
    )
    assert "RENEWAL_CLOCK_ROLLBACK" in rollback["errors"]
    circular = _records()
    circular[0]["dependencies"] = ["authoritative_settlement"]
    result = _orchestrate(circular)
    assert "PREFLIGHT_EVIDENCE_INVALID" in result["errors"]


def test_partial_reordered_replayed_or_altered_result_fails_verification() -> None:
    records = _records()
    result = _orchestrate(records)
    variants = []
    partial = copy.deepcopy(result)
    partial["steps"].pop()
    variants.append(partial)
    reordered = copy.deepcopy(result)
    reordered["steps"][0], reordered["steps"][1] = reordered["steps"][1], reordered["steps"][0]
    variants.append(reordered)
    replayed = copy.deepcopy(result)
    replayed["steps"][1] = copy.deepcopy(replayed["steps"][0])
    variants.append(replayed)
    altered = copy.deepcopy(result)
    altered["renewed_records"][0]["proof_sha256"] = "f" * 64
    variants.append(altered)
    for candidate in variants:
        verification = verify_orchestration(records, candidate)
        assert verification["verdict"] == "REFUSE"
        assert "NONDETERMINISTIC_OR_ALTERED_RENEWAL" in verification["errors"]


def test_orchestration_is_deterministic_input_preserving_and_execution_free() -> None:
    records = _records()
    original = copy.deepcopy(records)
    first = _orchestrate(records)
    second = _orchestrate(records)
    assert first == second
    assert records == original
    verification = verify_orchestration(records, first)
    assert verification["verdict"] == "PASS"
    assert first["safety"]["offline_only"] is True
    assert all(value is False for key, value in first["safety"].items() if key != "offline_only")
