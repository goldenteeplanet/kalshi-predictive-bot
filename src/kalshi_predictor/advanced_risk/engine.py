from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_FLOOR, Decimal
from enum import StrEnum
from math import sqrt
from typing import Any

from kalshi_predictor.config import Settings
from kalshi_predictor.utils.decimals import decimal_to_str

ALLOWED_CONTRACTS = (0, 1, 3, 5)
UNKNOWN = "UNKNOWN"


class AdvancedRiskMode(StrEnum):
    DISABLED = "disabled"
    SHADOW = "shadow"
    LIVE = "live"


class AdvancedRiskAction(StrEnum):
    ALLOW = "ALLOW"
    REDUCE = "REDUCE"
    BLOCK = "BLOCK"


@dataclass(frozen=True)
class AdvancedRiskConfig:
    version: str = "3N"
    mode: AdvancedRiskMode = AdvancedRiskMode.DISABLED
    live_max_contracts: int = 1
    global_max_contracts: int = 5
    portfolio_snapshot_max_age_ms: int = 300000
    quote_max_age_ms: int = 900000
    unknown_category_action: str = "block"
    unknown_model_action: str = "block"
    missing_edge_statistics_action: str = "cap_to_one"
    missing_optional_liquidity_data_action: str = "cap_to_one"
    max_total_open_risk_fraction: Decimal = Decimal("0.20")
    default_category_risk_fraction: Decimal = Decimal("0.10")
    default_model_risk_fraction: Decimal = Decimal("0.10")
    max_daily_loss_fraction: Decimal | None = Decimal("0.05")
    max_daily_loss_fixed_amount: Decimal | None = None
    unrealized_pnl_weight: Decimal = Decimal("1.0")
    daily_loss_reserve_amount: Decimal = Decimal("0")
    session_timezone: str = "UTC"
    session_reset_time: str = "00:00"
    max_drawdown_fraction: Decimal = Decimal("0.20")
    drawdown_reserve_amount: Decimal = Decimal("0")
    drawdown_warning_one_utilization: Decimal = Decimal("0.50")
    drawdown_warning_two_utilization: Decimal = Decimal("0.75")
    drawdown_warning_one_cap: int = 3
    drawdown_warning_two_cap: int = 1
    drawdown_kill_utilization: Decimal = Decimal("1.00")
    max_instrument_risk_fraction: Decimal = Decimal("0.05")
    max_correlation_group_risk_fraction: Decimal | None = None
    spread_preferred_max_ticks: Decimal = Decimal("2")
    spread_elevated_max_ticks: Decimal = Decimal("5")
    spread_executable_max_ticks: Decimal = Decimal("10")
    max_depth_participation_fraction: Decimal = Decimal("0.10")
    max_recent_volume_participation_fraction: Decimal = Decimal("0.05")
    max_adv_participation_fraction: Decimal | None = None
    max_open_interest_participation_fraction: Decimal | None = None
    required_liquidity_sources: tuple[str, ...] = ("depth", "recent_volume")
    confidence_defensive_medium_floor: Decimal = Decimal("0.65")
    confidence_defensive_high_floor: Decimal = Decimal("0.80")
    kelly_enabled: bool = False
    kelly_prior_win_probability: Decimal = Decimal("0.50")
    kelly_prior_weight: int = 20
    kelly_minimum_sample_size: int = 30
    fractional_kelly_multiplier: Decimal = Decimal("0.25")
    max_applied_kelly_fraction: Decimal = Decimal("0.02")
    max_trade_risk_fraction: Decimal = Decimal("0.01")
    kelly_insufficient_data_cap: int = 1
    ev_enabled: bool = False
    ev_minimum_sample_size: int = 30
    ev_probability_z_score: Decimal = Decimal("1.0")
    cvar_weight: Decimal = Decimal("1.0")
    minimum_trade_ev_to_risk: Decimal = Decimal("0.00")
    medium_ev_to_risk: Decimal = Decimal("0.10")
    high_ev_to_risk: Decimal = Decimal("0.25")
    ev_insufficient_data_cap: int = 1

    @classmethod
    def from_settings(cls, settings: Settings) -> AdvancedRiskConfig:
        sources = tuple(
            item.strip().lower()
            for item in settings.advanced_risk_required_liquidity_sources.split(",")
            if item.strip()
        )
        return cls(
            version=settings.advanced_risk_engine_version,
            mode=AdvancedRiskMode(settings.advanced_risk_engine_mode),
            live_max_contracts=settings.advanced_risk_live_max_contracts,
            global_max_contracts=settings.advanced_risk_global_max_contracts,
            portfolio_snapshot_max_age_ms=settings.advanced_risk_portfolio_snapshot_max_age_ms,
            quote_max_age_ms=settings.advanced_risk_quote_max_age_ms,
            unknown_category_action=settings.advanced_risk_unknown_category_action,
            unknown_model_action=settings.advanced_risk_unknown_model_action,
            missing_edge_statistics_action=settings.advanced_risk_missing_edge_statistics_action,
            missing_optional_liquidity_data_action=(
                settings.advanced_risk_missing_optional_liquidity_data_action
            ),
            max_total_open_risk_fraction=settings.advanced_risk_max_total_open_risk_fraction,
            default_category_risk_fraction=settings.advanced_risk_default_category_risk_fraction,
            default_model_risk_fraction=settings.advanced_risk_default_model_risk_fraction,
            max_daily_loss_fraction=settings.advanced_risk_max_daily_loss_fraction,
            max_daily_loss_fixed_amount=settings.advanced_risk_max_daily_loss_fixed_amount,
            unrealized_pnl_weight=settings.advanced_risk_unrealized_pnl_weight,
            daily_loss_reserve_amount=settings.advanced_risk_daily_loss_reserve_amount,
            session_timezone=settings.advanced_risk_session_timezone,
            session_reset_time=settings.advanced_risk_session_reset_time,
            max_drawdown_fraction=settings.advanced_risk_max_drawdown_fraction,
            drawdown_reserve_amount=settings.advanced_risk_drawdown_reserve_amount,
            drawdown_warning_one_utilization=(
                settings.advanced_risk_drawdown_warning_one_utilization
            ),
            drawdown_warning_two_utilization=(
                settings.advanced_risk_drawdown_warning_two_utilization
            ),
            drawdown_warning_one_cap=settings.advanced_risk_drawdown_warning_one_cap,
            drawdown_warning_two_cap=settings.advanced_risk_drawdown_warning_two_cap,
            drawdown_kill_utilization=settings.advanced_risk_drawdown_kill_utilization,
            max_instrument_risk_fraction=settings.advanced_risk_max_instrument_risk_fraction,
            max_correlation_group_risk_fraction=(
                settings.advanced_risk_max_correlation_group_risk_fraction
            ),
            spread_preferred_max_ticks=settings.advanced_risk_spread_preferred_max_ticks,
            spread_elevated_max_ticks=settings.advanced_risk_spread_elevated_max_ticks,
            spread_executable_max_ticks=settings.advanced_risk_spread_executable_max_ticks,
            max_depth_participation_fraction=(
                settings.advanced_risk_max_depth_participation_fraction
            ),
            max_recent_volume_participation_fraction=(
                settings.advanced_risk_max_recent_volume_participation_fraction
            ),
            max_adv_participation_fraction=settings.advanced_risk_max_adv_participation_fraction,
            max_open_interest_participation_fraction=(
                settings.advanced_risk_max_open_interest_participation_fraction
            ),
            required_liquidity_sources=sources or ("depth", "recent_volume"),
            confidence_defensive_medium_floor=(
                settings.advanced_risk_confidence_defensive_medium_floor
            ),
            confidence_defensive_high_floor=settings.advanced_risk_confidence_defensive_high_floor,
            kelly_enabled=settings.advanced_risk_kelly_enabled,
            kelly_prior_win_probability=settings.advanced_risk_kelly_prior_win_probability,
            kelly_prior_weight=settings.advanced_risk_kelly_prior_weight,
            kelly_minimum_sample_size=settings.advanced_risk_kelly_minimum_sample_size,
            fractional_kelly_multiplier=settings.advanced_risk_fractional_kelly_multiplier,
            max_applied_kelly_fraction=settings.advanced_risk_max_applied_kelly_fraction,
            max_trade_risk_fraction=settings.advanced_risk_max_trade_risk_fraction,
            kelly_insufficient_data_cap=settings.advanced_risk_kelly_insufficient_data_cap,
            ev_enabled=settings.advanced_risk_ev_enabled,
            ev_minimum_sample_size=settings.advanced_risk_ev_minimum_sample_size,
            ev_probability_z_score=settings.advanced_risk_ev_probability_z_score,
            cvar_weight=settings.advanced_risk_cvar_weight,
            minimum_trade_ev_to_risk=settings.advanced_risk_minimum_trade_ev_to_risk,
            medium_ev_to_risk=settings.advanced_risk_medium_ev_to_risk,
            high_ev_to_risk=settings.advanced_risk_high_ev_to_risk,
            ev_insufficient_data_cap=settings.advanced_risk_ev_insufficient_data_cap,
        )


