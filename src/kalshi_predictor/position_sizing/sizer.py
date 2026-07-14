from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from math import isfinite
from typing import Any

from kalshi_predictor.config import Settings


class SizingMode(StrEnum):
    DISABLED = "disabled"
    SHADOW = "shadow"
    LIVE = "live"


class ConfidenceTier(StrEnum):
    BLOCKED = "blocked"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass(frozen=True)
class FactorWeights:
    confidence: float = 0.35
    opportunity: float = 0.25
    liquidity: float = 0.15
    historical_accuracy: float = 0.15
    drawdown_health: float = 0.10

    def validate(self) -> None:
        values = (
            self.confidence,
            self.opportunity,
            self.liquidity,
            self.historical_accuracy,
            self.drawdown_health,
        )
        if any((not isfinite(value) or value < 0.0) for value in values):
            raise ValueError("All factor weights must be finite and non-negative.")
        if abs(sum(values) - 1.0) > 1e-9:
            raise ValueError("Factor weights must sum to 1.0.")

    def as_dict(self) -> dict[str, float]:
        return {
            "confidence": self.confidence,
            "opportunity": self.opportunity,
            "liquidity": self.liquidity,
            "historical_accuracy": self.historical_accuracy,
            "drawdown_health": self.drawdown_health,
        }


@dataclass(frozen=True)
class PositionSizingConfig:
    mode: SizingMode = SizingMode.DISABLED
    version: str = "3M"
    live_max_contracts: int = 1
    global_max_contracts: int = 5

    low_contracts: int = 1
    medium_contracts: int = 3
    high_contracts: int = 5

    medium_score_threshold: float = 0.65
    high_score_threshold: float = 0.80

    medium_min_confidence: float = 0.65
    medium_min_opportunity: float = 0.60

    high_min_confidence: float = 0.80
    high_min_opportunity: float = 0.75
    high_min_adjusted_accuracy: float = 0.55
    high_min_history_samples: int = 30

    liquidity_one_contract_below: float = 0.45
    liquidity_three_contracts_below: float = 0.70

    drawdown_one_contract_at: float = 0.75
    drawdown_three_contracts_at: float = 0.50
    drawdown_kill_at: float = 1.00

    history_prior_accuracy: float = 0.50
    history_prior_weight: int = 20

    missing_external_risk_cap_defaults_to_one: bool = True
    weights: FactorWeights = FactorWeights()

    def validate(self) -> None:
        self.weights.validate()
        if not (0.0 <= self.medium_score_threshold < self.high_score_threshold <= 1.0):
            raise ValueError("Score thresholds must satisfy 0 <= medium < high <= 1.")
        if not (
            0.0
            <= self.liquidity_one_contract_below
            < self.liquidity_three_contracts_below
            <= 1.0
        ):
            raise ValueError("Liquidity thresholds must be ordered within [0, 1].")
        if not (
            0.0
            <= self.drawdown_three_contracts_at
            < self.drawdown_one_contract_at
            < self.drawdown_kill_at
        ):
            raise ValueError("Drawdown thresholds are not ordered correctly.")
        if self.history_prior_weight < 0 or self.high_min_history_samples < 0:
            raise ValueError("History sample settings must be non-negative.")
        if not (0.0 <= self.history_prior_accuracy <= 1.0):
            raise ValueError("Prior accuracy must be within [0, 1].")
        if (self.low_contracts, self.medium_contracts, self.high_contracts) != (1, 3, 5):
            raise ValueError("Phase 3M tier sizes must remain exactly 1, 3, and 5.")
        if self.live_max_contracts not in {1, 3, 5}:
            raise ValueError("live_max_contracts must be one of 1, 3, or 5.")
        if self.global_max_contracts not in {1, 3, 5}:
            raise ValueError("global_max_contracts must be one of 1, 3, or 5.")


