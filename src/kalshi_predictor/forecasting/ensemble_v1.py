from decimal import Decimal

from sqlalchemy.orm import Session

from kalshi_predictor.data.schema import MarketSnapshot
from kalshi_predictor.forecasting.base import ForecastOutput
from kalshi_predictor.forecasting.crypto_v1 import CryptoV1Forecaster
from kalshi_predictor.forecasting.economic_v1 import EconomicV1Forecaster
from kalshi_predictor.forecasting.registry_helpers import MarketImpliedSnapshotForecaster
from kalshi_predictor.forecasting.weather_v1 import WeatherV1Forecaster
from kalshi_predictor.utils.decimals import clamp_probability, to_decimal


class EnsembleV1Forecaster:
    model_name = "ensemble_v1"

    def __init__(self) -> None:
        self.component_models = (
            MarketImpliedSnapshotForecaster(),
            WeatherV1Forecaster(),
            CryptoV1Forecaster(),
            EconomicV1Forecaster(),
        )

    def forecast(self, session: Session, snapshot: MarketSnapshot) -> ForecastOutput | None:
        components: dict[str, str] = {}
        probabilities: list[Decimal] = []
        for model in self.component_models:
            forecast = model.forecast(session, snapshot)
            if forecast is None:
                continue
            probabilities.append(forecast.yes_probability)
            components[forecast.model_name] = str(forecast.yes_probability)

        if not probabilities:
            return None

        yes_probability = sum(probabilities, Decimal("0")) / Decimal(len(probabilities))
        return ForecastOutput(
            ticker=snapshot.ticker,
            forecasted_at=snapshot.captured_at,
            model_name=self.model_name,
            yes_probability=clamp_probability(yes_probability),
            market_mid_probability=None,
            best_yes_bid=to_decimal(snapshot.best_yes_bid),
            best_yes_ask=to_decimal(snapshot.best_yes_ask),
            feature_json={
                "source": "simple_average",
                "component_model_probabilities": components,
                "component_count": len(probabilities),
            },
            notes="Simple average of available component model probabilities.",
        )

