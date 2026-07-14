from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from kalshi_predictor.data.repositories import decode_json
from kalshi_predictor.opportunities.scoring import score_edge
from kalshi_predictor.paper.models import BUY_NO, BUY_YES
from kalshi_predictor.utils.decimals import ONE_DOLLAR, decimal_to_str, to_decimal

ZERO = Decimal("0")
HUNDRED = Decimal("100")


@dataclass(frozen=True)
class PayoutMetrics:
    side: str | None
    probability: Decimal | None
    cost: Decimal | None
    payout_if_correct: Decimal | None
    downside_if_wrong: Decimal | None
    risk_adjusted_edge: Decimal | None
    payout_to_risk_ratio: Decimal | None
    expected_value: Decimal | None
    expected_value_score: Decimal
    payout_adjusted_score: Decimal

    def as_dict(self) -> dict[str, str | None]:
        return {
            "payout_if_correct": decimal_to_str(self.payout_if_correct),
            "downside_if_wrong": decimal_to_str(self.downside_if_wrong),
            "risk_adjusted_edge": decimal_to_str(self.risk_adjusted_edge),
            "payout_to_risk_ratio": decimal_to_str(self.payout_to_risk_ratio),
            "expected_value": decimal_to_str(self.expected_value),
            "expected_value_score": decimal_to_str(self.expected_value_score),
            "payout_adjusted_score": decimal_to_str(self.payout_adjusted_score),
        }


def calculate_payout_metrics(
    *,
    side: str | None,
    yes_probability: Any,
    cost: Any,
    edge: Any = None,
    liquidity_score: Any = None,
    spread_score: Any = None,
    confidence_score: Any = None,
    time_score: Any = None,
) -> PayoutMetrics:
    probability = probability_for_side(side=side, yes_probability=yes_probability)
    cost_value = to_decimal(cost)
    if side not in {BUY_YES, BUY_NO} or probability is None or cost_value is None:
        return PayoutMetrics(
            side=side,
            probability=probability,
            cost=cost_value,
            payout_if_correct=None,
            downside_if_wrong=None,
            risk_adjusted_edge=None,
            payout_to_risk_ratio=None,
            expected_value=None,
            expected_value_score=ZERO,
            payout_adjusted_score=ZERO,
        )

    payout_if_correct = ONE_DOLLAR - cost_value
    downside_if_wrong = cost_value
    expected_value = (
        probability * payout_if_correct
        - (ONE_DOLLAR - probability) * downside_if_wrong
    )
    payout_to_risk_ratio = (
        payout_if_correct / downside_if_wrong if downside_if_wrong > ZERO else None
    )
    raw_edge = to_decimal(edge)
    if raw_edge is None:
        raw_edge = probability - cost_value
    confidence = (to_decimal(confidence_score) or ZERO) / HUNDRED
    risk_adjusted_edge = raw_edge * confidence
    expected_value_score = _score_expected_value(expected_value)
    payout_adjusted_score = calculate_payout_adjusted_score(
        expected_value_score=expected_value_score,
        edge=raw_edge,
        liquidity_score=liquidity_score,
        spread_score=spread_score,
        confidence_score=confidence_score,
        time_score=time_score,
    )
    return PayoutMetrics(
        side=side,
        probability=probability,
        cost=cost_value,
        payout_if_correct=payout_if_correct,
        downside_if_wrong=downside_if_wrong,
        risk_adjusted_edge=risk_adjusted_edge,
        payout_to_risk_ratio=payout_to_risk_ratio,
        expected_value=expected_value,
        expected_value_score=expected_value_score,
        payout_adjusted_score=payout_adjusted_score,
    )


def probability_for_side(*, side: str | None, yes_probability: Any) -> Decimal | None:
    yes_value = to_decimal(yes_probability)
    if yes_value is None:
        return None
    if side == BUY_YES:
        return yes_value
    if side == BUY_NO:
        return ONE_DOLLAR - yes_value
    return None


def calculate_payout_adjusted_score(
    *,
    expected_value_score: Any,
    edge: Any,
    liquidity_score: Any,
    spread_score: Any,
    confidence_score: Any,
    time_score: Any,
) -> Decimal:
    return _clamp_score(
        (to_decimal(expected_value_score) or ZERO) * Decimal("0.30")
        + score_edge(edge) * Decimal("0.25")
        + (to_decimal(liquidity_score) or ZERO) * Decimal("0.15")
        + (to_decimal(spread_score) or ZERO) * Decimal("0.15")
        + (to_decimal(confidence_score) or ZERO) * Decimal("0.10")
        + (to_decimal(time_score) or ZERO) * Decimal("0.05")
    )


def payout_metrics_from_ranking(ranking: Any) -> PayoutMetrics:
    raw = decode_json(_field(ranking, "raw_json"))
    cost = _field(ranking, "best_price")
    metrics = calculate_payout_metrics(
        side=_field(ranking, "best_side"),
        yes_probability=_field(ranking, "forecast_probability"),
        cost=cost,
        edge=_field(ranking, "estimated_edge"),
        liquidity_score=_field(ranking, "liquidity_score"),
        spread_score=_field(ranking, "spread_score"),
        confidence_score=_field(ranking, "model_confidence_score"),
        time_score=_field(ranking, "time_score"),
    )
    if metrics.expected_value is not None:
        return metrics
    return calculate_payout_metrics(
        side=_field(ranking, "best_side"),
        yes_probability=_field(ranking, "forecast_probability"),
        cost=cost,
        edge=raw.get("risk_adjusted_edge") or _field(ranking, "estimated_edge"),
        liquidity_score=_field(ranking, "liquidity_score"),
        spread_score=_field(ranking, "spread_score"),
        confidence_score=_field(ranking, "model_confidence_score"),
        time_score=_field(ranking, "time_score"),
    )


def is_acceptable_best_payout(ranking: Any, metrics: PayoutMetrics) -> bool:
    if metrics.expected_value is None or metrics.expected_value <= ZERO:
        return False
    if not _field(ranking, "best_side") or not _field(ranking, "best_price"):
        return False
    confidence = to_decimal(_field(ranking, "model_confidence_score")) or ZERO
    liquidity = to_decimal(_field(ranking, "liquidity_score")) or ZERO
    spread = to_decimal(_field(ranking, "spread"))
    price = to_decimal(_field(ranking, "best_price")) or ZERO
    if confidence < Decimal("40") or liquidity < Decimal("30"):
        return False
    if spread is not None and spread > Decimal("0.15"):
        return False
    if price <= Decimal("0.25") and (confidence < Decimal("60") or liquidity < Decimal("50")):
        return False
    return True


def _score_expected_value(value: Decimal | None) -> Decimal:
    if value is None or value <= ZERO:
        return ZERO
    return _clamp_score((value / Decimal("0.25")) * HUNDRED)


def _clamp_score(value: Decimal) -> Decimal:
    if value < ZERO:
        return ZERO
    if value > HUNDRED:
        return HUNDRED
    return value


def _field(row: Any, name: str) -> Any:
    if isinstance(row, dict):
        return row.get(name)
    return getattr(row, name, None)
