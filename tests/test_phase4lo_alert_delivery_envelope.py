from __future__ import annotations

import copy
import hashlib
import json

import pytest

from scripts.local.phase4lo_alert_delivery_envelope import create_envelope, validate_envelope


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _decision(state: str = "EMIT", verdict: str = "PASS") -> dict[str, object]:
    value: dict[str, object] = {
        "schema": "phase4ln.alert-state-contract.v1",
        "verdict": verdict,
        "decision": state,
        "alert_key": "a" * 64,
        "severity": "WARNING",
    }
    value["decision_sha256"] = _digest(value)
    return value


def _payload() -> dict[str, str]:
    return {
        "title": "Runtime warning",
        "summary": "UI unavailable",
        "recommended_action": "Monitor",
    }


def _create(decision=None, route="local_ui", expires="2026-08-28T20:10:00Z", payload=None):
    return create_envelope(
        decision or _decision(),
        route=route,
        created_at="2026-08-28T20:00:00Z",
        expires_at=expires,
        payload=payload or _payload(),
    )


def test_valid_envelope_is_deterministic_and_inert() -> None:
    first = _create()
    assert first == _create()
    assert first["verdict"] == "PASS"
    assert first["envelope"]["transport"] == "none"
    assert first["envelope"]["safety"]["delivery_enabled"] is False


@pytest.mark.parametrize("state,verdict", [("SUPPRESS", "PASS"), ("REFUSE", "REFUSE")])
def test_non_emitting_decision_cannot_create_envelope(state: str, verdict: str) -> None:
    assert _create(_decision(state, verdict))["envelope"] is None


def test_unknown_route_extra_payload_and_long_ttl_refuse() -> None:
    assert _create(route="email")["verdict"] == "REFUSE"
    assert _create(payload={**_payload(), "url": "https://example.invalid"})["verdict"] == "REFUSE"
    assert _create(expires="2026-08-28T21:00:00Z")["verdict"] == "REFUSE"


def test_validates_binding_freshness_and_duplicate_state() -> None:
    decision = _decision()
    envelope = _create(decision)["envelope"]
    passing = validate_envelope(
        envelope, decision, evaluated_at="2026-08-28T20:05:00Z", seen_hashes=[]
    )
    duplicate = validate_envelope(
        envelope,
        decision,
        evaluated_at="2026-08-28T20:05:00Z",
        seen_hashes=[envelope["envelope_sha256"]],
    )
    expired = validate_envelope(
        envelope, decision, evaluated_at="2026-08-28T20:10:00Z", seen_hashes=[]
    )
    assert passing["verdict"] == "PASS"
    assert "DUPLICATE_ENVELOPE" in duplicate["errors"]
    assert "ENVELOPE_EXPIRED" in expired["errors"]


@pytest.mark.parametrize(
    "field", ["severity", "alert_key", "decision_sha256", "template_version", "destination"]
)
def test_mutable_bound_fields_are_rejected(field: str) -> None:
    decision = _decision()
    envelope = copy.deepcopy(_create(decision)["envelope"])
    envelope[field] = "tampered"
    envelope["envelope_sha256"] = _digest(
        {key: value for key, value in envelope.items() if key != "envelope_sha256"}
    )
    result = validate_envelope(
        envelope, decision, evaluated_at="2026-08-28T20:05:00Z", seen_hashes=[]
    )
    assert result["verdict"] == "REFUSE"


def test_future_dated_and_hash_tampered_envelopes_refuse() -> None:
    decision = _decision()
    envelope = copy.deepcopy(_create(decision)["envelope"])
    future = validate_envelope(
        envelope, decision, evaluated_at="2026-08-28T19:59:59Z", seen_hashes=[]
    )
    envelope["payload"]["summary"] = "changed"
    tampered = validate_envelope(
        envelope, decision, evaluated_at="2026-08-28T20:05:00Z", seen_hashes=[]
    )
    assert "ENVELOPE_FROM_FUTURE" in future["errors"]
    assert "ENVELOPE_HASH_MISMATCH" in tampered["errors"]


@pytest.mark.parametrize(
    "mutation,error",
    [
        (lambda envelope: envelope.update(schema="wrong"), "ENVELOPE_SCHEMA_INVALID"),
        (
            lambda envelope: envelope["payload"].update(summary="x" * 501),
            "PAYLOAD_VALUE_INVALID",
        ),
        (
            lambda envelope: envelope["safety"].update(delivery_enabled=True),
            "DELIVERY_SAFETY_INVALID",
        ),
    ],
)
def test_rehashed_invalid_envelope_still_refuses(mutation, error: str) -> None:
    decision = _decision()
    envelope = copy.deepcopy(_create(decision)["envelope"])
    mutation(envelope)
    envelope["envelope_sha256"] = _digest(
        {key: value for key, value in envelope.items() if key != "envelope_sha256"}
    )
    result = validate_envelope(
        envelope,
        decision,
        evaluated_at="2026-08-28T20:05:00Z",
        seen_hashes=[],
    )
    assert error in result["errors"]


def test_validation_has_no_delivery_capability() -> None:
    decision = _decision()
    envelope = _create(decision)["envelope"]
    safety = validate_envelope(
        envelope, decision, evaluated_at="2026-08-28T20:05:00Z", seen_hashes=[]
    )["safety"]
    assert safety["validation_only"] is True
    assert all(value is False for key, value in safety.items() if key != "validation_only")
