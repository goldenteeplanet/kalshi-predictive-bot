from decimal import Decimal
from typing import Any

from kalshi_predictor.utils.decimals import to_decimal


def explain_model(
    model_name: str | None,
    *,
    forecast_probability: Any = None,
    feature_json: dict[str, Any] | None = None,
) -> str:
    name = model_name or "unknown model"
    probability = _percent(forecast_probability)
    features = feature_json or {}

    if name == "ensemble_v2":
        components = _component_names(features)
        component_text = ", ".join(components) if components else "available component models"
        if probability:
            return (
                f"ensemble_v2 is combining {component_text}. "
                f"The current weighted forecast is {probability}."
            )
        return f"ensemble_v2 is combining {component_text} when stored component forecasts exist."

    if name == "market_implied_v1":
        if probability:
            return (
                f"market_implied_v1 reads the stored market price as the baseline. "
                f"The current forecast is {probability}."
            )
        return "market_implied_v1 uses stored market prices as a baseline forecast."

    if name == "crypto_v2":
        return "crypto_v2 starts from market prices and applies bounded crypto feature adjustments."

    if name == "weather_v2":
        return (
            "weather_v2 starts from market prices and applies bounded weather feature "
            "adjustments."
        )

    if name == "economic_v1":
        return "economic_v1 uses stored economic feature context when it is available."

    return f"{name} is a local stored forecast model used for simulated decision review."


def _component_names(feature_json: dict[str, Any]) -> list[str]:
    components = feature_json.get("component_forecasts") or feature_json.get(
        "component_model_probabilities"
    )
    if isinstance(components, dict):
        return [str(key) for key in components.keys()]
    return []


def _percent(value: Any) -> str | None:
    decimal_value = to_decimal(value)
    if decimal_value is None:
        return None
    return f"{(decimal_value * Decimal('100')).quantize(Decimal('0.1'))}%"