@dataclass(frozen=True)
class PortfolioRiskSnapshot:
    snapshot_id: str
    snapshot_version: str
    captured_at: datetime
    account_equity: Decimal
    start_of_session_equity: Decimal
    high_water_equity: Decimal
    realized_pnl_session: Decimal
    unrealized_pnl_session: Decimal
    current_total_open_risk: Decimal
    current_pending_reserved_risk: Decimal
    category_open_risk: dict[str, Decimal] = field(default_factory=dict)
    category_pending_reserved_risk: dict[str, Decimal] = field(default_factory=dict)
    model_open_risk: dict[str, Decimal] = field(default_factory=dict)
    model_pending_reserved_risk: dict[str, Decimal] = field(default_factory=dict)
    instrument_open_risk: dict[str, Decimal] = field(default_factory=dict)
    instrument_pending_reserved_risk: dict[str, Decimal] = field(default_factory=dict)
    correlation_group_open_risk: dict[str, Decimal] = field(default_factory=dict)
    correlation_group_pending_reserved_risk: dict[str, Decimal] = field(default_factory=dict)
    current_gross_notional: Decimal | None = None
    current_net_notional: Decimal | None = None
    existing_position_contracts: int = 0
    existing_pending_entry_contracts: int = 0


@dataclass(frozen=True)
class MarketRiskSnapshot:
    captured_at: datetime
    bid_price: Decimal | None
    ask_price: Decimal | None
    last_price: Decimal | None = None
    quote_age_ms: int | None = None
    executable_depth_contracts: Decimal | None = None
    depth_price_band_ticks: Decimal | None = None
    recent_volume_contracts: Decimal | None = None
    recent_volume_window_seconds: int | None = None
    average_daily_volume_contracts: Decimal | None = None
    open_interest_contracts: Decimal | None = None
    expected_market_impact_per_contract: Decimal | None = None
    expected_slippage_per_contract: Decimal | None = None
    market_status: str = "UNKNOWN"
    data_quality_status: str = "INVALID"


@dataclass(frozen=True)
class TradeEdgeStatistics:
    bucket_key: str
    bucket_level: str
    sample_size: int
    raw_win_probability: Decimal | None
    average_gross_win_per_contract: Decimal | None
    average_gross_loss_per_contract: Decimal | None
    win_loss_variance: Decimal | None = None
    cvar_loss_per_contract: Decimal | None = None
    maximum_observed_loss_per_contract: Decimal | None = None
    statistics_as_of: datetime | None = None
    outcome_basis: str = "GROSS"


@dataclass(frozen=True)
class AdvancedRiskRequest:
    version: str
    decision_timestamp: datetime
    trade_intent_id: str
    order_correlation_id: str | None
    strategy_id: str
    model_id: str
    category_id: str
    instrument_id: str
    correlation_group_id: str | None
    direction: str
    phase_3m_tier: str
    phase_3m_proposed_contracts: int
    confidence_score: Decimal | None
    entry_price: Decimal | None
    stop_price: Decimal | None
    point_value: Decimal | None
    tick_size: Decimal | None
    estimated_round_trip_fees: Decimal
    estimated_slippage_per_contract: Decimal
    gap_or_tail_buffer_per_contract: Decimal
    portfolio_snapshot: PortfolioRiskSnapshot
    market_snapshot: MarketRiskSnapshot
    edge_statistics: TradeEdgeStatistics | None
    external_hard_risk_block: bool = False
    external_margin_cap: int | None = None
    external_buying_power_cap: int | None = None


