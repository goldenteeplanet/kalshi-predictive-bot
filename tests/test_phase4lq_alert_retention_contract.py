from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest

from scripts.local.phase4lq_alert_retention_contract import evaluate_retention


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _lifecycle(state="ACKNOWLEDGED", occurred="2026-08-01T00:00:00Z"):
    record = {
        "sequence": 1,
        "transition_id": "one",
        "envelope_sha256": "a" * 64,
        "from_state": "PRESENTED",
        "to_state": state,
        "occurred_at": occurred,
        "reason": "operator acknowledgement",
        "previous_record_sha256": "0" * 64,
    }
    record["record_sha256"] = _digest(record)
    value = {
        "schema": "phase4lp.alert-lifecycle.v1",
        "verdict": "PASS",
        "errors": [],
        "envelope_sha256": "a" * 64,
        "current_state": state,
        "terminal": True,
        "records": [record],
        "replay": {"chain_head_sha256": record["record_sha256"]},
        "safety": {},
    }
    value["lifecycle_sha256"] = _digest(value)
    return value


def _evaluate(lifecycle=None, hold=None, at="2026-08-28T00:00:00Z"):
    return evaluate_retention(lifecycle or _lifecycle(), hold, evaluated_at=at)


def test_minimizes_reason_and_preserves_required_provenance() -> None:
    result = _evaluate()
    assert result["verdict"] == "PASS"
    record = result["minimized_provenance"][0]
    assert record["reason"] == "[REDACTED:NONESSENTIAL_REASON]"
    assert set(record) == {
        "sequence",
        "transition_id",
        "to_state",
        "occurred_at",
        "record_sha256",
        "reason",
    }


@pytest.mark.parametrize(
    "state,days,eligibility",
    [
        ("CREATED", 2, "EXPIRY_ELIGIBLE"),
        ("VALIDATED", 6, "PRESERVE"),
        ("PRESENTED", 31, "EXPIRY_ELIGIBLE"),
        ("ACKNOWLEDGED", 89, "PRESERVE"),
        ("EXPIRED", 31, "EXPIRY_ELIGIBLE"),
        ("REJECTED", 31, "EXPIRY_ELIGIBLE"),
        ("ARCHIVED", 91, "EXPIRY_ELIGIBLE"),
    ],
)
def test_per_state_retention_limits(state: str, days: int, eligibility: str) -> None:
    evaluated = datetime(2026, 8, 28, tzinfo=UTC)
    occurred = (evaluated - timedelta(days=days)).isoformat().replace("+00:00", "Z")
    result = _evaluate(_lifecycle(state, occurred))
    assert result["eligibility"] == eligibility


def test_active_allowlisted_hold_preserves_expired_record() -> None:
    hold = {
        "reason_code": "incident_review",
        "created_at": "2026-08-27T00:00:00Z",
        "expires_at": "2026-08-29T00:00:00Z",
    }
    assert _evaluate(_lifecycle("EXPIRED"), hold)["eligibility"] == "PRESERVE"


@pytest.mark.parametrize("reason", ["trading_enablement", "personal_request", "forever"])
def test_unsafe_hold_reason_refuses(reason: str) -> None:
    hold = {
        "reason_code": reason,
        "created_at": "2026-08-27T00:00:00Z",
        "expires_at": "2026-08-29T00:00:00Z",
    }
    assert _evaluate(hold=hold)["verdict"] == "REFUSE"


def test_secret_personal_data_unknown_fields_and_bad_hash_refuse() -> None:
    lifecycle = _lifecycle()
    lifecycle["records"][0]["reason"] = "contact trader@example.com"
    lifecycle["lifecycle_sha256"] = _digest(
        {key: value for key, value in lifecycle.items() if key != "lifecycle_sha256"}
    )
    assert "SENSITIVE_DATA_REJECTED" in _evaluate(lifecycle)["errors"]
    lifecycle = _lifecycle()
    lifecycle["extra"] = "value"
    assert "LIFECYCLE_FIELD_SET_INVALID" in _evaluate(lifecycle)["errors"]
    lifecycle = _lifecycle()
    lifecycle["current_state"] = "ARCHIVED"
    assert "LIFECYCLE_HASH_MISMATCH" in _evaluate(lifecycle)["errors"]


def test_malformed_future_and_missing_anchor_refuse() -> None:
    assert _evaluate(at="tomorrow")["verdict"] == "REFUSE"
    assert _evaluate(_lifecycle(occurred="2027-01-01T00:00:00Z"))["verdict"] == "REFUSE"
    lifecycle = _lifecycle()
    lifecycle["records"] = []
    lifecycle["lifecycle_sha256"] = _digest(
        {key: value for key, value in lifecycle.items() if key != "lifecycle_sha256"}
    )
    assert "RETENTION_ANCHOR_MISSING" in _evaluate(lifecycle)["errors"]


@pytest.mark.parametrize(
    "mutation,error",
    [
        (lambda value: value["records"][0].update(sequence=2), "SEQUENCE_INVALID"),
        (
            lambda value: value["records"][0].update(envelope_sha256="c" * 64),
            "ENVELOPE_BINDING_MISMATCH",
        ),
        (lambda value: value["records"][0].update(record_sha256="d" * 64), "HASH_MISMATCH"),
        (lambda value: value.update(current_state="ARCHIVED"), "CURRENT_STATE_MISMATCH"),
    ],
)
def test_rehashed_broken_internal_provenance_refuses(mutation, error: str) -> None:
    lifecycle = _lifecycle()
    mutation(lifecycle)
    lifecycle["lifecycle_sha256"] = _digest(
        {key: value for key, value in lifecycle.items() if key != "lifecycle_sha256"}
    )
    assert any(error in item for item in _evaluate(lifecycle)["errors"])


def test_contract_is_deterministic_and_incapable_of_actions() -> None:
    first = _evaluate()
    assert first == _evaluate()
    assert first["safety"]["advisory_only"] is True
    assert all(value is False for key, value in first["safety"].items() if key != "advisory_only")