@dataclass(frozen=True)
class PositionSizingInput:
    confidence_score: float | None
    opportunity_score: float | None
    liquidity_score: float | None
    current_drawdown_fraction: float | None
    max_drawdown_fraction: float | None
    historical_accuracy: float | None
    historical_sample_size: int | None
    decision_timestamp: datetime
    external_risk_cap: int | None = None
    margin_cap: int | None = None
    portfolio_cap: int | None = None
    hard_risk_block: bool = False


@dataclass(frozen=True)
class PositionSizingDecision:
    version: str
    mode: SizingMode
    tier: ConfidenceTier
    composite_score: float
    proposed_contracts: int
    live_candidate_contracts: int
    executed_contracts: int
    factor_scores: dict[str, float]
    factor_weights: dict[str, float]
    adjusted_historical_accuracy: float
    historical_sample_size: int
    drawdown_utilization: float
    caps: dict[str, int]
    limiting_factors: tuple[str, ...]
    reason_codes: tuple[str, ...]
    fallback_used: bool
    decision_timestamp: datetime

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "mode": self.mode.value,
            "tier": self.tier.value,
            "composite_score": self.composite_score,
            "proposed_contracts": self.proposed_contracts,
            "live_candidate_contracts": self.live_candidate_contracts,
            "executed_contracts": self.executed_contracts,
            "factor_scores": self.factor_scores,
            "factor_weights": self.factor_weights,
            "adjusted_historical_accuracy": self.adjusted_historical_accuracy,
            "historical_sample_size": self.historical_sample_size,
            "drawdown_utilization": self.drawdown_utilization,
            "caps": self.caps,
            "limiting_factors": list(self.limiting_factors),
            "reason_codes": list(self.reason_codes),
            "fallback_used": self.fallback_used,
            "decision_timestamp": self.decision_timestamp.isoformat(),
        }