@dataclass(frozen=True)
class AdvancedRiskDecision:
    version: str
    mode: AdvancedRiskMode
    action: AdvancedRiskAction
    phase_3m_tier: str
    phase_3m_proposed_contracts: int
    live_candidate_contracts: int
    executed_contracts: int
    risk_per_contract: Decimal
    planned_trade_risk: Decimal
    portfolio_snapshot_id: str
    portfolio_snapshot_version: str
    market_snapshot_timestamp: datetime
    edge_statistics_as_of: datetime | None
    raw_caps: dict[str, int]
    bucketed_caps: dict[str, int]
    limiting_factors: tuple[str, ...]
    hard_blocks: tuple[str, ...]
    category_exposure_before: Decimal
    category_exposure_after: Decimal
    model_exposure_before: Decimal
    model_exposure_after: Decimal
    daily_loss_before: Decimal
    projected_daily_loss_after: Decimal
    drawdown_before: Decimal
    projected_drawdown_after: Decimal
    spread_price: Decimal | None
    spread_ticks: Decimal | None
    spread_bps: Decimal | None
    liquidity_evidence: dict[str, Any]
    position_concentration_before: Decimal
    position_concentration_after: Decimal
    adjusted_win_probability: Decimal | None
    payoff_ratio: Decimal | None
    full_kelly_fraction: Decimal | None
    applied_kelly_fraction: Decimal | None
    kelly_risk_budget: Decimal | None
    risk_adjusted_ev_per_contract: Decimal | None
    risk_adjusted_ev_to_risk: Decimal | None
    reservation_id: int | None
    reason_codes: tuple[str, ...]
    fallback_used: bool
    decision_timestamp: datetime
    risk_components: dict[str, str]
    telemetry: dict[str, Any]

    def with_reservation(self, reservation_id: int | None) -> AdvancedRiskDecision:
        return AdvancedRiskDecision(
            **{**self.__dict__, "reservation_id": reservation_id}
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "mode": self.mode.value,
            "action": self.action.value,
            "phase_3m_tier": self.phase_3m_tier,
            "phase_3m_proposed_contracts": self.phase_3m_proposed_contracts,
            "live_candidate_contracts": self.live_candidate_contracts,
            "executed_contracts": self.executed_contracts,
            "risk_per_contract": _decimal_str(self.risk_per_contract),
            "planned_trade_risk": _decimal_str(self.planned_trade_risk),
            "portfolio_snapshot_id": self.portfolio_snapshot_id,
            "portfolio_snapshot_version": self.portfolio_snapshot_version,
            "market_snapshot_timestamp": self.market_snapshot_timestamp.isoformat(),
            "edge_statistics_as_of": (
                self.edge_statistics_as_of.isoformat() if self.edge_statistics_as_of else None
            ),
            "raw_caps": self.raw_caps,
            "bucketed_caps": self.bucketed_caps,
            "limiting_factors": list(self.limiting_factors),
            "hard_blocks": list(self.hard_blocks),
            "category_exposure_before": _decimal_str(self.category_exposure_before),
            "category_exposure_after": _decimal_str(self.category_exposure_after),
            "model_exposure_before": _decimal_str(self.model_exposure_before),
            "model_exposure_after": _decimal_str(self.model_exposure_after),
            "daily_loss_before": _decimal_str(self.daily_loss_before),
            "projected_daily_loss_after": _decimal_str(self.projected_daily_loss_after),
            "drawdown_before": _decimal_str(self.drawdown_before),
            "projected_drawdown_after": _decimal_str(self.projected_drawdown_after),
            "spread_price": _optional_decimal_str(self.spread_price),
            "spread_ticks": _optional_decimal_str(self.spread_ticks),
            "spread_bps": _optional_decimal_str(self.spread_bps),
            "liquidity_evidence": self.liquidity_evidence,
            "position_concentration_before": _decimal_str(self.position_concentration_before),
            "position_concentration_after": _decimal_str(self.position_concentration_after),
            "adjusted_win_probability": _optional_decimal_str(self.adjusted_win_probability),
            "payoff_ratio": _optional_decimal_str(self.payoff_ratio),
            "full_kelly_fraction": _optional_decimal_str(self.full_kelly_fraction),
            "applied_kelly_fraction": _optional_decimal_str(self.applied_kelly_fraction),
            "kelly_risk_budget": _optional_decimal_str(self.kelly_risk_budget),
            "risk_adjusted_ev_per_contract": (
                _optional_decimal_str(self.risk_adjusted_ev_per_contract)
            ),
            "risk_adjusted_ev_to_risk": _optional_decimal_str(self.risk_adjusted_ev_to_risk),
            "reservation_id": self.reservation_id,
            "reason_codes": list(self.reason_codes),
            "fallback_used": self.fallback_used,
            "decision_timestamp": self.decision_timestamp.isoformat(),
            "risk_components": self.risk_components,
            "telemetry": self.telemetry,
        }


