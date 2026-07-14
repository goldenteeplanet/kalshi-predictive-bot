from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from kalshi_predictor.utils.decimals import decimal_to_str

READINESS_DECISION_SCHEMA_VERSION = "phase-3v-readiness-decision-v1"
EVIDENCE_MANIFEST_SCHEMA_VERSION = "phase-3v-evidence-manifest-v1"
CERTIFICATE_SCHEMA_VERSION = "phase-3v-live-readiness-certificate-v1"
CONTROL_CATALOG_VERSION = "phase_3v_control_catalog_v1"
EVALUATOR_VERSION = "phase_3v_evaluator_v1"
REPORT_VERSION = "phase_3v_report_v1"

TARGET_ENVIRONMENT = "PRODUCTION"
STAGE_MICRO = "MICRO"
STAGE_CONSTRAINED = "CONSTRAINED"
STAGE_FULL = "FULL"
TARGET_STAGES = {STAGE_MICRO, STAGE_CONSTRAINED, STAGE_FULL}

DECISION_GO = "GO"
DECISION_CONDITIONAL_GO = "CONDITIONAL_GO"
DECISION_NO_GO = "NO_GO"
DECISION_INCOMPLETE = "INCOMPLETE"

STATUS_PASS = "PASS"
STATUS_FAIL = "FAIL"
STATUS_NOT_TESTED = "NOT_TESTED"
STATUS_STALE = "STALE"
STATUS_CONFLICTED = "CONFLICTED"
STATUS_UNVERIFIABLE = "UNVERIFIABLE"
STATUS_NA = "N_A"
CONTROL_STATUSES = {
    STATUS_PASS,
    STATUS_FAIL,
    STATUS_NOT_TESTED,
    STATUS_STALE,
    STATUS_CONFLICTED,
    STATUS_UNVERIFIABLE,
    STATUS_NA,
}

SEVERITY_CRITICAL = "CRITICAL"
SEVERITY_HIGH = "HIGH"
SEVERITY_MEDIUM = "MEDIUM"
SEVERITY_LOW = "LOW"

BLOCKING_STATUSES = {
    STATUS_FAIL,
    STATUS_STALE,
    STATUS_CONFLICTED,
    STATUS_UNVERIFIABLE,
}

REASON_SCOPE_UNVERIFIABLE = "SCOPE_UNVERIFIABLE"
REASON_MANDATORY_CONTROL_NOT_TESTED = "MANDATORY_CONTROL_NOT_TESTED"
REASON_CRITICAL_CONTROL_FAILED = "CRITICAL_CONTROL_FAILED"
REASON_HIGH_CONTROL_FAILED = "HIGH_CONTROL_FAILED"
REASON_REQUIRED_APPROVAL_MISSING = "REQUIRED_HUMAN_APPROVAL_MISSING"
REASON_CERTIFICATE_DISABLED = "CERTIFICATE_ISSUANCE_DISABLED"
REASON_CERTIFICATE_INVALID = "READINESS_CERTIFICATE_INVALID"
REASON_CERTIFICATE_EXPIRED = "READINESS_CERTIFICATE_EXPIRED"
REASON_CERTIFICATE_REVOKED = "READINESS_CERTIFICATE_REVOKED"
REASON_CERTIFICATE_SCOPE_MISMATCH = "READINESS_CERTIFICATE_SCOPE_MISMATCH"
REASON_CERTIFICATE_STAGE_MISMATCH = "READINESS_CERTIFICATE_STAGE_MISMATCH"
REASON_ORDER_EXCEEDS_ENVELOPE = "ORDER_EXCEEDS_LAUNCH_ENVELOPE"
REASON_CANCEL_ONLY = "CANCEL_ONLY_ALLOWED"


@dataclass(frozen=True)
class LiveReadinessConfig:
    enabled: bool
    mode: str
    default_target_stage: str
    certificate_issuance_enabled: bool
    certificate_max_lifetime_hours: int
    evidence_stale_after_days: int
    required_approval_roles: tuple[str, ...]
    micro_max_contracts_per_order: int
    constrained_max_contracts_per_order: int
    full_max_contracts_per_order: int

    def validate(self) -> None:
        if self.default_target_stage not in TARGET_STAGES:
            raise ValueError("PHASE_3V_DEFAULT_TARGET_STAGE must be MICRO, CONSTRAINED, or FULL.")
        if self.certificate_max_lifetime_hours <= 0:
            raise ValueError("PHASE_3V_CERTIFICATE_MAX_LIFETIME_HOURS must be positive.")
        if self.evidence_stale_after_days <= 0:
            raise ValueError("PHASE_3V_EVIDENCE_STALE_AFTER_DAYS must be positive.")
        if min(
            self.micro_max_contracts_per_order,
            self.constrained_max_contracts_per_order,
            self.full_max_contracts_per_order,
        ) <= 0:
            raise ValueError("Phase 3V stage contract caps must be positive.")


@dataclass(frozen=True)
class ControlDefinition:
    control_id: str
    family: str
    severity: str
    title: str
    acceptance_criterion: str
    evidence_required: str

    def as_payload(self) -> dict[str, str]:
        return asdict(self)


def canonical_json(value: Any) -> str:
    return json.dumps(_json_safe(value), sort_keys=True, separators=(",", ":"), default=str)


def sha256_json(value: Any) -> str:
    return f"sha256:{hashlib.sha256(canonical_json(value).encode('utf-8')).hexdigest()}"


def sha256_text(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


def stable_id(prefix: str, *parts: Any) -> str:
    text = "|".join(str(part) for part in parts)
    return f"{prefix}_{uuid.uuid5(uuid.NAMESPACE_URL, f'kalshi_predictor:phase_3v:{text}')}"


def parse_required_roles(value: str | None) -> tuple[str, ...]:
    roles = [item.strip() for item in (value or "").split(",") if item.strip()]
    return tuple(roles or ["owner", "risk", "operator"])


def _json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return decimal_to_str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    return value

