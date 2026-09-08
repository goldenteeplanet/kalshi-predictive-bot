from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta

from scripts.local.phase4ot_evidence_aging_handoff import (
    DEPENDENCY_ORDER,
    build_handoff_packet,
    create_evidence_record,
)

START = datetime(2026, 8, 29, 12, tzinfo=UTC)


def _records(settlement="UNAVAILABLE"):
    records = []
    for index, evidence_id in enumerate(DEPENDENCY_ORDER):
        dependencies = () if index == 0 else (DEPENDENCY_ORDER[index - 1],)
        records.append(
            create_evidence_record(
                evidence_id=evidence_id,
                proof_sha256=settlement
                if evidence_id == "authoritative_settlement"
                else f"{index:x}" * 64,
                observed_at=START,
                ttl=timedelta(hours=6)
                if evidence_id != "authoritative_settlement"
                else timedelta(days=3),
                dependencies=dependencies,
            )
        )
    return records


def test_pre_settlement_handoff_is_complete_but_non_executable_and_refused() -> None:
    result = build_handoff_packet(_records(), evaluated_at=START + timedelta(hours=1))
    assert result["verdict"] == "REFUSE"
    assert result["settlement_ready"] is False
    assert result["executable"] is False
    assert "SETTLEMENT_HANDOFF_NOT_READY" in result["errors"]
    assert result["paper_position_preserved"].startswith("KXRAINAUSM")


def test_expired_offline_evidence_gets_deterministic_renewal_schedule() -> None:
    result = build_handoff_packet(_records(), evaluated_at=START + timedelta(hours=7))
    scheduled = [row["evidence_id"] for row in result["renewal_schedule"]]
    assert scheduled == list(DEPENDENCY_ORDER[:-1])
    assert "authoritative_settlement" not in scheduled


def test_fresh_authoritative_settlement_can_make_handoff_ready_but_not_executable() -> None:
    result = build_handoff_packet(
        _records(settlement="a" * 64), evaluated_at=START + timedelta(hours=1)
    )
    assert result["verdict"] == "PASS"
    assert result["settlement_ready"] is True
    assert result["executable"] is False


def test_stale_settlement_and_circular_or_missing_dependency_refuse() -> None:
    stale = build_handoff_packet(
        _records(settlement="a" * 64), evaluated_at=START + timedelta(days=4)
    )
    assert "AUTHORITATIVE_SETTLEMENT_UNAVAILABLE" in stale["errors"]
    circular = _records()
    circular[0]["dependencies"] = ["authoritative_settlement"]
    result = build_handoff_packet(circular, evaluated_at=START)
    assert "CIRCULAR_RENEWAL_DEPENDENCY" in result["errors"]
    missing = _records()[:-1]
    assert (
        "EVIDENCE_ORDER_OR_COVERAGE_INVALID"
        in build_handoff_packet(missing, evaluated_at=START)["errors"]
    )


def test_tampering_reordering_and_safety_exposure_refuse() -> None:
    variants = []
    tampered = _records()
    tampered[0]["expires_at"] = (START + timedelta(days=1)).isoformat()
    variants.append((tampered, "aggregate_certificate:RECORD_HASH_MISMATCH"))
    reordered = list(reversed(_records()))
    variants.append((reordered, "EVIDENCE_ORDER_OR_COVERAGE_INVALID"))
    unsafe = _records()
    unsafe[1]["safety"]["paper_order_creation"] = True
    variants.append((unsafe, "freshness_proof:SCHEMA_OR_SAFETY_INVALID"))
    for records, expected in variants:
        result = build_handoff_packet(records, evaluated_at=START)
        assert result["verdict"] == "REFUSE"
        assert expected in result["errors"]


def test_handoff_is_deterministic_input_preserving_and_execution_free() -> None:
    records = _records()
    original = copy.deepcopy(records)
    first = build_handoff_packet(records, evaluated_at=START)
    second = build_handoff_packet(records, evaluated_at=START)
    assert first == second
    assert records == original
    assert first["safety"]["offline_only"] is True
    assert all(value is False for key, value in first["safety"].items() if key != "offline_only")
