"""Versioned one-contract research economics; no paper or execution authority."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from kalshi_predictor.utils.single_fill_fees import single_buy_fill


class EvidenceStatus(StrEnum):
    CERTIFIED = "CERTIFIED"
    ESTIMATED = "ESTIMATED"
    UNKNOWN = "UNKNOWN"


def number(value: Decimal) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError("FINITE_DECIMAL_REQUIRED")
    return value


@dataclass(frozen=True)
class CostComponent:
    value: Decimal | None
    status: EvidenceStatus
    method_version: str
    evidence_hashes: tuple[str, ...]
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.status, EvidenceStatus):
            raise ValueError("EXPLICIT_EVIDENCE_STATUS_REQUIRED")
        if not self.method_version or not self.reason:
            raise ValueError("METHOD_AND_REASON_REQUIRED")
        if self.status == EvidenceStatus.UNKNOWN:
            if self.value is not None:
                raise ValueError("UNKNOWN_MUST_REMAIN_NULL")
        elif self.value is None or number(self.value) < 0 or not self.evidence_hashes:
            raise ValueError("NONNEGATIVE_COST_AND_EVIDENCE_REQUIRED")
        for digest in self.evidence_hashes:
            if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                raise ValueError("SHA256_REQUIRED")


def unknown(method: str, reason: str) -> CostComponent:
    return CostComponent(None, EvidenceStatus.UNKNOWN, method, (), reason)


def single_buy_fee(
    *, price: Decimal, multiplier: Decimal, rate: Decimal, balance_precision: Decimal
) -> dict[str, Decimal]:
    """Pure arithmetic, one whole contract/one fill/zero prior accumulator.

    Implements the documented six-decimal trade fee and balance alignment.
    The caller must separately prove current rate, multiplier and account class.
    """
    if not 0 < number(price) < 1:
        raise ValueError("OPEN_UNIT_PRICE_REQUIRED")
    if number(multiplier) < 0 or number(rate) < 0:
        raise ValueError("NONNEGATIVE_RATE_REQUIRED")
    if balance_precision not in (Decimal("0.01"), Decimal("0.0001")):
        raise ValueError("DOCUMENTED_ACCOUNT_PRECISION_REQUIRED")
    result = single_buy_fill(
        price=price, multiplier=multiplier, rate=rate, balance_precision=balance_precision
    )
    return {key: value for key, value in result.items() if key != "total_debit"}


def book_slippage(
    *,
    executable_price: Decimal,
    depth: tuple[tuple[Decimal, Decimal], ...],
    recent_asks: tuple[Decimal, ...],
    evidence_hashes: tuple[str, ...],
    current_and_time_ordered: bool,
) -> CostComponent:
    """One-contract depth cost plus observed adverse ask movement.

    Inputs are executable asks for the selected side. Recent asks must belong
    to the same ticker/side and a prospectively declared observation interval.
    Caller supplies validated original hashes and freshness/order verdict.
    This descriptive allowance is ESTIMATED, never a liquidity guarantee.
    """
    method = "ONE_CONTRACT_DEPTH_PLUS_ADVERSE_ASK_RANGE_V1"
    if not current_and_time_ordered or len(recent_asks) < 2 or not evidence_hashes:
        return unknown(method, "CURRENT_BOOK_AND_QUOTE_MOVEMENT_EVIDENCE_REQUIRED")
    if not 0 < number(executable_price) < 1 or not depth:
        raise ValueError("EXECUTABLE_BOOK_REQUIRED")
    remaining = Decimal(1)
    spend = Decimal(0)
    previous = Decimal(0)
    for price, quantity in depth:
        if not 0 < number(price) < 1 or price < previous or number(quantity) <= 0:
            raise ValueError("SORTED_POSITIVE_ASK_DEPTH_REQUIRED")
        previous = price
        take = min(remaining, quantity)
        spend += take * price
        remaining -= take
    if remaining or depth[0][0] != executable_price:
        return unknown(method, "ONE_CONTRACT_DEPTH_OR_PRICE_BINDING_MISSING")
    for price in recent_asks:
        if not 0 < number(price) < 1:
            raise ValueError("VALID_RECENT_ASK_REQUIRED")
    adverse = max(Decimal(0), max(recent_asks) - executable_price)
    return CostComponent(
        spend - executable_price + adverse,
        EvidenceStatus.ESTIMATED,
        method,
        evidence_hashes,
        "OBSERVED_DEPTH_AND_ADVERSE_ASK_MOVEMENT",
    )


@dataclass(frozen=True)
class DependenceCounts:
    contracts: int
    events: int
    asset_hours: int
    independent_events: int | None
    clusters: int

    def __post_init__(self) -> None:
        for value in (self.contracts, self.events, self.asset_hours, self.clusters):
            if type(value) is not int or value < 0:
                raise ValueError("NONNEGATIVE_COUNTS_REQUIRED")
        if self.events > self.contracts or self.asset_hours > self.events:
            raise ValueError("COUNT_HIERARCHY_INVALID")
        if self.independent_events is not None and (
            type(self.independent_events) is not int
            or not 0 <= self.independent_events <= self.events
        ):
            raise ValueError("INDEPENDENT_EVENT_COUNT_INVALID")


@dataclass(frozen=True)
class FullCostResult:
    probability: Decimal
    executable_price: Decimal
    gross_edge: Decimal
    fee: CostComponent
    slippage: CostComponent
    uncertainty: CostComponent
    full_net_ev: Decimal | None
    shortfall_to_five_cents: Decimal | None
    clears_ev_gate: bool
    engine_version: str = "ONE_CONTRACT_FULL_COST_V2_CERTIFIED_FEE"
    execution_authority: bool = False


def full_costs(
    *,
    probability: Decimal,
    executable_price: Decimal,
    fee: CostComponent,
    slippage: CostComponent,
    uncertainty: CostComponent,
) -> FullCostResult:
    """Only applicable certified fees support full-net arithmetic.

    Estimated fee scenarios remain in the component provenance but cannot turn
    an unresolved account/series fee into known full net EV. Slippage and
    uncertainty may be explicit evidenced estimates; this grants no admission.
    """
    if not 0 <= number(probability) <= 1 or not 0 < number(executable_price) < 1:
        raise ValueError("VALID_PROBABILITY_AND_PRICE_REQUIRED")
    gross = probability - executable_price
    values = [component.value for component in (fee, slippage, uncertainty)]
    net = (
        None
        if fee.status != EvidenceStatus.CERTIFIED or any(v is None for v in values)
        else gross - sum((v for v in values if v is not None), Decimal(0))
    )
    return FullCostResult(
        probability,
        executable_price,
        gross,
        fee,
        slippage,
        uncertainty,
        net,
        None if net is None else max(Decimal(0), Decimal("0.05") - net),
        net is not None and net > Decimal("0.05"),
    )
