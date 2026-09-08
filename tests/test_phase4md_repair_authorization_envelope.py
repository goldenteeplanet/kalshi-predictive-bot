from __future__ import annotations

import copy

import pytest

from scripts.local.phase4mb_checkpoint_repair_planner import plan_repair
from scripts.local.phase4md_repair_authorization_envelope import (
    _signature,
    issue_token,
    validate_token,
)
from tests.test_phase4mb_checkpoint_repair_planner import _checkpoints, _manifest

KEY = bytes.fromhex("11" * 32)
COMMON = {
    "incident_id_sha256": "1" * 64,
    "recovery_epoch": 8,
    "issuer": "dejoia.offline.recovery-authorizer",
    "audience": "dejoia.offline.repair-simulator",
}


def _plan():
    return plan_repair(
        "1" * 64,
        8,
        _checkpoints()[:6],
        _manifest(),
        evaluated_at="2026-08-28T20:10:00Z",
    )


def _token(plan=None):
    return issue_token(
        plan or _plan(),
        **COMMON,
        issued_at="2026-08-28T20:11:00Z",
        expires_at="2026-08-28T20:21:00Z",
        nonce="recovery-epoch-8-attempt-1",
        signing_key=KEY,
    )


def _validate(token=None, plan=None, consumed_nonces=(), **overrides):
    arguments = {
        **COMMON,
        "evaluated_at": "2026-08-28T20:12:00Z",
        "signing_key": KEY,
        "consumed_nonces": consumed_nonces,
        **overrides,
    }
    return validate_token(token or _token(plan), plan or _plan(), **arguments)


def _resign(token):
    body = {key: value for key, value in token.items() if key != "signature_sha256"}
    token["signature_sha256"] = _signature(body, KEY)


def test_issue_and_validate_is_deterministic_and_inert() -> None:
    token = _token()
    assert token == _token()
    first = _validate(token)
    assert first == _validate(token)
    assert first["verdict"] == "PASS"
    assert first["proposed_consumption_receipt"]["execution_authorized_by_validator"] is False


def test_replay_is_refused_without_mutating_ledger() -> None:
    ledger = ["recovery-epoch-8-attempt-1"]
    before = copy.deepcopy(ledger)
    result = _validate(consumed_nonces=ledger)
    assert result["verdict"] == "REFUSE"
    assert "TOKEN_REPLAYED" in result["errors"]
    assert ledger == before


@pytest.mark.parametrize(
    "evaluated_at,error",
    [
        ("2026-08-28T20:10:59Z", "TOKEN_NOT_YET_VALID"),
        ("2026-08-28T20:21:00Z", "TOKEN_EXPIRED"),
    ],
)
def test_time_window_is_fail_closed(evaluated_at: str, error: str) -> None:
    result = _validate(evaluated_at=evaluated_at)
    assert result["verdict"] == "REFUSE"
    assert error in result["errors"]


@pytest.mark.parametrize(
    "field,value,error",
    [
        ("plan_sha256", "f" * 64, "PLAN_SHA256_BINDING_MISMATCH"),
        ("incident_id_sha256", "f" * 64, "INCIDENT_ID_SHA256_BINDING_MISMATCH"),
        ("recovery_epoch", 9, "RECOVERY_EPOCH_BINDING_MISMATCH"),
        ("trusted_prefix_count", 7, "TRUSTED_PREFIX_COUNT_BINDING_MISMATCH"),
        ("trusted_checkpoint_sha256", "f" * 64, "TRUSTED_CHECKPOINT_SHA256_BINDING_MISMATCH"),
        ("authorized_actions_sha256", "f" * 64, "AUTHORIZED_ACTIONS_SHA256_BINDING_MISMATCH"),
        ("authorized_action_names", ["NO_REPAIR"], "AUTHORIZED_ACTION_NAMES_BINDING_MISMATCH"),
        (
            "fail_closed_invariants_sha256",
            "f" * 64,
            "FAIL_CLOSED_INVARIANTS_SHA256_BINDING_MISMATCH",
        ),
        ("audience", "dejoia.wrong.audience", "AUDIENCE_BINDING_MISMATCH"),
    ],
)
def test_signed_substitution_and_expansion_are_refused(field, value, error: str) -> None:
    token = _token()
    token[field] = value
    _resign(token)
    result = _validate(token)
    assert result["verdict"] == "REFUSE"
    assert error in result["errors"]


def test_wrong_key_and_unsigned_mutation_are_refused() -> None:
    token = _token()
    token["audience"] = "dejoia.wrong.audience"
    assert "SIGNATURE_INVALID" in _validate(token)["errors"]
    assert "SIGNATURE_INVALID" in _validate(signing_key=b"2" * 32)["errors"]


def test_plan_substitution_is_refused() -> None:
    plan = _plan()
    token = _token(plan)
    other = copy.deepcopy(plan)
    other["trusted_prefix_count"] += 1
    result = _validate(token, other)
    assert result["verdict"] == "REFUSE"
    assert "PLAN_NOT_CERTIFIED" in result["errors"]


def test_issuance_rejects_bad_plan_lifetime_key_and_identity() -> None:
    plan = _plan()
    bad = copy.deepcopy(plan)
    bad["safety"]["order_capability"] = True
    with pytest.raises(ValueError):
        _token(bad)
    with pytest.raises(ValueError):
        issue_token(
            plan,
            **COMMON,
            issued_at="2026-08-28T20:11:00Z",
            expires_at="2026-08-28T21:11:00Z",
            nonce="recovery-epoch-8-attempt-1",
            signing_key=KEY,
        )
    with pytest.raises(ValueError):
        issue_token(
            plan,
            **COMMON,
            issued_at="2026-08-28T20:11:00Z",
            expires_at="2026-08-28T20:12:00Z",
            nonce="recovery-epoch-8-attempt-1",
            signing_key=b"short",
        )


def test_validation_has_no_execution_or_operational_capability() -> None:
    safety = _validate()["safety"]
    assert safety["validation_only"] is True
    assert all(value is False for key, value in safety.items() if key != "validation_only")
