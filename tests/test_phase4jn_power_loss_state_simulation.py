from dataclasses import replace

import pytest
from kalshi_predictor.workstation.power_loss_state_simulation import (
    EXPECTED_STATES,
    PowerLossStateSimulationError,
    make_power_loss_fixture,
    simulate_power_loss_state,
    validate_power_loss_simulation_result,
)


def _fixture(boundary, **overrides):
    expected = EXPECTED_STATES.get(boundary, (False, False, False))
    fields = dict(
        simulation_id_hash="1" * 64,
        sandbox_manifest_hash="2" * 64,
        loss_boundary=boundary,
        record_bytes_present=expected[0],
        record_complete=expected[1],
        fsync_completed=expected[2],
        fixture_only=True,
        complete=True,
    )
    fields.update(overrides)
    return make_power_loss_fixture(**fields)


def test_all_power_loss_boundaries_have_exact_fail_closed_disposition() -> None:
    expected_dispositions = {
        "BEFORE_APPEND": "NO_INTENT",
        "DURING_APPEND": "REFUSE_CORRUPT",
        "AFTER_APPEND_BEFORE_FSYNC": "REFUSE_NOT_DURABLE",
        "AFTER_FSYNC": "RECONCILE_DURABLE",
    }
    for boundary, disposition in expected_dispositions.items():
        first = simulate_power_loss_state(_fixture(boundary))
        assert first == simulate_power_loss_state(_fixture(boundary))
        assert first.status == "PASS" and first.recovery_disposition == disposition
        assert not first.automatic_replay_permitted and not first.restart_authorized
        validate_power_loss_simulation_result(first)


@pytest.mark.parametrize("boundary", list(EXPECTED_STATES))
def test_each_boundary_state_mismatch_fails(boundary) -> None:
    expected = EXPECTED_STATES[boundary]
    result = simulate_power_loss_state(_fixture(boundary, record_bytes_present=not expected[0]))
    assert result.status == "FAIL" and not result.automatic_replay_permitted


def test_unknown_non_fixture_and_incomplete_inputs_fail_closed() -> None:
    assert simulate_power_loss_state(_fixture("UNKNOWN_BOUNDARY")).status == "TAMPERED"
    assert (
        simulate_power_loss_state(_fixture("BEFORE_APPEND", fixture_only=False)).status
        == "TAMPERED"
    )
    assert (
        simulate_power_loss_state(_fixture("BEFORE_APPEND", complete=False)).status == "INCOMPLETE"
    )


def test_malformed_fixture_and_tampering_fail_closed() -> None:
    with pytest.raises(PowerLossStateSimulationError, match="FIELD_INVALID"):
        _fixture("bad boundary")
    fixture = _fixture("AFTER_FSYNC")
    with pytest.raises(PowerLossStateSimulationError, match="FIXTURE_HASH_MISMATCH"):
        simulate_power_loss_state(replace(fixture, fsync_completed=False))
    result = simulate_power_loss_state(fixture)
    with pytest.raises(PowerLossStateSimulationError, match="RESULT_HASH_MISMATCH"):
        validate_power_loss_simulation_result(replace(result, reasons=("FORGED",)))
    with pytest.raises(PowerLossStateSimulationError, match="SAFETY_BOUNDARY"):
        validate_power_loss_simulation_result(replace(result, restart_authorized=True))


def test_simulator_has_no_operational_surfaces() -> None:
    forbidden = {"open", "run", "Popen", "subprocess", "system", "spawn", "socket", "shutdown"}
    assert forbidden.isdisjoint(simulate_power_loss_state.__code__.co_names)