class DynamicPositionSizer:
    _ALLOWED_SIZES = (0, 1, 3, 5)

    def __init__(self, config: PositionSizingConfig) -> None:
        config.validate()
        self.config = config

    @classmethod
    def from_settings(cls, settings: Settings) -> DynamicPositionSizer:
        return cls(position_sizing_config_from_settings(settings))

    @staticmethod
    def bucket_down(cap: int) -> int:
        if cap <= 0:
            return 0
        if cap < 3:
            return 1
        if cap < 5:
            return 3
        return 5

    @staticmethod
    def _is_unit_score(value: float) -> bool:
        return isfinite(value) and 0.0 <= value <= 1.0

    def decide(self, item: PositionSizingInput) -> PositionSizingDecision:
        if item.hard_risk_block:
            return self._fallback_decision(
                item,
                ("HARD_RISK_BLOCK",),
                hard_block=True,
            )

        validation_errors = self._validate_input(item)
        if validation_errors:
            return self._fallback_decision(
                item,
                ("INVALID_INPUT", *validation_errors, "SAFE_FALLBACK"),
            )

        assert item.confidence_score is not None
        assert item.opportunity_score is not None
        assert item.liquidity_score is not None
        assert item.current_drawdown_fraction is not None
        assert item.max_drawdown_fraction is not None
        assert item.historical_accuracy is not None
        assert item.historical_sample_size is not None

        cfg = self.config
        drawdown_utilization = item.current_drawdown_fraction / item.max_drawdown_fraction
        drawdown_health = max(0.0, 1.0 - min(drawdown_utilization, 1.0))
        adjusted_accuracy = self._adjust_history(
            item.historical_accuracy,
            item.historical_sample_size,
        )
        factor_scores = {
            "confidence": item.confidence_score,
            "opportunity": item.opportunity_score,
            "liquidity": item.liquidity_score,
            "historical_accuracy": adjusted_accuracy,
            "drawdown_health": drawdown_health,
        }
        weights = cfg.weights
        composite_score = (
            factor_scores["confidence"] * weights.confidence
            + factor_scores["opportunity"] * weights.opportunity
            + factor_scores["liquidity"] * weights.liquidity
            + factor_scores["historical_accuracy"] * weights.historical_accuracy
            + factor_scores["drawdown_health"] * weights.drawdown_health
        )
        tier = self._tier(
            composite_score=composite_score,
            confidence=item.confidence_score,
            opportunity=item.opportunity_score,
            adjusted_accuracy=adjusted_accuracy,
            history_sample_size=item.historical_sample_size,
        )
        proposed_contracts = {
            ConfidenceTier.LOW: cfg.low_contracts,
            ConfidenceTier.MEDIUM: cfg.medium_contracts,
            ConfidenceTier.HIGH: cfg.high_contracts,
        }[tier]
        caps = {
            "liquidity": self._liquidity_cap(item.liquidity_score),
            "drawdown": self._drawdown_cap(drawdown_utilization),
            "history": self._history_cap(item.historical_sample_size),
            "external_risk": self._optional_cap(
                item.external_risk_cap,
                cfg.missing_external_risk_cap_defaults_to_one,
            ),
            "margin": self._optional_cap(item.margin_cap, False),
            "portfolio": self._optional_cap(item.portfolio_cap, False),
            "rollout": self.bucket_down(cfg.live_max_contracts),
            "global": self.bucket_down(cfg.global_max_contracts),
        }
        hard_blocked = any(value == 0 for value in caps.values())
        live_candidate = (
            0 if hard_blocked else self.bucket_down(min(proposed_contracts, *caps.values()))
        )
        limiting_factors = tuple(
            name for name, value in caps.items() if value < proposed_contracts
        )
        reasons = self._reason_codes(
            tier=tier,
            sample_size=item.historical_sample_size,
            external_risk_cap=item.external_risk_cap,
            caps=caps,
            limiting_factors=limiting_factors,
            hard_blocked=hard_blocked,
        )
        if hard_blocked:
            tier = ConfidenceTier.BLOCKED
            executed_contracts = 0
        elif cfg.mode == SizingMode.LIVE:
            executed_contracts = live_candidate
        elif cfg.mode == SizingMode.SHADOW:
            executed_contracts = 1
        else:
            executed_contracts = 1
        reasons = (*reasons, self._mode_reason())

        assert proposed_contracts in self._ALLOWED_SIZES
        assert live_candidate in self._ALLOWED_SIZES
        assert executed_contracts in self._ALLOWED_SIZES

        return PositionSizingDecision(
            version=cfg.version,
            mode=cfg.mode,
            tier=tier,
            composite_score=composite_score,
            proposed_contracts=proposed_contracts,
            live_candidate_contracts=live_candidate,
            executed_contracts=executed_contracts,
            factor_scores=factor_scores,
            factor_weights=cfg.weights.as_dict(),
            adjusted_historical_accuracy=adjusted_accuracy,
            historical_sample_size=item.historical_sample_size,
            drawdown_utilization=drawdown_utilization,
            caps=caps,
            limiting_factors=limiting_factors,
            reason_codes=reasons,
            fallback_used=False,
            decision_timestamp=item.decision_timestamp,
        )

    def _validate_input(self, item: PositionSizingInput) -> tuple[str, ...]:
        errors: list[str] = []
        for name, value, missing_code in (
            ("confidence_score", item.confidence_score, "MISSING_CONFIDENCE"),
            ("opportunity_score", item.opportunity_score, "MISSING_OPPORTUNITY"),
            ("liquidity_score", item.liquidity_score, "MISSING_LIQUIDITY"),
            (
                "historical_accuracy",
                item.historical_accuracy,
                "MISSING_HISTORICAL_ACCURACY",
            ),
        ):
            if value is None:
                errors.append(missing_code)
            elif not self._is_unit_score(value):
                errors.append(f"INVALID_{name.upper()}")
        if item.current_drawdown_fraction is None or item.max_drawdown_fraction is None:
            errors.append("MISSING_DRAWDOWN")
        elif (
            not isfinite(item.current_drawdown_fraction)
            or item.current_drawdown_fraction < 0.0
            or not isfinite(item.max_drawdown_fraction)
            or item.max_drawdown_fraction <= 0.0
        ):
            errors.append("INVALID_DRAWDOWN")
        if item.historical_sample_size is None:
            errors.append("MISSING_HISTORICAL_ACCURACY")
        elif item.historical_sample_size < 0:
            errors.append("INVALID_HISTORY_SAMPLE_SIZE")
        for name, cap in (
            ("external_risk_cap", item.external_risk_cap),
            ("margin_cap", item.margin_cap),
            ("portfolio_cap", item.portfolio_cap),
        ):
            if cap is not None and cap < 0:
                errors.append(f"INVALID_{name.upper()}")
        return tuple(errors)

    def _adjust_history(self, accuracy: float, sample_size: int) -> float:
        prior_weight = self.config.history_prior_weight
        denominator = sample_size + prior_weight
        if denominator <= 0:
            return self.config.history_prior_accuracy
        return (
            accuracy * sample_size + self.config.history_prior_accuracy * prior_weight
        ) / denominator

    def _tier(
        self,
        *,
        composite_score: float,
        confidence: float,
        opportunity: float,
        adjusted_accuracy: float,
        history_sample_size: int,
    ) -> ConfidenceTier:
        cfg = self.config
        high = (
            composite_score >= cfg.high_score_threshold
            and confidence >= cfg.high_min_confidence
            and opportunity >= cfg.high_min_opportunity
            and adjusted_accuracy >= cfg.high_min_adjusted_accuracy
            and history_sample_size >= cfg.high_min_history_samples
        )
        if high:
            return ConfidenceTier.HIGH
        medium = (
            composite_score >= cfg.medium_score_threshold
            and confidence >= cfg.medium_min_confidence
            and opportunity >= cfg.medium_min_opportunity
        )
        if medium:
            return ConfidenceTier.MEDIUM
        return ConfidenceTier.LOW

    def _liquidity_cap(self, liquidity_score: float) -> int:
        cfg = self.config
        if liquidity_score < cfg.liquidity_one_contract_below:
            return 1
        if liquidity_score < cfg.liquidity_three_contracts_below:
            return 3
        return 5

    def _drawdown_cap(self, utilization: float) -> int:
        cfg = self.config
        if utilization >= cfg.drawdown_kill_at:
            return 0
        if utilization >= cfg.drawdown_one_contract_at:
            return 1
        if utilization >= cfg.drawdown_three_contracts_at:
            return 3
        return 5

    def _history_cap(self, sample_size: int) -> int:
        if sample_size < self.config.high_min_history_samples:
            return 3
        return 5

    def _optional_cap(self, cap: int | None, missing_defaults_to_one: bool) -> int:
        if cap is None:
            return 1 if missing_defaults_to_one else 5
        return self.bucket_down(cap)

    def _reason_codes(
        self,
        *,
        tier: ConfidenceTier,
        sample_size: int,
        external_risk_cap: int | None,
        caps: dict[str, int],
        limiting_factors: tuple[str, ...],
        hard_blocked: bool,
    ) -> tuple[str, ...]:
        reasons: list[str] = [f"TIER_{tier.value.upper()}"]
        if sample_size < self.config.high_min_history_samples:
            reasons.append("INSUFFICIENT_HISTORY_FOR_HIGH")
        if external_risk_cap is None:
            reasons.append("MISSING_EXTERNAL_RISK_CAP")
        cap_reason = {
            "liquidity": "LIQUIDITY_CAP_APPLIED",
            "drawdown": "DRAWDOWN_CAP_APPLIED",
            "history": "HISTORY_CAP_APPLIED",
            "external_risk": "EXTERNAL_RISK_CAP_APPLIED",
            "margin": "MARGIN_CAP_APPLIED",
            "portfolio": "PORTFOLIO_CAP_APPLIED",
            "rollout": "ROLLOUT_CAP_APPLIED",
            "global": "GLOBAL_CAP_APPLIED",
        }
        for factor in limiting_factors:
            reasons.append(cap_reason[factor])
        if caps["drawdown"] == 0:
            reasons.append("DRAWDOWN_KILL_SWITCH")
        if hard_blocked:
            reasons.append("HARD_RISK_BLOCK")
        return tuple(dict.fromkeys(reasons))

    def _fallback_decision(
        self,
        item: PositionSizingInput,
        reason_codes: tuple[str, ...],
        *,
        hard_block: bool = False,
    ) -> PositionSizingDecision:
        executed_contracts = 0 if hard_block else 1
        tier = ConfidenceTier.BLOCKED if hard_block else ConfidenceTier.LOW
        reasons = (*reason_codes, self._mode_reason())
        return PositionSizingDecision(
            version=self.config.version,
            mode=self.config.mode,
            tier=tier,
            composite_score=0.0,
            proposed_contracts=executed_contracts,
            live_candidate_contracts=executed_contracts,
            executed_contracts=executed_contracts,
            factor_scores={},
            factor_weights=self.config.weights.as_dict(),
            adjusted_historical_accuracy=self.config.history_prior_accuracy,
            historical_sample_size=max(0, item.historical_sample_size or 0),
            drawdown_utilization=0.0,
            caps={},
            limiting_factors=("HARD_RISK_BLOCK",) if hard_block else ("SAFE_FALLBACK",),
            reason_codes=tuple(dict.fromkeys(reasons)),
            fallback_used=True,
            decision_timestamp=item.decision_timestamp,
        )

    def _mode_reason(self) -> str:
        return {
            SizingMode.DISABLED: "MODE_DISABLED",
            SizingMode.SHADOW: "MODE_SHADOW",
            SizingMode.LIVE: "MODE_LIVE",
        }[self.config.mode]


