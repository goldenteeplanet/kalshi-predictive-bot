from dataclasses import replace

import pytest
from kalshi_predictor.workstation.crash_boundary_simulation import (
    BOUNDARY_EXPECTATIONS,
    CrashBoundarySimulationError,
    make_crash_boundary_fixture,
    simulate_crash_boundary,
    validate_crash_boundary_simulation_result,
)


def _fixture(boundary, **overrides):
    expected = BOUNDARY_EXPECTATIONS.get(boundary, (False, False, 0))
    fields = dict(
        simulation_id_hash="1" * 64,
        sandbox_manifest_hash="2" * 64,
        boundary=boundary,
        intent_persisted=expected[0],
        post_boot_pending=expected[1],
        mock_invocation_count=expected[2],
        fixture_only=True,
        complete=True,
    )
    fields.update(overrides)
    return make_crash_boundary_fixture(**fields)


def test_every_crash_boundary_matches_exact_expected_state() -> None:
    for boundary in BOUNDARY_EXPECTATIONS:
        first = simulate_crash_boundary(_fixture(boundary))
        assert first == simulate_crash_boundary(_fixture(boundary))
        assert first.status == "PASS" and not first.automatic_replay_permitted
        assert first.reconciliation_required == (boundary != "BEFORE_INTENT_PERSIST")
        assert not first.restart_authorized
        validate_crash_boundary_simulation_result(first)


@pytest.mark.parametrize("boundary", list(BOUNDARY_EXPECTATIONS))
def test_each_boundary_state_mismatch_fails(boundary) -> None:
    expected = BOUNDARY_EXPECTATIONS[boundary]
    result = simulate_crash_boundary(_fixture(boundary, intent_persisted=not expected[0]))
    assert result.status == "FAIL" and not result.automatic_replay_permitted


def test_unknown_non_fixture_and_incomplete_inputs_fail_closed() -> None:
    assert simulate_crash_boundary(_fixture("UNKNOWN_BOUNDARY")).status == "TAMPERED"
    assert (
        simulate_crash_boundary(_fixture("BEFORE_INTENT_PERSIST", fixture_only=False)).status
        == "TAMPERED"
    )
    assert (
        simulate_crash_boundary(_fixture("BEFORE_INTENT_PERSIST", complete=False)).status
        == "INCOMPLETE"
    )


def test_malformed_fixture_and_tampering_fail_closed() -> None:
    with pytest.raises(CrashBoundarySimulationError, match="FIELD_INVALID"):
        _fixture("bad boundary")
    fixture = _fixture("AFTER_INTENT_PERSIST")
    with pytest.raises(CrashBoundarySimulationError, match="FIXTURE_HASH_MISMATCH"):
        simulate_crash_boundary(replace(fixture, post_boot_pending=False))
    result = simulate_crash_boundary(fixture)
    with pytest.raises(CrashBoundarySimulationError, match="RESULT_HASH_MISMATCH"):
        validate_crash_boundary_simulation_result(replace(result, reasons=("FORGED",)))
    with pytest.raises(CrashBoundarySimulationError, match="SAFETY_BOUNDARY"):
        validate_crash_boundary_simulation_result(replace(result, restart_authorized=True))


def test_simulator_has_no_operational_surfaces() -> None:
    forbidden = {"open", "run", "Popen", "subprocess", "system", "spawn", "socket", "shutdown"}
    assert forbidden.isdisjoint(simulate_crash_boundary.__code__.co_names)
