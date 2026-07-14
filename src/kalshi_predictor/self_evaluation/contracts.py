from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from kalshi_predictor.utils.decimals import decimal_to_str

SCHEMA_VERSION = "1.0.0"
METRIC_VERSION = "1.0.0"
JOURNAL_STATUS_FINAL = "FINAL"
JOURNAL_STATUS_PROVISIONAL = "PROVISIONAL"
JOURNAL_STATUS_INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
JOURNAL_STATUS_NO_ACTIVITY = "NO_ACTIVITY"
JOURNAL_STATUS_FAILED = "FAILED"
HUMAN_REVIEW_REQUIRED = "HUMAN_REVIEW_REQUIRED"

REQUIRED_JOURNAL_KEYS = {
    "schema_version",
    "journal_id",
    "evaluation_run_id",
    "journal_revision",
    "journal_status",
    "trading_session",
    "evaluation_as_of",
    "generated_at",
    "data_mode",
    "source_manifest_summary",
    "coverage_summary",
    "headline",
    "executive_summary",
    "what_worked",
    "what_failed",
    "what_changed",
    "watch_items",
    "data_quality_items",
    "risk_and_sizing_summary",
    "forecast_and_opportunity_summary",
    "trade_and_execution_summary",
    "model_and_version_summary",
    "key_metrics",
    "unresolved_outcomes",
    "recommended_follow_ups",
    "caveats",
    "evidence_appendix",
}

UNSUPPORTED_CAUSAL_PHRASES = (
    "caused",
    "proved",
    "proof that",
    "because of",
    "guarantees",
)


@dataclass(frozen=True)
class TradingSession:
    trading_session_id: str
    calendar_id: str
    session_label: str
    session_timezone: str
    session_open_at: datetime
    session_close_at: datetime
    evaluation_window_start: datetime
    evaluation_window_end: datetime
    is_holiday: bool = False
    is_early_close: bool = False
    includes_overnight_hours: bool = False

    def as_payload(self) -> dict[str, Any]:
        return {
            "trading_session_id": self.trading_session_id,
            "calendar_id": self.calendar_id,
            "session_label": self.session_label,
            "session_timezone": self.session_timezone,
            "session_open_at": self.session_open_at.isoformat(),
            "session_close_at": self.session_close_at.isoformat(),
            "evaluation_window_start": self.evaluation_window_start.isoformat(),
            "evaluation_window_end": self.evaluation_window_end.isoformat(),
            "is_holiday": self.is_holiday,
            "is_early_close": self.is_early_close,
            "includes_overnight_hours": self.includes_overnight_hours,
        }


@dataclass(frozen=True)
class Baseline:
    baseline_type: str
    sample_size: int = 0
    value: str | None = None
    window: str | None = None
    fallback_used: bool = True

    def as_payload(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class MetricRecord:
    metric_record_id: str
    metric_name: str
    metric_type: str
    section: str
    value: str | None
    unit: str | None
    sample_size: int
    cohort: dict[str, Any]
    finalized_count: int = 0
    pending_count: int = 0
    reliability_grade: str = "INSUFFICIENT_DATA"
    metric_version: str = METRIC_VERSION
    baseline: Baseline = field(default_factory=lambda: Baseline("NONE"))
    evidence_references: list[dict[str, str]] = field(default_factory=list)
    notes: str = ""

    def as_payload(self) -> dict[str, Any]:
        return {
            "metric_record_id": self.metric_record_id,
            "metric_name": self.metric_name,
            "metric_type": self.metric_type,
            "metric_version": self.metric_version,
            "section": self.section,
            "cohort": self.cohort,
            "value": _json_safe_value(self.value),
            "unit": self.unit,
            "sample_size": self.sample_size,
            "finalized_count": self.finalized_count,
            "pending_count": self.pending_count,
            "baseline": self.baseline.as_payload(),
            "reliability_grade": self.reliability_grade,
            "evidence_references": self.evidence_references,
            "notes": self.notes,
        }


@dataclass(frozen=True)
class FindingRecord:
    finding_id: str
    finding_type: str
    finding_subtype: str
    severity: str
    status: str
    title: str
    concise_statement: str
    detailed_explanation: str
    primary_metric_record_id: str | None
    sample_size: int
    reliability_grade: str
    evidence_type: str = "OBSERVED"
    attribution_level: str = "OBSERVATION"
    current_value: str | None = None
    baseline: Baseline = field(default_factory=lambda: Baseline("NONE"))
    absolute_delta: str | None = None
    relative_delta: str | None = None
    effect_size: str | None = None
    confidence_interval: dict[str, Any] | None = None
    cohort: dict[str, Any] = field(default_factory=dict)
    evidence_references: list[dict[str, str]] = field(default_factory=list)
    reason_codes: list[str] = field(default_factory=list)
    consecutive_sessions_detected: int = 1
    hypothesis: str | None = None
    recommended_follow_up_ids: list[str] = field(default_factory=list)

    def as_payload(self) -> dict[str, Any]:
        payload = {
            "finding_id": self.finding_id,
            "finding_type": self.finding_type,
            "finding_subtype": self.finding_subtype,
            "severity": self.severity,
            "status": self.status,
            "title": self.title,
            "concise_statement": self.concise_statement,
            "detailed_explanation": self.detailed_explanation,
            "primary_metric_record_id": self.primary_metric_record_id,
            "supporting_metric_record_ids": [],
            "cohort": self.cohort,
            "current_value": _json_safe_value(self.current_value),
            "baseline": self.baseline.as_payload(),
            "absolute_delta": _json_safe_value(self.absolute_delta),
            "relative_delta": _json_safe_value(self.relative_delta),
            "effect_size": _json_safe_value(self.effect_size),
            "sample_size": self.sample_size,
            "confidence_interval": self.confidence_interval,
            "reliability_grade": self.reliability_grade,
            "evidence_type": self.evidence_type,
            "attribution_level": self.attribution_level,
            "evidence_references": self.evidence_references,
            "reason_codes": self.reason_codes,
            "consecutive_sessions_detected": self.consecutive_sessions_detected,
            "recommended_follow_up_ids": self.recommended_follow_up_ids,
        }
        if self.hypothesis:
            payload["hypothesis"] = self.hypothesis
        return payload


def stable_phase_3p_id(*parts: Any) -> str:
    text = "|".join(str(part) for part in parts)
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"kalshi_predictor:phase_3p:{text}"))


