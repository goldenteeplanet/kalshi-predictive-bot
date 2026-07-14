from sqlalchemy.orm import Session

from kalshi_predictor.data.repositories import decode_json
from kalshi_predictor.data.schema import MarketSnapshot
from kalshi_predictor.forecasting.base import ForecastInput, ForecastOutput
from kalshi_predictor.forecasting.market_implied import MarketImpliedForecaster


class MarketImpliedSnapshotForecaster:
    model_name = "market_implied_v1"

    def __init__(self) -> None:
        self._forecaster = MarketImpliedForecaster()

    def forecast(self, session: Session, snapshot: MarketSnapshot) -> ForecastOutput | None:
        del session
        return self._forecaster.forecast(
            ForecastInput(
                ticker=snapshot.ticker,
                captured_at=snapshot.captured_at,
                market_json=decode_json(snapshot.raw_market_json),
                orderbook_json=decode_json(snapshot.raw_orderbook_json),
            )
        )

