import hashlib
import hmac
from dataclasses import replace

import pytest

from kalshi_predictor.workstation.configuration_signature_validation import (
    ConfigurationSignatureValidationError,
    make_configuration_signature_envelope,
    validate_configuration_signature,
    validate_configuration_signature_decision,
)

CONFIG = b'{"activation":false,"mode":"paper-only"}'
KEY = b"fixture-signature-key-material-32-bytes-minimum"
KEY_ID = "1" * 64


def _envelope(configuration=CONFIG, key=KEY, **overrides):
    fields = dict(
        configuration_hash=hashlib.sha256(configuration).hexdigest(),
        key_id_hash=KEY_ID,
        algorithm="HMAC_SHA256",
        signature_hex=hmac.new(key, configuration, hashlib.sha256).hexdigest(),
        complete=True,
    )
    fields.update(overrides)
    return make_configuration_signature_envelope(**fields)


def _validate(configuration=CONFIG, envelope=None, **overrides):
    return validate_configuration_signature(
        configuration,
        envelope or _envelope(configuration),
        trusted_key=overrides.get("trusted_key", KEY),
        trusted_key_id_hash=overrides.get("trusted_key_id_hash", KEY_ID),
        max_configuration_bytes=overrides.get("max_configuration_bytes", 64 * 1024),
    )


def test_valid_signature_is_deterministic_without_configuration_authority() -> None:
    first = _validate()
    assert first == _validate() and first.status == "VALID" and first.signature_valid
    assert not any(
        (
            first.configuration_use_authorized,
            first.task_activation_authorized,
            first.restart_authorized,
        )
    )
    assert KEY.hex() not in repr(first)
    validate_configuration_signature_decision(first)


def test_configuration_signature_and_key_mismatch_fail_closed() -> None:
    assert _validate(b'{"activation":true}', _envelope()).status == "INVALID"
    assert _validate(envelope=_envelope(signature_hex="0" * 64)).status == "INVALID"
    assert _validate(trusted_key=b"x" * 32).status == "INVALID"
    assert _validate(trusted_key_id_hash="2" * 64).status == "TAMPERED"


def test_algorithm_incomplete_malformed_and_bounds_fail_closed() -> None:
    assert _validate(envelope=_envelope(algorithm="SHA256")).status == "TAMPERED"
    assert _validate(envelope=_envelope(complete=False)).status == "INCOMPLETE"
    with pytest.raises(ConfigurationSignatureValidationError, match="FIELD_INVALID"):
        _envelope(signature_hex="bad")
    with pytest.raises(ConfigurationSignatureValidationError, match="BOUND_INVALID"):
        _validate(max_configuration_bytes=1)
    with pytest.raises(ConfigurationSignatureValidationError, match="KEY_INVALID"):
        _validate(trusted_key=b"short")


def test_envelope_decision_and_authority_tampering_fail_closed() -> None:
    envelope = _envelope()
    with pytest.raises(ConfigurationSignatureValidationError, match="ENVELOPE_HASH_MISMATCH"):
        _validate(envelope=replace(envelope, signature_hex="0" * 64))
    decision = _validate()
    with pytest.raises(ConfigurationSignatureValidationError, match="DECISION_HASH_MISMATCH"):
        validate_configuration_signature_decision(replace(decision, validation_hash="f" * 64))
    with pytest.raises(ConfigurationSignatureValidationError, match="SAFETY_BOUNDARY"):
        validate_configuration_signature_decision(
            replace(decision, configuration_use_authorized=True)
        )


def test_validator_has_no_secret_persistence_or_operational_surface() -> None:
    forbidden = {"open", "write", "run", "Popen", "subprocess", "spawn", "schtasks", "shutdown"}
    assert forbidden.isdisjoint(validate_configuration_signature.__code__.co_names)