def checksum_payload(value: Any) -> str:
    encoded = canonical_json(value)
    return f"sha256:{hashlib.sha256(encoded.encode('utf-8')).hexdigest()}"


def canonical_json(value: Any) -> str:
    return json.dumps(_json_safe(value), sort_keys=True, separators=(",", ":"), default=str)


def validate_journal_payload(payload: dict[str, Any]) -> None:
    missing = sorted(REQUIRED_JOURNAL_KEYS - payload.keys())
    if missing:
        raise ValueError(f"Phase 3P journal payload missing keys: {', '.join(missing)}")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"Unsupported Phase 3P journal schema: {payload.get('schema_version')}")
    for key in ("what_worked", "what_failed", "what_changed"):
        if not isinstance(payload.get(key), list):
            raise ValueError(f"{key} must be a list.")
    for section in (
        "what_worked",
        "what_failed",
        "what_changed",
        "watch_items",
        "data_quality_items",
    ):
        for finding in payload.get(section, []):
            _validate_finding(finding)
    _assert_no_bad_numbers(payload)


def decimal_string(value: Decimal | int | float | str | None) -> str | None:
    if value is None:
        return None
    decimal = Decimal(str(value))
    if not decimal.is_finite():
        raise ValueError("Phase 3P metrics cannot contain NaN or infinity.")
    return decimal_to_str(decimal)


def reliability_grade(sample_size: int, *, minimum_current_sample: int) -> str:
    if sample_size <= 0:
        return "INSUFFICIENT_DATA"
    if sample_size < minimum_current_sample:
        return "LOW"
    if sample_size >= max(30, minimum_current_sample * 3):
        return "HIGH"
    return "MEDIUM"


def _validate_finding(finding: dict[str, Any]) -> None:
    required = {
        "finding_id",
        "finding_type",
        "finding_subtype",
        "severity",
        "status",
        "title",
        "concise_statement",
        "sample_size",
        "reliability_grade",
        "evidence_references",
    }
    missing = sorted(required - finding.keys())
    if missing:
        raise ValueError(f"Finding {finding.get('finding_id') or '<unknown>'} missing {missing}")
    if not finding["evidence_references"]:
        raise ValueError(f"Finding {finding['finding_id']} has no evidence references.")
    text = " ".join(
        str(finding.get(key) or "")
        for key in ("title", "concise_statement", "detailed_explanation", "hypothesis")
    ).lower()
    if _contains_unsupported_causal_phrase(text):
        raise ValueError(f"Finding {finding['finding_id']} uses unsupported causal language.")


def _assert_no_bad_numbers(value: Any) -> None:
    if isinstance(value, dict):
        for item in value.values():
            _assert_no_bad_numbers(item)
    elif isinstance(value, list):
        for item in value:
            _assert_no_bad_numbers(item)
    elif isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        raise ValueError("Phase 3P journal cannot contain NaN or infinity.")


def _contains_unsupported_causal_phrase(text: str) -> bool:
    for phrase in UNSUPPORTED_CAUSAL_PHRASES:
        pattern = r"\b" + re.escape(phrase) + r"\b"
        if re.search(pattern, text):
            return True
    return False


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat() if value.tzinfo else value.isoformat()
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("Phase 3P payload cannot contain non-finite decimals.")
        return decimal_to_str(value)
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        raise ValueError("Phase 3P payload cannot contain NaN or infinity.")
    return value


def _json_safe_value(value: Any) -> Any:
    if value is None:
        return None
    decimal = None
    try:
        decimal = Decimal(str(value))
    except Exception:
        return value
    if decimal.is_finite():
        return float(decimal) if "." in str(value) else int(decimal)
    raise ValueError("Phase 3P payload cannot contain NaN or infinity.")
