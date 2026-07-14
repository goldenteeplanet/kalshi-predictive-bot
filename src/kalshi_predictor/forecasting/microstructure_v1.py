from decimal import Decimal

from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.data.schema import MarketSnapshot
from kalshi_predictor.forecasting.base import ForecastOutput
from kalshi_predictor.microstructure.repository import latest_microstructure_feature
from kalshi_predictor.utils.decimals import ONE_DOLLAR, to_decimal


class MicrostructureV1Forecaster:
    model_name = "microstructure_v1"

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def forecast(self, session: Session, snapshot: MarketSnapshot) -> ForecastOutput | None:
        feature = latest_microstructure_feature(session, snapshot.ticker)
        if feature is None:
            return None
        if feature.snapshot_count < self.settings.microstructure_min_snapshots:
            return None
        midpoint = _market_midpoint(snapshot)
        if midpoint is None:
            return None
        adjustment = _adjustment(feature, self.settings)
        probability = _clamp(midpoint + adjustment)
        return ForecastOutput(
            ticker=snapshot.ticker,
            forecasted_at=snapshot.captured_at,
            model_name=self.model_name,
            yes_probability=probability,
            market_mid_probability=midpoint,
            best_yes_bid=to_decimal(snapshot.best_yes_bid),
            best_yes_ask=to_decimal(snapshot.best_yes_ask),
            feature_json={
                "microstructure_feature_id": feature.id,
                "orderbook_imbalance": feature.orderbook_imbalance,
                "price_velocity": feature.price_velocity,
                "late_move_score": feature.late_move_score,
                "dislocation_score": feature.dislocation_score,
                "smart_money_score": feature.smart_money_score,
                "microstructure_confidence": feature.microstructure_confidence,
                "adjustment": str(adjustment),
                "max_adjustment": str(self.settings.microstructure_v1_max_adjustment),
            },
            notes=(
                "microstructure_v1 midpoint-adjusted forecast from stored orderbook "
                "and short-term market behavior features."
            ),
        )


def _adjustment(feature: object, settings: Settings) -> Decimal:
    imbalance = to_decimal(getattr(feature, "orderbook_imbalance", None)) or Decimal("0")
    velocity = to_decimal(getattr(feature, "price_velocity", None)) or Decimal("0")
    late = to_decimal(getattr(feature, "late_move_score", None)) or Decimal("0")
    dislocation = to_decimal(getattr(feature, "dislocation_score", None)) or Decimal("0")
    flow = to_decimal(getattr(feature, "smart_money_score", None)) or Decimal("0")
    direction = Decimal("1") if imbalance + velocity >= 0 else Decimal("-1")
    raw = (
        imbalance * Decimal("0.025")
        + velocity * Decimal("0.50")
        + direction * late * Decimal("0.015")
        + direction * dislocation * Decimal("0.010")
        + direction * flow * Decimal("0.010")
    )
    max_adjustment = settings.microstructure_v1_max_adjustment
    if raw > max_adjustment:
        return max_adjustment
    if raw < -max_adjustment:
        return -max_adjustment
    return raw


def _market_midpoint(snapshot: MarketSnapshot) -> Decimal | None:
    bid = to_decimal(snapshot.best_yes_bid)
    ask = to_decimal(snapshot.best_yes_ask)
    if bid is not None and ask is not None:
        return (bid + ask) / Decimal("2")
    last_price = to_decimal(snapshot.last_price_dollars)
    if last_price is not None:
        return last_price
    no_bid = to_decimal(snapshot.best_no_bid)
    if no_bid is not None:
        return ONE_DOLLAR - no_bid
    return None


def _clamp(value: Decimal) -> Decimal:
    if value < Decimal("0.01"):
        return Decimal("0.01")
    if value > Decimal("0.99"):
        return Decimal("0.99")
    return value

