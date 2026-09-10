"""Pure BTC analytical proxy calculation; no persistence or release authority.

Existing feature code creates transient ORM values but never uses a session here.
A declared target/rule hash is provenance, not verified contract semantics.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from kalshi_predictor.crypto.distribution_model import (
    DistributionInputs,
    inputs_from_features,
    threshold_probability,
)
from kalshi_predictor.crypto.features import calculate_crypto_features
from kalshi_predictor.data.schema import CryptoPrice
from kalshi_predictor.overnight_paper.crypto_source import (
    BTC_SERIES,
    coinbase_feature_record,
    verify_coinbase_source,
)
from kalshi_predictor.overnight_paper.source_health import aware

MODEL_NAME = "btc_coinbase_terminal_proxy_v1"
MODEL_ENTRYPOINT = "kalshi_predictor.overnight_paper.btc_fixed_model:forecast_btc_proxy"
# Direct numerical dependencies, NOT a complete/audited transitive code closure.
DIRECT_DEPENDENCIES = (
    "kalshi_predictor.overnight_paper.btc_fixed_model",
    "kalshi_predictor.overnight_paper.crypto_source",
    "kalshi_predictor.crypto.features",
    "kalshi_predictor.crypto.distribution_model",
    "kalshi_predictor.data.schema",
    "kalshi_predictor.overnight_paper.source_health",
)


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def model_specification() -> dict[str, Any]:
    """Stable mathematical identity; deliberately not a code-freeze certificate."""
    return dict(
        name=MODEL_NAME,
        feature_window_minutes=1440,
        volatility_scale=1.0,
        volatility_order=["volatility_1h", "volatility_4h", "volatility_24h"],
        drift_return_window_minutes=60,
        drift_shrink=0.10,
        absolute_drift_cap_per_minute=0.0005,
        probability_bounds=[0.001, 0.999],
        price_basis="COINBASE_LAST_TRADE",
        horizon_basis="LAST_TRADE_TO_DECLARED_TARGET",
        payoff_approximation="CONTINUOUS_TERMINAL_PRICE_PROXY",
        code_closure_verified=False,
    )


@dataclass(frozen=True)
class BTCInputCalculation:
    verified: dict[str, Any]
    features: dict[str, Any]
    distribution: DistributionInputs
    horizon_end_at: datetime
    computed_at: datetime


def verified_btc_features(
    source: dict[str, Any], *, computed_at: datetime
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return verified originals and the exact existing closed-candle feature calculation."""
    generated = aware(computed_at)
    verified = verify_coinbase_source(source, decision_at=generated, now=generated)
    view = verified["inputs"]
    prices = [
        CryptoPrice(
            symbol="BTC",
            source="coinbase_closed_1m_candles",
            observed_at=datetime.fromtimestamp(row[0] + 60, UTC),
            price_usd=str(row[4]),
            raw_json=json.dumps(row),
        )
        for row in view["closed_candles"]
    ]
    features = calculate_crypto_features(prices, window_minutes=1440)
    return verified, features


def prepare_btc_inputs(
    source: dict[str, Any], *, horizon_end_at: datetime | str, computed_at: datetime
) -> BTCInputCalculation:
    """Shared exact-original feature math; horizon carries no settlement assertion."""
    generated = aware(computed_at)
    target = aware(horizon_end_at)
    if target <= generated:
        raise ValueError("BTC_TARGET_NOT_FUTURE")
    verified, features = verified_btc_features(source, computed_at=generated)
    view = verified["inputs"]
    model_features = dict(features, price=float(view["spot"]))
    for name in ("price", "volatility_1h", "volatility_4h", "volatility_24h", "return_1h"):
        value = model_features.get(name)
        if value is not None and (isinstance(value, bool) or not math.isfinite(float(value))):
            raise ValueError("BTC_NONFINITE_MODEL_INPUT")
    horizon = (target - aware(view["trade_at"])).total_seconds() / 60
    inputs = inputs_from_features(model_features, horizon_minutes=horizon)
    if inputs is None:
        raise ValueError("CRYPTO_DIAGNOSTIC_INSUFFICIENT_CANDLE_HISTORY")
    if not all(math.isfinite(value) for value in asdict(inputs).values()):
        raise ValueError("BTC_NONFINITE_DISTRIBUTION_INPUT")
    return BTCInputCalculation(verified, features, inputs, target, generated)


def _positive(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, str | int | float | Decimal):
        raise ValueError("BTC_INVALID_STRIKE")
    try:
        number = Decimal(str(value))
        result = float(number)
    except (InvalidOperation, ValueError, OverflowError) as exc:
        raise ValueError("BTC_INVALID_STRIKE") from exc
    if not number.is_finite() or number <= 0 or result <= 0 or not math.isfinite(result):
        raise ValueError("BTC_INVALID_STRIKE")
    return result