class AdvancedRiskEngine:
    def __init__(self, config: AdvancedRiskConfig) -> None:
        self.config = config

    @staticmethod
    def bucket_down(raw_cap: int | Decimal | None) -> int:
        if raw_cap is None:
            return 0
        cap = int(raw_cap)
        if cap <= 0:
            return 0
        if cap < 3:
            return 1
        if cap < 5:
            return 3
        return 5

    def decide(self, request: AdvancedRiskRequest) -> AdvancedRiskDecision:
        self._last_trade_metrics = {}
        reasons: list[str] = []
        hard_blocks: list[str] = []
        raw_caps: dict[str, int] = {}
        bucketed_caps: dict[str, int] = {}
        risk_per_contract, risk_components, risk_errors = self._risk_per_contract(request)
        if request.external_hard_risk_block:
            hard_blocks.append("EXTERNAL_HARD_RISK_BLOCK")
            reasons.append("EXTERNAL_HARD_RISK_BLOCK")
        for reason in self._validate_request(request):
            hard_blocks.append(reason)
            reasons.append(reason)
        for reason in risk_errors:
            hard_blocks.append(reason)
            reasons.append(reason)

        spread = self._spread_metrics(request.market_snapshot, request.tick_size)
        if not hard_blocks:
            raw_caps, cap_reasons, cap_hard_blocks = self._raw_caps(
                request,
                risk_per_contract=risk_per_contract,
                spread_metrics=spread,
            )
            reasons.extend(cap_reasons)
            hard_blocks.extend(cap_hard_blocks)

        raw_caps.setdefault("phase_3m", max(0, request.phase_3m_proposed_contracts))
        raw_caps.setdefault("rollout", self.config.live_max_contracts)
        raw_caps.setdefault("global", self.config.global_max_contracts)
        for name, raw_cap in raw_caps.items():
            bucketed_caps[name] = self.bucket_down(raw_cap)

        if hard_blocks:
            live_candidate = 0
        else:
            live_candidate = self.bucket_down(min(bucketed_caps.values()))

        live_candidate = min(live_candidate, request.phase_3m_proposed_contracts)
        live_candidate = self.bucket_down(live_candidate)
        action = self._action(live_candidate, request.phase_3m_proposed_contracts)
        executed = self._executed_contracts(
            mode=self.config.mode,
            phase_3m_contracts=request.phase_3m_proposed_contracts,
            live_candidate=live_candidate,
            hard_blocks=tuple(hard_blocks),
        )
        planned_trade_risk = risk_per_contract * Decimal(executed)
        limiting = tuple(
            name
            for name, cap in bucketed_caps.items()
            if cap < request.phase_3m_proposed_contracts
        )
        reasons.extend(self._limiting_reasons(limiting))
        reasons.append(
            {
                AdvancedRiskMode.DISABLED: "MODE_DISABLED",
                AdvancedRiskMode.SHADOW: "MODE_SHADOW",
                AdvancedRiskMode.LIVE: "MODE_LIVE",
            }[self.config.mode]
        )
        reasons.append(
            {
                AdvancedRiskAction.ALLOW: "ALLOW_UNCHANGED",
                AdvancedRiskAction.REDUCE: "SIZE_REDUCED",
                AdvancedRiskAction.BLOCK: "TRADE_BLOCKED",
            }[action]
        )

        category_before = self._bucket_committed_risk(
            request.portfolio_snapshot.category_open_risk,
            request.portfolio_snapshot.category_pending_reserved_risk,
            request.category_id,
        )
        model_before = self._bucket_committed_risk(
            request.portfolio_snapshot.model_open_risk,
            request.portfolio_snapshot.model_pending_reserved_risk,
            request.model_id,
        )
        instrument_before = self._bucket_committed_risk(
            request.portfolio_snapshot.instrument_open_risk,
            request.portfolio_snapshot.instrument_pending_reserved_risk,
            request.instrument_id,
        )
        daily_loss_before = self._observed_session_loss(request.portfolio_snapshot)
        drawdown_before = self._current_drawdown(request.portfolio_snapshot)
        telemetry = self._telemetry(
            request,
            action=action,
            live_candidate=live_candidate,
            executed=executed,
            risk_per_contract=risk_per_contract,
            raw_caps=raw_caps,
            bucketed_caps=bucketed_caps,
            limiting_factors=limiting,
            hard_blocks=tuple(dict.fromkeys(hard_blocks)),
            reasons=tuple(dict.fromkeys(reasons)),
        )
        return AdvancedRiskDecision(
            version=self.config.version,
            mode=self.config.mode,
            action=action,
            phase_3m_tier=request.phase_3m_tier,
            phase_3m_proposed_contracts=request.phase_3m_proposed_contracts,
            live_candidate_contracts=live_candidate,
            executed_contracts=executed,
            risk_per_contract=risk_per_contract,
            planned_trade_risk=planned_trade_risk,
            portfolio_snapshot_id=request.portfolio_snapshot.snapshot_id,
            portfolio_snapshot_version=request.portfolio_snapshot.snapshot_version,
            market_snapshot_timestamp=request.market_snapshot.captured_at,
            edge_statistics_as_of=(
                request.edge_statistics.statistics_as_of if request.edge_statistics else None
            ),
            raw_caps=raw_caps,
            bucketed_caps=bucketed_caps,
            limiting_factors=limiting,
            hard_blocks=tuple(dict.fromkeys(hard_blocks)),
            category_exposure_before=category_before,
            category_exposure_after=category_before + planned_trade_risk,
            model_exposure_before=model_before,
            model_exposure_after=model_before + planned_trade_risk,
            daily_loss_before=daily_loss_before,
            projected_daily_loss_after=daily_loss_before + planned_trade_risk,
            drawdown_before=drawdown_before,
            projected_drawdown_after=drawdown_before + planned_trade_risk,
            spread_price=spread["spread_price"],
            spread_ticks=spread["spread_ticks"],
            spread_bps=spread["spread_bps"],
            liquidity_evidence=self._liquidity_evidence(request.market_snapshot),
            position_concentration_before=instrument_before,
            position_concentration_after=instrument_before + planned_trade_risk,
            adjusted_win_probability=telemetry.get("adjusted_win_probability"),
            payoff_ratio=telemetry.get("payoff_ratio"),
            full_kelly_fraction=telemetry.get("full_kelly_fraction"),
            applied_kelly_fraction=telemetry.get("applied_kelly_fraction"),
            kelly_risk_budget=telemetry.get("kelly_risk_budget"),
            risk_adjusted_ev_per_contract=telemetry.get("risk_adjusted_ev_per_contract"),
            risk_adjusted_ev_to_risk=telemetry.get("risk_adjusted_ev_to_risk"),
            reservation_id=None,
            reason_codes=tuple(dict.fromkeys(reasons)),
            fallback_used=bool(hard_blocks or "SAFE_FALLBACK" in reasons),
            decision_timestamp=request.decision_timestamp,
            risk_components=risk_components,
            telemetry={key: _json_value(value) for key, value in telemetry.items()},
        )

    def _validate_request(self, request: AdvancedRiskRequest) -> tuple[str, ...]:
        reasons: list[str] = []
        timestamp = request.decision_timestamp
        if timestamp.tzinfo is None:
            reasons.append("INVALID_INPUT")
        expected = {"LOW": 1, "MEDIUM": 3, "HIGH": 5}
        if request.phase_3m_tier.upper() not in expected:
            reasons.append("PHASE_3M_INPUT_INCONSISTENT")
        elif expected[request.phase_3m_tier.upper()] != request.phase_3m_proposed_contracts:
            reasons.append("PHASE_3M_INPUT_INCONSISTENT")
        if request.phase_3m_proposed_contracts not in {1, 3, 5}:
            reasons.append("PHASE_3M_INPUT_INCONSISTENT")
        if request.confidence_score is None or not _is_unit_decimal(request.confidence_score):
            reasons.append("INVALID_INPUT")
        if not request.trade_intent_id or not request.instrument_id:
            reasons.append("INVALID_INPUT")
        if not _positive(request.portfolio_snapshot.account_equity):
            reasons.append("INVALID_INPUT")
        if not _positive(request.portfolio_snapshot.start_of_session_equity):
            reasons.append("INVALID_INPUT")
        if not _positive(request.portfolio_snapshot.high_water_equity):
            reasons.append("HIGH_WATER_MARK_INVALID")
        portfolio_age = _age_ms(request.decision_timestamp, request.portfolio_snapshot.captured_at)
        if portfolio_age is None or portfolio_age > self.config.portfolio_snapshot_max_age_ms:
            reasons.append("STALE_PORTFOLIO_SNAPSHOT")
        return tuple(dict.fromkeys(reasons))

    def _risk_per_contract(
        self,
        request: AdvancedRiskRequest,
    ) -> tuple[Decimal, dict[str, str], tuple[str, ...]]:
        errors: list[str] = []
        entry = request.entry_price
        stop = request.stop_price
        point = request.point_value
        if entry is None or stop is None or point is None or request.tick_size is None:
            errors.append("RISK_PER_CONTRACT_INVALID")
            return Decimal("0"), {}, tuple(errors)
        invalid_price_inputs = (
            not _positive(entry)
            or stop < 0
            or not _positive(point)
            or not _positive(request.tick_size)
        )
        if invalid_price_inputs:
            errors.append("RISK_PER_CONTRACT_INVALID")
            return Decimal("0"), {}, tuple(errors)
        if any(
            not _non_negative(value)
            for value in (
                request.estimated_round_trip_fees,
                request.estimated_slippage_per_contract,
                request.gap_or_tail_buffer_per_contract,
            )
        ):
            errors.append("EXECUTION_COST_INVALID")
            return Decimal("0"), {}, tuple(errors)
        market_impact = request.market_snapshot.expected_market_impact_per_contract
        if market_impact is None:
            market_impact = Decimal("0")
        if not _non_negative(market_impact):
            errors.append("EXECUTION_COST_INVALID")
            return Decimal("0"), {}, tuple(errors)
        price_risk = abs(entry - stop) * point
        execution_cost = (
            request.estimated_round_trip_fees
            + request.estimated_slippage_per_contract
            + market_impact
        )
        risk = price_risk + execution_cost + request.gap_or_tail_buffer_per_contract
        if not _positive(risk):
            errors.append("RISK_PER_CONTRACT_INVALID")
        return (
            risk,
            {
                "price_risk_per_contract": _decimal_str(price_risk),
                "execution_cost_per_contract": _decimal_str(execution_cost),
                "estimated_round_trip_fees": _decimal_str(request.estimated_round_trip_fees),
                "estimated_slippage_per_contract": _decimal_str(
                    request.estimated_slippage_per_contract
                ),
                "expected_market_impact_per_contract": _decimal_str(market_impact),
                "gap_or_tail_buffer_per_contract": _decimal_str(
                    request.gap_or_tail_buffer_per_contract
                ),
            },
            tuple(errors),
        )

    def _raw_caps(
        self,
        request: AdvancedRiskRequest,
        *,
        risk_per_contract: Decimal,
        spread_metrics: dict[str, Decimal | None],
    ) -> tuple[dict[str, int], list[str], list[str]]:
        reasons: list[str] = []
        hard_blocks: list[str] = []
        caps: dict[str, int] = {
            "phase_3m": request.phase_3m_proposed_contracts,
            "rollout": self.config.live_max_contracts,
            "global": self.config.global_max_contracts,
        }
        portfolio_caps, portfolio_reasons, portfolio_blocks = self._portfolio_caps(
            request,
            risk_per_contract=risk_per_contract,
        )
        market_caps, market_reasons, market_blocks = self._market_caps(
            request,
            risk_per_contract=risk_per_contract,
            spread_metrics=spread_metrics,
        )
        trade_caps, trade_reasons, trade_blocks, trade_metrics = self._trade_caps(
            request,
            risk_per_contract=risk_per_contract,
        )
        caps.update(portfolio_caps)
        caps.update(market_caps)
        caps.update(trade_caps)
        reasons.extend(portfolio_reasons + market_reasons + trade_reasons)
        hard_blocks.extend(portfolio_blocks + market_blocks + trade_blocks)
        self._last_trade_metrics = trade_metrics
        if request.external_margin_cap is not None:
            caps["margin"] = max(0, request.external_margin_cap)
            if request.external_margin_cap < request.phase_3m_proposed_contracts:
                reasons.append("MARGIN_CAP_APPLIED")
        if request.external_buying_power_cap is not None:
            caps["buying_power"] = max(0, request.external_buying_power_cap)
            if request.external_buying_power_cap < request.phase_3m_proposed_contracts:
                reasons.append("BUYING_POWER_CAP_APPLIED")
        if self.config.live_max_contracts < request.phase_3m_proposed_contracts:
            reasons.append("ROLLOUT_CAP_APPLIED")
        if self.config.global_max_contracts < request.phase_3m_proposed_contracts:
            reasons.append("GLOBAL_CAP_APPLIED")
        return caps, reasons, hard_blocks

    def _portfolio_caps(
        self,
        request: AdvancedRiskRequest,
        *,
        risk_per_contract: Decimal,
    ) -> tuple[dict[str, int], list[str], list[str]]:
        snap = request.portfolio_snapshot
        reasons: list[str] = []
        hard_blocks: list[str] = []
        caps: dict[str, int] = {}
        total_limit = snap.account_equity * self.config.max_total_open_risk_fraction
        total_remaining = (
            total_limit - snap.current_total_open_risk - snap.current_pending_reserved_risk
        )
        caps["total_open_risk"] = _raw_cap(total_remaining, risk_per_contract)
        if caps["total_open_risk"] < request.phase_3m_proposed_contracts:
            reasons.append("TOTAL_OPEN_RISK_CAP_APPLIED")

        if _is_unknown(request.category_id):
            reasons.append("UNKNOWN_CATEGORY")
            caps["category"] = 0 if self.config.unknown_category_action == "block" else 1
        else:
            category_before = self._bucket_committed_risk(
                snap.category_open_risk,
                snap.category_pending_reserved_risk,
                request.category_id,
            )
            category_limit = snap.account_equity * self.config.default_category_risk_fraction
            caps["category"] = _raw_cap(category_limit - category_before, risk_per_contract)
        if caps["category"] <= 0:
            hard_blocks.append("CATEGORY_LIMIT_REACHED")
        elif caps["category"] < request.phase_3m_proposed_contracts:
            reasons.append("CATEGORY_CAP_APPLIED")

        if _is_unknown(request.model_id):
            reasons.append("UNKNOWN_MODEL")
            caps["model"] = 0 if self.config.unknown_model_action == "block" else 1
        else:
            model_before = self._bucket_committed_risk(
                snap.model_open_risk,
                snap.model_pending_reserved_risk,
                request.model_id,
            )
            model_limit = snap.account_equity * self.config.default_model_risk_fraction
            caps["model"] = _raw_cap(model_limit - model_before, risk_per_contract)
        if caps["model"] <= 0:
            hard_blocks.append("MODEL_LIMIT_REACHED")
        elif caps["model"] < request.phase_3m_proposed_contracts:
            reasons.append("MODEL_CAP_APPLIED")

        instrument_before = self._bucket_committed_risk(
            snap.instrument_open_risk,
            snap.instrument_pending_reserved_risk,
            request.instrument_id,
        )
        instrument_limit = snap.account_equity * self.config.max_instrument_risk_fraction
        caps["concentration"] = _raw_cap(instrument_limit - instrument_before, risk_per_contract)
        if caps["concentration"] <= 0:
            hard_blocks.append("POSITION_CONCENTRATION_LIMIT_REACHED")
        elif caps["concentration"] < request.phase_3m_proposed_contracts:
            reasons.append("POSITION_CONCENTRATION_CAP_APPLIED")

        if request.correlation_group_id and self.config.max_correlation_group_risk_fraction:
            group_before = self._bucket_committed_risk(
                snap.correlation_group_open_risk,
                snap.correlation_group_pending_reserved_risk,
                request.correlation_group_id,
            )
            group_limit = snap.account_equity * self.config.max_correlation_group_risk_fraction
            caps["correlation_group"] = _raw_cap(group_limit - group_before, risk_per_contract)
            if caps["correlation_group"] < request.phase_3m_proposed_contracts:
                reasons.append("CORRELATION_GROUP_CAP_APPLIED")

        daily_cap, daily_reasons, daily_blocks = self._daily_loss_cap(request, risk_per_contract)
        drawdown_cap, drawdown_reasons, drawdown_blocks = self._drawdown_cap(
            request,
            risk_per_contract,
        )
        caps["daily_loss"] = daily_cap
        caps["drawdown"] = drawdown_cap
        reasons.extend(daily_reasons + drawdown_reasons)
        hard_blocks.extend(daily_blocks + drawdown_blocks)
        return caps, reasons, hard_blocks

    def _daily_loss_cap(
        self,
        request: AdvancedRiskRequest,
        risk_per_contract: Decimal,
    ) -> tuple[int, list[str], list[str]]:
        snap = request.portfolio_snapshot
        reasons: list[str] = []
        hard_blocks: list[str] = []
        if self.config.max_daily_loss_fixed_amount is not None:
            max_loss = self.config.max_daily_loss_fixed_amount
        elif self.config.max_daily_loss_fraction is not None:
            max_loss = snap.start_of_session_equity * self.config.max_daily_loss_fraction
        else:
            return 0, ["DAILY_LOSS_DATA_MISSING"], ["DAILY_LOSS_DATA_MISSING"]
        observed = self._observed_session_loss(snap)
        committed = snap.current_total_open_risk + snap.current_pending_reserved_risk
        remaining = max_loss - observed - committed - self.config.daily_loss_reserve_amount
        cap = _raw_cap(remaining, risk_per_contract)
        if observed >= max_loss or remaining < risk_per_contract:
            hard_blocks.append("DAILY_LOSS_LIMIT_REACHED")
        elif cap < request.phase_3m_proposed_contracts:
            reasons.append("DAILY_LOSS_CAP_APPLIED")
        return cap, reasons, hard_blocks

    def _drawdown_cap(
        self,
        request: AdvancedRiskRequest,
        risk_per_contract: Decimal,
    ) -> tuple[int, list[str], list[str]]:
        snap = request.portfolio_snapshot
        reasons: list[str] = []
        hard_blocks: list[str] = []
        if not _positive(snap.high_water_equity):
            return 0, ["HIGH_WATER_MARK_INVALID"], ["HIGH_WATER_MARK_INVALID"]
        current = self._current_drawdown(snap)
        current_fraction = current / snap.high_water_equity
        max_drawdown = snap.high_water_equity * self.config.max_drawdown_fraction
        remaining = (
            max_drawdown
            - current
            - snap.current_total_open_risk
            - snap.current_pending_reserved_risk
            - self.config.drawdown_reserve_amount
        )
        cap = _raw_cap(remaining, risk_per_contract)
        utilization = (
            current_fraction / self.config.max_drawdown_fraction
            if self.config.max_drawdown_fraction > 0
            else Decimal("999")
        )
        if (
            current_fraction >= self.config.max_drawdown_fraction
            or utilization >= self.config.drawdown_kill_utilization
            or remaining < risk_per_contract
        ):
            hard_blocks.append("DRAWDOWN_LIMIT_REACHED")
        elif utilization >= self.config.drawdown_warning_two_utilization:
            cap = min(cap, self.config.drawdown_warning_two_cap)
            reasons.append("DRAWDOWN_CAP_APPLIED")
        elif utilization >= self.config.drawdown_warning_one_utilization:
            cap = min(cap, self.config.drawdown_warning_one_cap)
            reasons.append("DRAWDOWN_CAP_APPLIED")
        elif cap < request.phase_3m_proposed_contracts:
            reasons.append("DRAWDOWN_CAP_APPLIED")
        return cap, reasons, hard_blocks

    def _market_caps(
        self,
        request: AdvancedRiskRequest,
        *,
        risk_per_contract: Decimal,
        spread_metrics: dict[str, Decimal | None],
    ) -> tuple[dict[str, int], list[str], list[str]]:
        _ = risk_per_contract
        snap = request.market_snapshot
        reasons: list[str] = []
        hard_blocks: list[str] = []
        caps: dict[str, int] = {}
        quote_reasons = self._quote_hard_blocks(request, spread_metrics)
        hard_blocks.extend(quote_reasons)
        reasons.extend(quote_reasons)
        spread_ticks = spread_metrics["spread_ticks"]
        if spread_ticks is None:
            caps["spread"] = 0
            hard_blocks.append("SPREAD_LIMIT_EXCEEDED")
        elif spread_ticks <= self.config.spread_preferred_max_ticks:
            caps["spread"] = 5
        elif spread_ticks <= self.config.spread_elevated_max_ticks:
            caps["spread"] = 3
            reasons.append("SPREAD_CAP_APPLIED")
        elif spread_ticks <= self.config.spread_executable_max_ticks:
            caps["spread"] = 1
            reasons.append("SPREAD_CAP_APPLIED")
        else:
            caps["spread"] = 0
            hard_blocks.append("SPREAD_LIMIT_EXCEEDED")
        caps["liquidity"] = self._liquidity_cap(snap, reasons, hard_blocks)
        if caps["liquidity"] <= 0:
            hard_blocks.append("INSUFFICIENT_LIQUIDITY")
        elif caps["liquidity"] < request.phase_3m_proposed_contracts:
            reasons.append("LIQUIDITY_CAP_APPLIED")
        return caps, reasons, hard_blocks

    def _quote_hard_blocks(
        self,
        request: AdvancedRiskRequest,
        spread_metrics: dict[str, Decimal | None],
    ) -> list[str]:
        snap = request.market_snapshot
        reasons: list[str] = []
        if snap.data_quality_status.upper() == "INVALID":
            reasons.append("INVALID_BID_ASK")
        if snap.bid_price is None or snap.ask_price is None:
            reasons.append("INVALID_BID_ASK")
        elif not _positive(snap.bid_price) or not _positive(snap.ask_price):
            reasons.append("INVALID_BID_ASK")
        elif snap.ask_price < snap.bid_price:
            reasons.append("INVALID_BID_ASK")
        if snap.quote_age_ms is None or snap.quote_age_ms > self.config.quote_max_age_ms:
            reasons.append("QUOTE_STALE")
        if snap.market_status.upper() not in {"OPEN", "ACTIVE"}:
            reasons.append("MARKET_NOT_TRADABLE")
        if spread_metrics["spread_price"] is None:
            reasons.append("SPREAD_LIMIT_EXCEEDED")
        return list(dict.fromkeys(reasons))

    def _liquidity_cap(
        self,
        snap: MarketRiskSnapshot,
        reasons: list[str],
        hard_blocks: list[str],
    ) -> int:
        caps: list[int] = []

        def missing(reason_code: str) -> int:
            reasons.append(reason_code)
            if self.config.missing_optional_liquidity_data_action == "block":
                hard_blocks.append(reason_code)
                return 0
            return 1

        if "depth" in self.config.required_liquidity_sources:
            if snap.executable_depth_contracts is None:
                caps.append(missing("DEPTH_DATA_MISSING"))
            else:
                caps.append(
                    int(
                        (
                            snap.executable_depth_contracts
                            * self.config.max_depth_participation_fraction
                        )
                        .to_integral_value(rounding=ROUND_FLOOR)
                    )
                )
        if "recent_volume" in self.config.required_liquidity_sources:
            if snap.recent_volume_contracts is None:
                caps.append(missing("VOLUME_DATA_MISSING"))
            else:
                caps.append(
                    int(
                        (
                            snap.recent_volume_contracts
                            * self.config.max_recent_volume_participation_fraction
                        ).to_integral_value(rounding=ROUND_FLOOR)
                    )
                )
        if self.config.max_adv_participation_fraction is not None:
            if snap.average_daily_volume_contracts is None:
                caps.append(missing("VOLUME_DATA_MISSING"))
            else:
                caps.append(
                    int(
                        (
                            snap.average_daily_volume_contracts
                            * self.config.max_adv_participation_fraction
                        ).to_integral_value(rounding=ROUND_FLOOR)
                    )
                )
        if self.config.max_open_interest_participation_fraction is not None:
            if snap.open_interest_contracts is None:
                caps.append(missing("VOLUME_DATA_MISSING"))
            else:
                caps.append(
                    int(
                        (
                            snap.open_interest_contracts
                            * self.config.max_open_interest_participation_fraction
                        ).to_integral_value(rounding=ROUND_FLOOR)
                    )
                )
        return min(caps) if caps else 1

    def _trade_caps(
        self,
        request: AdvancedRiskRequest,
        *,
        risk_per_contract: Decimal,
    ) -> tuple[dict[str, int], list[str], list[str], dict[str, Decimal | None]]:
        reasons: list[str] = []
        hard_blocks: list[str] = []
        metrics: dict[str, Decimal | None] = {}
        confidence_cap = min(
            request.phase_3m_proposed_contracts,
            self._confidence_floor_cap(request),
        )
        caps = {"confidence": confidence_cap}
        if confidence_cap < request.phase_3m_proposed_contracts:
            reasons.append("CONFIDENCE_CAP_APPLIED")

        kelly_cap, kelly_reasons, kelly_metrics = self._kelly_cap(
            request,
            risk_per_contract,
        )
        ev_cap, ev_reasons, ev_blocks, ev_metrics = self._ev_cap(
            request,
            risk_per_contract,
        )
        caps["kelly"] = kelly_cap
        caps["risk_adjusted_ev"] = ev_cap
        reasons.extend(kelly_reasons + ev_reasons)
        hard_blocks.extend(ev_blocks)
        metrics.update(kelly_metrics)
        metrics.update(ev_metrics)
        return caps, reasons, hard_blocks, metrics

    def _confidence_floor_cap(self, request: AdvancedRiskRequest) -> int:
        score = request.confidence_score or Decimal("0")
        if score < self.config.confidence_defensive_medium_floor:
            return 1
        if score < self.config.confidence_defensive_high_floor:
            return 3
        return 5

    def _kelly_cap(
        self,
        request: AdvancedRiskRequest,
        risk_per_contract: Decimal,
    ) -> tuple[int, list[str], dict[str, Decimal | None]]:
        if not self.config.kelly_enabled:
            return 5, [], {}
        stats = request.edge_statistics
        if stats is None or stats.raw_win_probability is None:
            return self.config.kelly_insufficient_data_cap, ["KELLY_STATS_MISSING"], {}
        if stats.sample_size < self.config.kelly_minimum_sample_size:
            return (
                self.config.kelly_insufficient_data_cap,
                ["KELLY_SAMPLE_TOO_SMALL"],
                {},
            )
        averages = self._net_average_outcomes(request, stats)
        if averages is None:
            return self.config.kelly_insufficient_data_cap, ["KELLY_STATS_MISSING"], {}
        avg_win, avg_loss = averages
        adjusted = self._adjusted_win_probability(stats)
        payoff_ratio = avg_win / avg_loss
        full_kelly = adjusted - ((Decimal("1") - adjusted) / payoff_ratio)
        nonnegative = max(Decimal("0"), full_kelly)
        applied = min(
            nonnegative * self.config.fractional_kelly_multiplier,
            self.config.max_applied_kelly_fraction,
            self.config.max_trade_risk_fraction,
        )
        budget = request.portfolio_snapshot.account_equity * applied
        cap = _raw_cap(budget, risk_per_contract)
        reasons: list[str] = []
        if full_kelly <= 0:
            reasons.append("KELLY_NONPOSITIVE")
        elif cap < request.phase_3m_proposed_contracts:
            reasons.append("KELLY_CAP_APPLIED")
        return (
            cap,
            reasons,
            {
                "adjusted_win_probability": adjusted,
                "payoff_ratio": payoff_ratio,
                "full_kelly_fraction": full_kelly,
                "applied_kelly_fraction": applied,
                "kelly_risk_budget": budget,
            },
        )

    def _ev_cap(
        self,
        request: AdvancedRiskRequest,
        risk_per_contract: Decimal,
    ) -> tuple[int, list[str], list[str], dict[str, Decimal | None]]:
        if not self.config.ev_enabled:
            return 5, [], [], {}
        stats = request.edge_statistics
        if stats is None or stats.raw_win_probability is None:
            return self.config.ev_insufficient_data_cap, ["EV_STATS_MISSING"], [], {}
        if stats.sample_size < self.config.ev_minimum_sample_size:
            return self.config.ev_insufficient_data_cap, ["EV_SAMPLE_TOO_SMALL"], [], {}
        averages = self._net_average_outcomes(request, stats)
        if averages is None:
            return self.config.ev_insufficient_data_cap, ["EV_STATS_MISSING"], [], {}
        avg_win, avg_loss = averages
        adjusted = self._adjusted_win_probability(stats)
        effective_sample_size = Decimal(stats.sample_size + self.config.kelly_prior_weight)
        variance = adjusted * (Decimal("1") - adjusted) / max(effective_sample_size, Decimal("1"))
        standard_error = Decimal(str(sqrt(float(max(variance, Decimal("0"))))))
        conservative_probability = max(
            Decimal("0"),
            adjusted - self.config.ev_probability_z_score * standard_error,
        )
        tail_charge = Decimal("0")
        if stats.cvar_loss_per_contract is not None:
            tail_charge = (
                max(Decimal("0"), stats.cvar_loss_per_contract - avg_loss)
                * self.config.cvar_weight
            )
        ev = conservative_probability * avg_win
        ev -= (Decimal("1") - conservative_probability) * avg_loss
        ev -= tail_charge
        ev_to_risk = ev / risk_per_contract
        metrics = {
            "adjusted_win_probability": adjusted,
            "risk_adjusted_ev_per_contract": ev,
            "risk_adjusted_ev_to_risk": ev_to_risk,
        }
        if ev_to_risk <= self.config.minimum_trade_ev_to_risk:
            return 0, ["NONPOSITIVE_RISK_ADJUSTED_EV"], ["NONPOSITIVE_RISK_ADJUSTED_EV"], metrics
        if ev_to_risk < self.config.medium_ev_to_risk:
            return 1, ["RISK_ADJUSTED_EV_CAP_APPLIED"], [], metrics
        if ev_to_risk < self.config.high_ev_to_risk:
            return 3, ["RISK_ADJUSTED_EV_CAP_APPLIED"], [], metrics
        return 5, [], [], metrics

    def _net_average_outcomes(
        self,
        request: AdvancedRiskRequest,
        stats: TradeEdgeStatistics,
    ) -> tuple[Decimal, Decimal] | None:
        avg_win = stats.average_gross_win_per_contract
        avg_loss = stats.average_gross_loss_per_contract
        if avg_win is None or avg_loss is None:
            return None
        if not _positive(avg_win) or not _positive(avg_loss):
            return None
        if stats.outcome_basis.upper() == "GROSS":
            cost = (
                request.estimated_round_trip_fees
                + request.estimated_slippage_per_contract
                + (request.market_snapshot.expected_market_impact_per_contract or Decimal("0"))
            )
            avg_win -= cost
            avg_loss += cost
        if not _positive(avg_win) or not _positive(avg_loss):
            return None
        return avg_win, avg_loss

    def _adjusted_win_probability(self, stats: TradeEdgeStatistics) -> Decimal:
        sample_size = Decimal(stats.sample_size)
        prior_weight = Decimal(self.config.kelly_prior_weight)
        denominator = sample_size + prior_weight
        if denominator <= 0:
            return self.config.kelly_prior_win_probability
        raw = stats.raw_win_probability or self.config.kelly_prior_win_probability
        return (
            raw * sample_size + self.config.kelly_prior_win_probability * prior_weight
        ) / denominator

    def _spread_metrics(
        self,
        snap: MarketRiskSnapshot,
        tick_size: Decimal | None,
    ) -> dict[str, Decimal | None]:
        if snap.bid_price is None or snap.ask_price is None or tick_size is None:
            return {"spread_price": None, "spread_ticks": None, "spread_bps": None}
        if not _positive(snap.bid_price) or not _positive(snap.ask_price) or tick_size <= 0:
            return {"spread_price": None, "spread_ticks": None, "spread_bps": None}
        if snap.ask_price < snap.bid_price:
            return {"spread_price": None, "spread_ticks": None, "spread_bps": None}
        spread = snap.ask_price - snap.bid_price
        mid = (snap.bid_price + snap.ask_price) / Decimal("2")
        return {
            "spread_price": spread,
            "spread_ticks": spread / tick_size,
            "spread_bps": (spread / mid) * Decimal("10000") if mid > 0 else None,
        }

    def _liquidity_evidence(self, snap: MarketRiskSnapshot) -> dict[str, Any]:
        return {
            "executable_depth_contracts": _optional_decimal_str(snap.executable_depth_contracts),
            "depth_price_band_ticks": _optional_decimal_str(snap.depth_price_band_ticks),
            "recent_volume_contracts": _optional_decimal_str(snap.recent_volume_contracts),
            "average_daily_volume_contracts": _optional_decimal_str(
                snap.average_daily_volume_contracts
            ),
            "open_interest_contracts": _optional_decimal_str(snap.open_interest_contracts),
        }

    def _observed_session_loss(self, snap: PortfolioRiskSnapshot) -> Decimal:
        pnl_for_limit = snap.realized_pnl_session + (
            self.config.unrealized_pnl_weight * snap.unrealized_pnl_session
        )
        return max(Decimal("0"), -pnl_for_limit)

    def _current_drawdown(self, snap: PortfolioRiskSnapshot) -> Decimal:
        return max(Decimal("0"), snap.high_water_equity - snap.account_equity)

    def _bucket_committed_risk(
        self,
        open_risk: dict[str, Decimal],
        pending_risk: dict[str, Decimal],
        bucket: str,
    ) -> Decimal:
        return open_risk.get(bucket, Decimal("0")) + pending_risk.get(bucket, Decimal("0"))

    def _action(self, live_candidate: int, proposed: int) -> AdvancedRiskAction:
        if live_candidate == 0:
            return AdvancedRiskAction.BLOCK
        if live_candidate < proposed:
            return AdvancedRiskAction.REDUCE
        return AdvancedRiskAction.ALLOW

    def _executed_contracts(
        self,
        *,
        mode: AdvancedRiskMode,
        phase_3m_contracts: int,
        live_candidate: int,
        hard_blocks: tuple[str, ...],
    ) -> int:
        if (
            "EXTERNAL_HARD_RISK_BLOCK" in hard_blocks
            or "PHASE_3M_INPUT_INCONSISTENT" in hard_blocks
        ):
            return 0
        if mode == AdvancedRiskMode.LIVE:
            return live_candidate
        return phase_3m_contracts

    def _limiting_reasons(self, limiting: tuple[str, ...]) -> list[str]:
        mapping = {
            "category": "CATEGORY_CAP_APPLIED",
            "model": "MODEL_CAP_APPLIED",
            "daily_loss": "DAILY_LOSS_CAP_APPLIED",
            "drawdown": "DRAWDOWN_CAP_APPLIED",
            "spread": "SPREAD_CAP_APPLIED",
            "liquidity": "LIQUIDITY_CAP_APPLIED",
            "concentration": "POSITION_CONCENTRATION_CAP_APPLIED",
            "correlation_group": "CORRELATION_GROUP_CAP_APPLIED",
            "kelly": "KELLY_CAP_APPLIED",
            "confidence": "CONFIDENCE_CAP_APPLIED",
            "risk_adjusted_ev": "RISK_ADJUSTED_EV_CAP_APPLIED",
            "margin": "MARGIN_CAP_APPLIED",
            "buying_power": "BUYING_POWER_CAP_APPLIED",
            "rollout": "ROLLOUT_CAP_APPLIED",
            "global": "GLOBAL_CAP_APPLIED",
        }
        return [mapping[name] for name in limiting if name in mapping]

    def _telemetry(
        self,
        request: AdvancedRiskRequest,
        *,
        action: AdvancedRiskAction,
        live_candidate: int,
        executed: int,
        risk_per_contract: Decimal,
        raw_caps: dict[str, int],
        bucketed_caps: dict[str, int],
        limiting_factors: tuple[str, ...],
        hard_blocks: tuple[str, ...],
        reasons: tuple[str, ...],
    ) -> dict[str, Any]:
        trade_metrics = getattr(self, "_last_trade_metrics", {})
        return {
            "event": "advanced_risk_decision",
            "version": self.config.version,
            "mode": self.config.mode.value,
            "action": action.value,
            "strategy_id": request.strategy_id,
            "model_id": request.model_id,
            "category_id": request.category_id,
            "instrument_id": request.instrument_id,
            "trade_intent_id": request.trade_intent_id,
            "phase_3m_tier": request.phase_3m_tier,
            "phase_3m_proposed_contracts": request.phase_3m_proposed_contracts,
            "live_candidate_contracts": live_candidate,
            "executed_contracts": executed,
            "risk_per_contract": risk_per_contract,
            "planned_trade_risk": risk_per_contract * Decimal(executed),
            "raw_caps": raw_caps,
            "bucketed_caps": bucketed_caps,
            "limiting_factors": limiting_factors,
            "hard_blocks": hard_blocks,
            "reason_codes": reasons,
            "portfolio_snapshot_version": request.portfolio_snapshot.snapshot_version,
            "market_data_age_ms": request.market_snapshot.quote_age_ms,
            "edge_sample_size": (
                request.edge_statistics.sample_size if request.edge_statistics else 0
            ),
            "edge_bucket_level": (
                request.edge_statistics.bucket_level if request.edge_statistics else "missing"
            ),
            "reservation_status": (
                "pending" if self.config.mode == AdvancedRiskMode.LIVE else "none"
            ),
            "decision_timestamp": request.decision_timestamp.isoformat(),
            **trade_metrics,
        }


def _raw_cap(remaining: Decimal, risk_per_contract: Decimal) -> int:
    if not _positive(risk_per_contract):
        return 0
    if remaining <= 0:
        return 0
    return int((remaining / risk_per_contract).to_integral_value(rounding=ROUND_FLOOR))


def _age_ms(now: datetime, captured_at: datetime) -> int | None:
    if now.tzinfo is None or captured_at.tzinfo is None:
        return None
    return int((now.astimezone(UTC) - captured_at.astimezone(UTC)).total_seconds() * 1000)


def _is_unknown(value: str | None) -> bool:
    return not value or value.strip().upper() == UNKNOWN


def _is_unit_decimal(value: Decimal) -> bool:
    return _non_negative(value) and value <= Decimal("1")


def _positive(value: Decimal | None) -> bool:
    return value is not None and value.is_finite() and value > 0


def _non_negative(value: Decimal | None) -> bool:
    return value is not None and value.is_finite() and value >= 0


def _decimal_str(value: Decimal) -> str:
    return decimal_to_str(value) or "0"


def _optional_decimal_str(value: Decimal | None) -> str | None:
    return decimal_to_str(value) if value is not None else None


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return _decimal_str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    return value
