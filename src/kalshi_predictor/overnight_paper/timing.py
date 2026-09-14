"""Distinct market clocks and rule-supported settlement horizon qualification."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from .rule_verifier import RuleVerification
from .source_health import aware

CANONICAL_TIME_FIELDS = (
    "market_open_time",
    "market_close_time",
    "expected_expiration_time",
    "latest_expiration_time",
    "observation_time",
    "expected_settlement_time",
    "final_settlement_time",
    "settlement_deadline",
)
MAX_SETTLEMENT_HOURS = 72


@dataclass(frozen=True)
class TimingVerification:
    passed: bool
    blockers: tuple[str, ...]
    expected_settlement_time: datetime | None = None
    settlement_deadline: datetime | None = None


def canonical_market_times(market: dict[str, Any]) -> dict[str, datetime | None]:
    """Copy named fields only. No expiration-to-settlement or other aliases."""
    return {
        name: aware(market[name]) if market.get(name) is not None else None
        for name in CANONICAL_TIME_FIELDS
    }


def verify_settlement_horizon(
    *,
    decision: dict[str, Any],
    rule: RuleVerification,
    now: datetime,
) -> TimingVerification:
    """Require a certified, explicit final bound including review exceptions.

    final_settlement_time records actual observed settlement and must be absent
    before entry. settlement_deadline is a separate rule-supported latest bound.
    """
    try:
        if (
            not rule.passed
            or rule.policy is None
            or rule.rule_version != decision.get("rule_version")
        ):
            raise ValueError("CERTIFIED_TIMING_RULE_REQUIRED")
        policy = rule.policy
        if any(
            decision.get(name) != getattr(policy, name) for name in ("ticker", "event_id", "series")
        ):
            raise ValueError("TIMING_RULE_IDENTITY_MISMATCH")
        clocks = canonical_market_times(decision)
        if clocks["final_settlement_time"] is not None:
            raise ValueError("ACTUAL_SETTLEMENT_TIME_PRESENT_BEFORE_ENTRY")
        at = aware(now)
        observation = clocks["observation_time"]
        if observation is None or observation != aware(policy.observation_time):
            raise ValueError("RULE_OBSERVATION_TIME_MISMATCH")
        if policy.review_extension_seconds is None:
            raise ValueError("REVIEW_EXTENSION_UNKNOWN")
        expected = observation + timedelta(seconds=policy.expected_settlement_seconds)
        final = observation + timedelta(
            seconds=policy.final_settlement_seconds + policy.review_extension_seconds
        )
        if clocks["expected_settlement_time"] != expected or clocks["settlement_deadline"] != final:
            raise ValueError("EXPLICIT_RULE_SUPPORTED_SETTLEMENT_TIMES_REQUIRED")
        opened, closed = clocks["market_open_time"], clocks["market_close_time"]
        if (
            opened is None
            or closed is None
            or not opened <= at < closed <= observation <= expected <= final
        ):
            raise ValueError("MARKET_TIMING_ORDER_INVALID")
        # Both original decision and activation must fit the unchanged authorization.
        decision_at = aware(decision["decision_at"])
        if (
            decision_at > at
            or not 0 < (final - decision_at).total_seconds() <= MAX_SETTLEMENT_HOURS * 3600
        ):
            raise ValueError("SETTLEMENT_HORIZON_EXCEEDS_72H")
        if not 0 < (final - at).total_seconds() <= MAX_SETTLEMENT_HOURS * 3600:
            raise ValueError("SETTLEMENT_HORIZON_EXCEEDS_72H")
        return TimingVerification(True, (), expected, final)
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError) as exc:
        return TimingVerification(False, (str(exc) or "TIMING_EVIDENCE_INVALID",))