def position_sizing_config_from_settings(settings: Settings) -> PositionSizingConfig:
    return PositionSizingConfig(
        mode=SizingMode(settings.dynamic_position_sizing_mode),
        version=settings.dynamic_position_sizing_version,
        live_max_contracts=settings.dynamic_position_sizing_live_max_contracts,
        global_max_contracts=settings.dynamic_position_sizing_global_max_contracts,
        medium_score_threshold=float(settings.dynamic_position_sizing_medium_score),
        high_score_threshold=float(settings.dynamic_position_sizing_high_score),
        medium_min_confidence=float(
            settings.dynamic_position_sizing_medium_min_confidence
        ),
        medium_min_opportunity=float(
            settings.dynamic_position_sizing_medium_min_opportunity
        ),
        high_min_confidence=float(settings.dynamic_position_sizing_high_min_confidence),
        high_min_opportunity=float(settings.dynamic_position_sizing_high_min_opportunity),
        high_min_adjusted_accuracy=float(
            settings.dynamic_position_sizing_high_min_adjusted_accuracy
        ),
        high_min_history_samples=settings.dynamic_position_sizing_minimum_samples_for_high,
        liquidity_one_contract_below=float(
            settings.dynamic_position_sizing_liquidity_one_contract_below
        ),
        liquidity_three_contracts_below=float(
            settings.dynamic_position_sizing_liquidity_three_contracts_below
        ),
        drawdown_one_contract_at=float(
            settings.dynamic_position_sizing_drawdown_one_contract_at
        ),
        drawdown_three_contracts_at=float(
            settings.dynamic_position_sizing_drawdown_three_contracts_at
        ),
        drawdown_kill_at=float(settings.dynamic_position_sizing_drawdown_kill_at),
        history_prior_accuracy=float(
            settings.dynamic_position_sizing_history_prior_accuracy
        ),
        history_prior_weight=settings.dynamic_position_sizing_history_prior_weight,
        missing_external_risk_cap_defaults_to_one=(
            settings.dynamic_position_sizing_missing_external_risk_cap_defaults_to_one
        ),
        weights=FactorWeights(
            confidence=float(settings.dynamic_position_sizing_weight_confidence),
            opportunity=float(settings.dynamic_position_sizing_weight_opportunity),
            liquidity=float(settings.dynamic_position_sizing_weight_liquidity),
            historical_accuracy=float(
                settings.dynamic_position_sizing_weight_historical_accuracy
            ),
            drawdown_health=float(
                settings.dynamic_position_sizing_weight_drawdown_health
            ),
        ),
    )
