from dataclasses import replace
from datetime import datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.data.repositories import decode_json
from kalshi_predictor.data.schema import MarketSnapshot, MicrostructureFeature
from kalshi_predictor.forecasting.base import ForecastOutput
from kalshi_predictor.microstructure.provenance import (
    FeatureBuildContext,
    aware,
    bound_close_time,
    record_hash,
    record_time,
    settings_hash,
)
from kalshi_predictor.microstructure.repository import latest_microstructure_feature
from kalshi_predictor.utils.decimals import ONE_DOLLAR, to_decimal
from kalshi_predictor.utils.time import utc_now


class MicrostructureV1Forecaster:
    model_name = "microstructure_v1"

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def forecast(self, session: Session, snapshot: MarketSnapshot) -> ForecastOutput | None:
        """Compatibility path: latest stored features, explicitly not temporal evidence."""
        feature = latest_microstructure_feature(session, snapshot.ticker)
        return self._forecast(
            snapshot, feature, snapshot.captured_at, {"timing_status": "LEGACY_UNVERIFIED"}
        )

    def forecast_bound(
        self,
        session: Session,
        snapshot: MarketSnapshot,
        *,
        feature_id: int,
        feature_sha256: str,
        context: FeatureBuildContext,
    ) -> ForecastOutput | None:
        """Bind an exact genuine feature; recording/decision must follow this computation."""
        from kalshi_predictor.microstructure.orderbook_features import (
            compute_microstructure_feature,
        )

        if type(feature_id) is not int or feature_id <= 0:
            raise ValueError("POSITIVE_FEATURE_ID_REQUIRED")
        if not self.settings.microstructure_v1_max_adjustment.is_finite():
            raise ValueError("NONFINITE_ADJUSTMENT_SETTING")
        feature = session.get(MicrostructureFeature, feature_id)
        if (
            feature is None
            or feature.ticker != snapshot.ticker
            or record_hash(feature) != feature_sha256
        ):
            raise ValueError("SELECTED_FEATURE_HASH_MISMATCH")
        raw = decode_json(feature.raw_json)
        lineage = raw.get("prospective_lineage")
        if (
            not isinstance(lineage, dict)
            or lineage.get("schema") != "microstructure-cutoff-lineage-v1"
        ):
            raise ValueError("UNPROVEN_FEATURE_LINEAGE")
        if lineage.get("settings_sha256") != settings_hash(self.settings):
            raise ValueError("FEATURE_SETTINGS_MISMATCH")
        snapshots = [session.get(MarketSnapshot, r.record_id) for r in context.snapshots]
        if any(s is None for s in snapshots):
            raise ValueError("BOUND_SNAPSHOT_MISSING")
        bound_snapshots = [s for s in snapshots if s is not None]
        if not bound_snapshots or record_hash(bound_snapshots[-1]) != record_hash(snapshot):
            raise ValueError("FORECAST_SNAPSHOT_BINDING_MISMATCH")
        # Recompute using the exact supplied receipts, settings and ensemble. A raw_json
        # marker alone is not provenance; changed input records fail in context.bind.
        expected = compute_microstructure_feature(
            session,
            bound_snapshots,
            lookback_minutes=feature.lookback_minutes,
            settings=self.settings,
            context=context,
        )
        expected_lineage = expected["raw_json"]["prospective_lineage"]
        if {k: v for k, v in lineage.items() if k != "feature_created_at"} != {
            k: v for k, v in expected_lineage.items() if k != "feature_created_at"
        }:
            raise ValueError("FEATURE_LINEAGE_MISMATCH")
        for key, value in expected.items():
            if key in {"created_at", "raw_json"}:
                continue
            actual = getattr(feature, key)
            if isinstance(value, Decimal):
                numeric = to_decimal(actual)
                if numeric is None or not numeric.is_finite():
                    raise ValueError("NONFINITE_FEATURE_VALUE")
            if to_decimal(actual) != value if isinstance(value, Decimal) else actual != value:
                raise ValueError("FEATURE_VALUE_MISMATCH")
        computed = utc_now()
        created = record_time(feature.created_at)
        if (
            lineage.get("feature_created_at") != created.isoformat()
            or not aware(context.model_input_as_of) <= created <= computed
        ):
            raise ValueError("FEATURE_CREATION_CLOCK_MISMATCH")
        target = bound_close_time(decode_json(snapshot.raw_market_json))
        if computed >= target:
            raise ValueError("EXPIRED_OR_UNPROVEN_FORECAST_TARGET")
        output = self._forecast(
            snapshot,
            feature,
            computed,
            {
                "timing_status": "BOUND_INPUTS_RECORDING_REQUIRED",
                "snapshot_at": record_time(snapshot.captured_at).isoformat(),
                "model_input_as_of": aware(context.model_input_as_of).isoformat(),
                "feature_created_at": created.isoformat(),
                "prediction_computed_at": computed.isoformat(),
                "selected_feature_sha256": feature_sha256,
                "prediction_recorded_at": None,
                "decision_at": None,
                "release_certified": False,
                "execution_authority": False,
            },
        )
        completed = utc_now()
        if completed < computed or completed >= target:
            raise ValueError("PREDICTION_COMPLETION_CLOCK_OR_EXPIRY")
        if output is None:
            return None
        if not output.yes_probability.is_finite():
            raise ValueError("NONFINITE_PROBABILITY")
        return replace(
            output,
            forecasted_at=completed,
            feature_json={
                **output.feature_json,
                "prediction_computed_at": completed.isoformat(),
            },
        )

    def _forecast(
        self,
        snapshot: MarketSnapshot,
        feature: MicrostructureFeature | None,
        forecasted_at: datetime,
        timing: dict,
    ) -> ForecastOutput | None:
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
            forecasted_at=forecasted_at,
            model_name=self.model_name,
            yes_probability=probability,
            market_mid_probability=midpoint,
            best_yes_bid=to_decimal(snapshot.best_yes_bid),
            best_yes_ask=to_decimal(snapshot.best_yes_ask),
            feature_json={
                **timing,
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
