from dataclasses import replace

import pytest
from kalshi_predictor.workstation.restart_cancellation_token import (
    RestartCancellationTokenError,
    evaluate_restart_cancellation_token,
    make_restart_cancellation_token,
    validate_restart_cancellation_token_decision,
)

INCIDENT = "1" * 64
WARNING = "2" * 64
INTENT = "3" * 64


def _token(**overrides):
    fields = dict(
        token_id_hash="4" * 64,
        incident_id_hash=INCIDENT,
        warning_decision_hash=WARNING,
        restart_intent_hash=INTENT,
        issued_at_epoch=1_000,
        expires_at_epoch=1_300,
        single_use=True,
        consumed=False,
        complete=True,
    )
    fields.update(overrides)
    return make_restart_cancellation_token(**fields)


def _evaluate(token=None, at=1_100, **bindings):
    return evaluate_restart_cancellation_token(
        token or _token(),
        evaluated_at_epoch=at,
        expected_incident_id_hash=bindings.get("incident", INCIDENT),
        expected_warning_decision_hash=bindings.get("warning", WARNING),
        expected_restart_intent_hash=bindings.get("intent", INTENT),
    )


def test_valid_token_is_deterministic_single_use_cancellation_only() -> None:
    first = _evaluate()
    assert first == _evaluate() and first.status == "VALID" and first.cancellation_validated
    assert first.restart_denied_after_cancellation and not first.restart_authorized
    validate_restart_cancellation_token_decision(first)


def test_exact_expiry_valid_then_expired_and_consumed_is_idempotent() -> None:
    assert _evaluate(at=1_300).status == "VALID"
    assert _evaluate(at=1_301).status == "EXPIRED"
    used = _evaluate(_token(consumed=True))
    assert used.status == "USED" and used.cancellation_validated


@pytest.mark.parametrize(
    "token",
    [
        _token(complete=False),
        _token(single_use=False),
        _token(expires_at_epoch=1_301),
        _token(issued_at_epoch=1_101),
    ],
)
def test_incomplete_policy_invalid_excessive_or_future_tokens_fail_closed(token) -> None:
    assert _evaluate(token).status in {"INCOMPLETE", "DENIED"}


def test_binding_mismatch_and_tampering_fail_closed() -> None:
    assert _evaluate(incident="a" * 64, warning="b" * 64, intent="c" * 64).status == "TAMPERED"
    with pytest.raises(RestartCancellationTokenError, match="TOKEN_HASH_MISMATCH"):
        _evaluate(replace(_token(), consumed=True))
    decision = _evaluate()
    with pytest.raises(RestartCancellationTokenError, match="DECISION_HASH_MISMATCH"):
        validate_restart_cancellation_token_decision(replace(decision, reasons=("FORGED",)))
    with pytest.raises(RestartCancellationTokenError, match="SAFETY_BOUNDARY"):
        validate_restart_cancellation_token_decision(replace(decision, restart_authorized=True))


def test_malformed_bounds_and_operational_surfaces_fail_closed() -> None:
    with pytest.raises(RestartCancellationTokenError, match="FIELD_INVALID"):
        _token(token_id_hash="bad")
    with pytest.raises(RestartCancellationTokenError, match="TIME_INVALID"):
        _evaluate(at=-1)
    forbidden = {"open", "run", "popen", "subprocess", "socket", "shutdown", "systemctl"}
    assert forbidden.isdisjoint(evaluate_restart_cancellation_token.__code__.co_names)