def forecast_btc_proxy(
    *,
    source: dict[str, Any],
    target: dict[str, Any],
    generated_at: datetime,
    decision_at: datetime,
    rule_original: bytes | None = None,
) -> dict[str, Any]:
    """Calculate an explicitly conditional research proxy, never contract approval.

    target must declare observation_at, exact BTC identity, comparator and strike.
    close_time is not accepted as a substitute. CF averaging/review/payoff semantics
    are intentionally unverified; a supplied rule original only binds its bytes.
    """
    required = {
        "ticker",
        "event_id",
        "series",
        "observation_at",
        "comparator",
        "threshold",
        "lower",
        "upper",
        "rule_sha256",
    }
    if set(target) != required:
        raise ValueError("BTC_EXPLICIT_TARGET_FIELDS_REQUIRED")
    # Canonical JSON preserves Decimal strike precision without mutating the caller.
    target = dict(target)
    for field in ("threshold", "lower", "upper"):
        if isinstance(target[field], Decimal):
            target[field] = str(target[field])
    series, event, ticker = target["series"], target["event_id"], target["ticker"]
    if (
        not isinstance(series, str)
        or series not in BTC_SERIES
        or not isinstance(event, str)
        or not event.startswith(series + "-")
        or not isinstance(ticker, str)
        or not ticker.startswith(event + "-")
    ):
        raise ValueError("BTC_EXACT_IDENTITY_REQUIRED")
    generated, decision = aware(generated_at), aware(decision_at)
    if generated > decision or aware(target["observation_at"]) <= decision:
        raise ValueError("BTC_EXECUTION_OR_TARGET_CLOCK")
    target["observation_at"] = aware(target["observation_at"]).isoformat()
    # Revalidate current visibility/freshness independently of calculation time.
    verify_coinbase_source(source, decision_at=generated, now=decision)
    comparator = target["comparator"]
    if comparator in ("ABOVE", "BELOW", "AT_OR_ABOVE", "AT_OR_BELOW"):
        if target["lower"] is not None or target["upper"] is not None:
            raise ValueError("BTC_CONFLICTING_STRIKES")
        threshold, lower, upper = _positive(target["threshold"]), None, None
    elif comparator == "RANGE":
        if target["threshold"] is not None:
            raise ValueError("BTC_CONFLICTING_STRIKES")
        threshold, lower, upper = None, _positive(target["lower"]), _positive(target["upper"])
        if upper <= lower:
            raise ValueError("BTC_INVALID_RANGE")
    else:
        raise ValueError("BTC_UNSUPPORTED_COMPARATOR")
    if rule_original is None:
        if target["rule_sha256"] is not None:
            raise ValueError("BTC_RULE_ORIGINAL_REQUIRED")
    elif (
        not isinstance(rule_original, bytes)
        or not 0 < len(rule_original) <= 2_000_000
        or hashlib.sha256(rule_original).hexdigest() != target["rule_sha256"]
    ):
        raise ValueError("BTC_RULE_ORIGINAL_BINDING")
    calculated = prepare_btc_inputs(
        source, horizon_end_at=target["observation_at"], computed_at=generated
    )
    probability = threshold_probability(
        calculated.distribution,
        comparator=comparator,
        threshold=threshold,
        lower=lower,
        upper=upper,
    )
    if probability is None or not math.isfinite(probability):
        raise ValueError("BTC_DISTRIBUTION_UNAVAILABLE")
    specification = model_specification()
    return dict(
        schema="btc-conditional-proxy-v1",
        model=specification,
        execution_entrypoint=MODEL_ENTRYPOINT,
        horizon_role="DECLARED_OBSERVATION_TIME_UNVERIFIED",
        model_spec_sha256=_hash(specification),
        target=dict(target),
        target_sha256=_hash(target),
        probability=str(probability),
        generated_at=generated.isoformat(),
        decision_at=decision.isoformat(),
        source_hashes=[_hash(source)],
        coinbase_input_sha256=calculated.verified["input_sha256"],
        feature_record=coinbase_feature_record(source, decision_at=generated, now=decision),
        computed_features=calculated.features,
        distribution_inputs=asdict(calculated.distribution),
        scope="CONDITIONAL_ANALYTICAL_PROXY_NOT_CONTRACT_FORECAST",
        blockers=[
            "PAYOFF_TIME_AND_RULE_SEMANTICS_UNVERIFIED",
            "CF_PAYOFF_PROXY_CALIBRATION_MISSING",
            "MODEL_CODE_FREEZE_REQUIRED",
            "MODEL_RELEASE_REQUIRED",
            "FEE_POLICY_REQUIRED",
        ],
        execution_authority=False,
    )
