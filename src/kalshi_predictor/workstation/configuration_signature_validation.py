from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import asdict, dataclass
from typing import Any, Literal

VALIDATION_SCHEMA_VERSION = "phase4jz-configuration-signature-validation-v1"
SIGNATURE_ALGORITHM = "HMAC_SHA256"
ValidationStatus = Literal["VALID", "INVALID", "INCOMPLETE", "TAMPERED"]


class ConfigurationSignatureValidationError(ValueError):
    """Stable fail-closed configuration signature validation error."""


@dataclass(frozen=True)
class ConfigurationSignatureEnvelope:
    configuration_hash: str
    key_id_hash: str
    algorithm: str
    signature_hex: str
    complete: bool
    envelope_hash: str


@dataclass(frozen=True)
class ConfigurationSignatureDecision:
    status: ValidationStatus
    reasons: tuple[str, ...]
    configuration_hash: str
    key_id_hash: str
    envelope_hash: str
    validation_hash: str
    decision_hash: str
    read_only: bool = True
    signature_valid: bool = False
    configuration_use_authorized: bool = False
    task_activation_authorized: bool = False
    restart_authorized: bool = False
    service_control_authorized: bool = False
    execution_authorized: bool = False


def make_configuration_signature_envelope(
    **fields: Any,
) -> ConfigurationSignatureEnvelope:
    _validate_envelope_fields(fields)
    return ConfigurationSignatureEnvelope(**fields, envelope_hash=_hash(fields))


def validate_configuration_signature(
    configuration: bytes,
    envelope: Any,
    *,
    trusted_key: bytes,
    trusted_key_id_hash: str,
    max_configuration_bytes: int = 64 * 1024,
) -> ConfigurationSignatureDecision:
    if not isinstance(configuration, bytes) or not isinstance(trusted_key, bytes):
        raise ConfigurationSignatureValidationError("CONFIG_SIGNATURE_INPUT_TYPE_INVALID")
    if (
        isinstance(max_configuration_bytes, bool)
        or not isinstance(max_configuration_bytes, int)
        or max_configuration_bytes <= 0
        or len(configuration) > max_configuration_bytes
    ):
        raise ConfigurationSignatureValidationError("CONFIG_SIGNATURE_BOUND_INVALID")
    if len(trusted_key) < 32:
        raise ConfigurationSignatureValidationError("CONFIG_SIGNATURE_KEY_INVALID")
    if (
        not isinstance(trusted_key_id_hash, str)
        or re.fullmatch(r"[0-9a-f]{64}", trusted_key_id_hash) is None
    ):
        raise ConfigurationSignatureValidationError("CONFIG_SIGNATURE_KEY_ID_INVALID")
    item = _validated_envelope(envelope)
    observed_hash = hashlib.sha256(configuration).hexdigest()
    expected_signature = hmac.new(trusted_key, configuration, hashlib.sha256).hexdigest()
    if not item.complete:
        status: ValidationStatus = "INCOMPLETE"
        reasons = ["CONFIG_SIGNATURE_ENVELOPE_INCOMPLETE"]
    elif item.algorithm != SIGNATURE_ALGORITHM:
        status = "TAMPERED"
        reasons = ["CONFIG_SIGNATURE_ALGORITHM_INVALID"]
    elif item.key_id_hash != trusted_key_id_hash:
        status = "TAMPERED"
        reasons = ["CONFIG_SIGNATURE_KEY_ID_MISMATCH"]
    else:
        reasons = []
        if item.configuration_hash != observed_hash:
            reasons.append("CONFIG_SIGNATURE_CONFIGURATION_HASH_MISMATCH")
        if not hmac.compare_digest(item.signature_hex, expected_signature):
            reasons.append("CONFIG_SIGNATURE_MISMATCH")
        status = "INVALID" if reasons else "VALID"
    valid = status == "VALID"
    validation_hash = _hash(
        {
            "configuration_hash": observed_hash,
            "key_id_hash": trusted_key_id_hash,
            "algorithm": SIGNATURE_ALGORITHM,
            "signature_valid": valid,
        }
    )
    unsigned = {
        "schema_version": VALIDATION_SCHEMA_VERSION,
        "status": status,
        "reasons": reasons,
        "configuration_hash": observed_hash,
        "key_id_hash": trusted_key_id_hash,
        "envelope_hash": item.envelope_hash,
        "validation_hash": validation_hash,
        "read_only": True,
        "signature_valid": valid,
        "configuration_use_authorized": False,
        "task_activation_authorized": False,
        "restart_authorized": False,
        "service_control_authorized": False,
        "execution_authorized": False,
    }
    return ConfigurationSignatureDecision(
        status=status,
        reasons=tuple(reasons),
        configuration_hash=observed_hash,
        key_id_hash=trusted_key_id_hash,
        envelope_hash=item.envelope_hash,
        validation_hash=validation_hash,
        decision_hash=_hash(unsigned),
        signature_valid=valid,
    )


def validate_configuration_signature_decision(value: Any) -> None:
    if not isinstance(value, ConfigurationSignatureDecision):
        raise ConfigurationSignatureValidationError("CONFIG_SIGNATURE_DECISION_TYPE_INVALID")
    if (
        value.read_only is not True
        or value.signature_valid != (value.status == "VALID")
        or any(
            (
                value.configuration_use_authorized,
                value.task_activation_authorized,
                value.restart_authorized,
                value.service_control_authorized,
                value.execution_authorized,
            )
        )
    ):
        raise ConfigurationSignatureValidationError("CONFIG_SIGNATURE_SAFETY_BOUNDARY_INVALID")
    unsigned = asdict(value)
    unsigned.pop("decision_hash")
    unsigned["schema_version"] = VALIDATION_SCHEMA_VERSION
    unsigned["reasons"] = list(unsigned["reasons"])
    if value.decision_hash != _hash(unsigned):
        raise ConfigurationSignatureValidationError("CONFIG_SIGNATURE_DECISION_HASH_MISMATCH")


def _validated_envelope(value: Any) -> ConfigurationSignatureEnvelope:
    if not isinstance(value, ConfigurationSignatureEnvelope):
        raise ConfigurationSignatureValidationError("CONFIG_SIGNATURE_ENVELOPE_TYPE_INVALID")
    unsigned = asdict(value)
    supplied = unsigned.pop("envelope_hash")
    _validate_envelope_fields(unsigned)
    if supplied != _hash(unsigned):
        raise ConfigurationSignatureValidationError("CONFIG_SIGNATURE_ENVELOPE_HASH_MISMATCH")
    return value


def _validate_envelope_fields(fields: dict[str, Any]) -> None:
    required = {"configuration_hash", "key_id_hash", "algorithm", "signature_hex", "complete"}
    if set(fields) != required:
        raise ConfigurationSignatureValidationError("CONFIG_SIGNATURE_ENVELOPE_FIELD_INVALID")
    for key in ("configuration_hash", "key_id_hash", "signature_hex"):
        if not isinstance(fields[key], str) or re.fullmatch(r"[0-9a-f]{64}", fields[key]) is None:
            raise ConfigurationSignatureValidationError("CONFIG_SIGNATURE_ENVELOPE_FIELD_INVALID")
    if (
        not isinstance(fields["algorithm"], str)
        or re.fullmatch(r"[A-Z][A-Z0-9_]{0,31}", fields["algorithm"]) is None
        or not isinstance(fields["complete"], bool)
    ):
        raise ConfigurationSignatureValidationError("CONFIG_SIGNATURE_ENVELOPE_FIELD_INVALID")


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
