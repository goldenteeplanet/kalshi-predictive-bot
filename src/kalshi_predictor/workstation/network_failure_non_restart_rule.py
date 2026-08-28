from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import Any, Literal

RULE_SCHEMA_VERSION = "phase4id-network-failure-non-restart-rule-v1"
NETWORK_FAILURE_CODES = frozenset(
    {
        "DNS_FAILURE",
        "TCP_TIMEOUT",
        "TLS_FAILURE",
        "HTTP_5XX",
        "ROUTE_UNREACHABLE",
        "CONNECTION_RESET",
    }
)
NetworkStatus = Literal["HEALTHY", "NETWORK_FAILURE", "UNKNOWN", "INCOMPLETE", "TAMPERED"]


class NetworkFailureNonRestartRuleError(ValueError):
    """Stable fail-closed network failure non-restart rule error."""


@dataclass(frozen=True)
class NetworkFailureEvidence:
    probe_id_hash: str
    observed_at_epoch_seconds: int
    endpoint_id_hash: str
    failure_code: str
    connectivity_succeeded: bool
    complete: bool
    evidence_hash: str


@dataclass(frozen=True)
class NetworkFailureDecision:
    status: NetworkStatus
    reasons: tuple[str, ...]
    evidence_hash: str
    failure_code: str
    evaluated_at_epoch_seconds: int
    decision_hash: str
    read_only: bool = True
    connectivity_proven: bool = False
    retry_policy_required: bool = False
    operator_alert_required: bool = True
    restart_eligible: bool = False
    recovery_authorized: bool = False
    service_control_authorized: bool = False
    host_restart_authorized: bool = False
    execution_authorized: bool = False


def make_network_failure_evidence(**fields: Any) -> NetworkFailureEvidence:
    _validate_fields(fields)
    return NetworkFailureEvidence(**fields, evidence_hash=_hash(fields))


def evaluate_network_failure_non_restart_rule(
    evidence: Any, *, evaluated_at_epoch_seconds: int, maximum_age_seconds: int = 120
) -> NetworkFailureDecision:
    for value in (evaluated_at_epoch_seconds, maximum_age_seconds):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise NetworkFailureNonRestartRuleError("NETWORK_RULE_BOUND_INVALID")
    item = _validated_evidence(evidence)
    coherent_success = item.connectivity_succeeded and item.failure_code == "NONE"
    coherent_failure = (
        not item.connectivity_succeeded and item.failure_code in NETWORK_FAILURE_CODES
    )
    if item.observed_at_epoch_seconds > evaluated_at_epoch_seconds:
        status: NetworkStatus = "TAMPERED"
        reasons = ["NETWORK_EVIDENCE_FROM_FUTURE"]
    elif evaluated_at_epoch_seconds - item.observed_at_epoch_seconds > maximum_age_seconds:
        status = "UNKNOWN"
        reasons = ["NETWORK_EVIDENCE_STALE"]
    elif not item.complete:
        status = "INCOMPLETE"
        reasons = ["NETWORK_EVIDENCE_INCOMPLETE"]
    elif coherent_success:
        status = "HEALTHY"
        reasons = []
    elif coherent_failure:
        status = "NETWORK_FAILURE"
        reasons = [f"NETWORK_FAILURE_NON_RESTARTABLE:{item.failure_code}"]
    else:
        status = "UNKNOWN"
        reasons = ["NETWORK_EVIDENCE_CONTRADICTORY_OR_UNKNOWN"]
    healthy = status == "HEALTHY"
    failure = status == "NETWORK_FAILURE"
    unsigned = {
        "schema_version": RULE_SCHEMA_VERSION,
        "status": status,
        "reasons": reasons,
        "evidence_hash": item.evidence_hash,
        "failure_code": item.failure_code,
        "evaluated_at_epoch_seconds": evaluated_at_epoch_seconds,
        "read_only": True,
        "connectivity_proven": healthy,
        "retry_policy_required": failure,
        "operator_alert_required": not healthy,
        "restart_eligible": False,
        "recovery_authorized": False,
        "service_control_authorized": False,
        "host_restart_authorized": False,
        "execution_authorized": False,
    }
    return NetworkFailureDecision(
        status=status,
        reasons=tuple(reasons),
        evidence_hash=item.evidence_hash,
        failure_code=item.failure_code,
        evaluated_at_epoch_seconds=evaluated_at_epoch_seconds,
        decision_hash=_hash(unsigned),
        connectivity_proven=healthy,
        retry_policy_required=failure,
        operator_alert_required=not healthy,
    )


def validate_network_failure_decision(value: Any) -> None:
    if not isinstance(value, NetworkFailureDecision):
        raise NetworkFailureNonRestartRuleError("NETWORK_DECISION_TYPE_INVALID")
    if (
        value.read_only is not True
        or value.restart_eligible is not False
        or any(
            (
                value.recovery_authorized,
                value.service_control_authorized,
                value.host_restart_authorized,
                value.execution_authorized,
            )
        )
    ):
        raise NetworkFailureNonRestartRuleError("NETWORK_SAFETY_BOUNDARY_INVALID")
    if value.connectivity_proven != (value.status == "HEALTHY"):
        raise NetworkFailureNonRestartRuleError("NETWORK_STATUS_INVALID")
    unsigned = asdict(value)
    unsigned.pop("decision_hash")
    unsigned["schema_version"] = RULE_SCHEMA_VERSION
    unsigned["reasons"] = list(unsigned["reasons"])
    if value.decision_hash != _hash(unsigned):
        raise NetworkFailureNonRestartRuleError("NETWORK_DECISION_HASH_MISMATCH")


def _validated_evidence(value: Any) -> NetworkFailureEvidence:
    if not isinstance(value, NetworkFailureEvidence):
        raise NetworkFailureNonRestartRuleError("NETWORK_EVIDENCE_TYPE_INVALID")
    unsigned = asdict(value)
    supplied = unsigned.pop("evidence_hash")
    _validate_fields(unsigned)
    if supplied != _hash(unsigned):
        raise NetworkFailureNonRestartRuleError("NETWORK_EVIDENCE_HASH_MISMATCH")
    return value


def _validate_fields(fields: dict[str, Any]) -> None:
    required = {
        "probe_id_hash",
        "observed_at_epoch_seconds",
        "endpoint_id_hash",
        "failure_code",
        "connectivity_succeeded",
        "complete",
    }
    if set(fields) != required:
        raise NetworkFailureNonRestartRuleError("NETWORK_EVIDENCE_FIELD_INVALID")
    for key in ("probe_id_hash", "endpoint_id_hash"):
        if not isinstance(fields[key], str) or re.fullmatch(r"[0-9a-f]{64}", fields[key]) is None:
            raise NetworkFailureNonRestartRuleError("NETWORK_EVIDENCE_FIELD_INVALID")
    timestamp = fields["observed_at_epoch_seconds"]
    if isinstance(timestamp, bool) or not isinstance(timestamp, int) or timestamp < 0:
        raise NetworkFailureNonRestartRuleError("NETWORK_EVIDENCE_FIELD_INVALID")
    if (
        not isinstance(fields["failure_code"], str)
        or re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", fields["failure_code"]) is None
    ):
        raise NetworkFailureNonRestartRuleError("NETWORK_EVIDENCE_FIELD_INVALID")
    if not isinstance(fields["connectivity_succeeded"], bool) or not isinstance(
        fields["complete"], bool
    ):
        raise NetworkFailureNonRestartRuleError("NETWORK_EVIDENCE_FIELD_INVALID")


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
