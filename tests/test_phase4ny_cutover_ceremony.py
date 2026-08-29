from __future__ import annotations

from scripts.local.phase4nw_witness_migration import build_migration_plan
from scripts.local.phase4ny_cutover_ceremony import (
    STEPS,
    build_ceremony,
    inject_operator_fault,
    simulate_ceremony,
)
from tests.test_phase4nw_witness_migration import _layouts
from tests.test_phase4nx_migration_authorization import _verify


def _fixture():
    initial, replacements = _layouts()
    plan = build_migration_plan(initial, replacements)
    grant = _verify()["grant"]
    roles = {
        "observer": "operator-observer",
        "executor": "operator-executor",
        "verifier": "operator-verifier",
    }
    ceremony = build_ceremony(
        plan,
        grant,
        roles=roles,
        checkpoint_anchor="checkpoint-anchor",
        quorum_sha256="quorum-proof",
        rollback_sha256="rollback-proof",
    )
    return plan, grant, ceremony


def _simulate(ceremony=None, used=None):
    plan, grant, default = _fixture()
    return simulate_ceremony(
        ceremony or default,
        plan,
        grant,
        used_authorization_ids=used or set(),
        maximum_observation_age=8,
        maximum_step_gap=8,
    )


def test_valid_ceremony_is_deterministic_complete_and_role_separated() -> None:
    plan, grant, ceremony = _fixture()
    first = _simulate(ceremony)
    assert first == _simulate(ceremony)
    assert first["verdict"] == "PASS"
    assert first["terminal_state"] == "COMPLETED"
    assert first["completed_stages"] == grant["stage_end"] - grant["stage_start"] + 1 == 8
    assert len(first["transcript"]) == 8 * len(STEPS)


def test_every_operator_fault_stops_at_unsafe_boundary() -> None:
    _, _, ceremony = _fixture()
    faults = (
        "SKIPPED_STEP",
        "OUT_OF_ORDER",
        "WRONG_WITNESS",
        "STALE_OBSERVATION",
        "INCORRECT_READBACK",
        "MISMATCHED_PLAN",
        "PREMATURE_REVOCATION",
        "THRESHOLD_MISTAKE",
        "AMBIGUOUS_RESPONSE",
        "FAILED_VERIFICATION",
        "TIMEOUT",
        "INTERRUPTION",
        "CONTINUE_AFTER_ROLLBACK",
    )
    for fault in faults:
        result = _simulate(inject_operator_fault(ceremony, fault))
        assert result["verdict"] == "REFUSE", fault
        assert result["terminal_state"] in {"PAUSED", "ROLLED_BACK"}
        assert result["completed_stages"] < 8


def test_reused_authorization_and_wrong_operator_roles_refuse() -> None:
    _, grant, ceremony = _fixture()
    reused = _simulate(ceremony, used={grant["authorization_id"]})
    assert "AUTHORIZATION_REPLAYED" in reused["errors"]
    ceremony["roles"]["verifier"] = ceremony["roles"]["executor"]
    assert "OPERATOR_ROLE_SEPARATION_FAILED" in _simulate(ceremony)["errors"]


def test_transcript_hash_chain_identifies_observer_executor_and_verifier() -> None:
    result = _simulate()
    actors = {row["actor"] for row in result["transcript"]}
    assert actors == {"operator-observer", "operator-executor", "operator-verifier"}
    assert all(
        row["previous_transcript_sha256"] == result["transcript"][index - 1]["transcript_sha256"]
        for index, row in enumerate(result["transcript"])
        if index
    )


def test_failed_stage_forbids_continuation_and_consumes_authorization() -> None:
    _, _, ceremony = _fixture()
    result = _simulate(inject_operator_fault(ceremony, "CONTINUE_AFTER_ROLLBACK"))
    assert "POST_STAGE_VERIFICATION_FAILED" in result["errors"]
    assert "CONTINUATION_AFTER_ROLLBACK" in result["errors"]
    assert result["authorization_consumed"] is True


def test_ceremony_has_no_infrastructure_or_trading_capability() -> None:
    safety = _simulate()["safety"]
    assert safety["offline_only"] is True
    assert all(value is False for key, value in safety.items() if key != "offline_only")
