from decimal import Decimal
from typing import Any

from kalshi_predictor.forecasting.base import ForecastInput, ForecastOutput
from kalshi_predictor.kalshi.orderbook import parse_orderbook
from kalshi_predictor.utils.decimals import clamp_probability, midpoint, to_decimal


class MarketImpliedForecaster:
    model_name = "market_implied_v1"

    def forecast(self, forecast_input: ForecastInput) -> ForecastOutput | None:
        best_prices = parse_orderbook(forecast_input.orderbook_json)
        features: dict[str, Any] = {
            "ticker": forecast_input.ticker,
            "source": None,
            "best_yes_bid": _decimal_feature(best_prices.best_yes_bid),
            "best_yes_ask": _decimal_feature(best_prices.best_yes_ask),
            "market_yes_bid_dollars": forecast_input.market_json.get("yes_bid_dollars"),
            "market_yes_ask_dollars": forecast_input.market_json.get("yes_ask_dollars"),
            "last_price_dollars": forecast_input.market_json.get("last_price_dollars"),
        }

        market_mid_probability: Decimal | None = None
        yes_probability: Decimal | None = None
        source: str | None = None
        best_yes_bid: Decimal | None = best_prices.best_yes_bid
        best_yes_ask: Decimal | None = best_prices.best_yes_ask

        if best_yes_bid is not None and best_yes_ask is not None:
            yes_probability = midpoint(best_yes_bid, best_yes_ask)
            market_mid_probability = yes_probability
            source = "orderbook_midpoint"
        else:
            market_yes_bid = to_decimal(forecast_input.market_json.get("yes_bid_dollars"))
            market_yes_ask = to_decimal(forecast_input.market_json.get("yes_ask_dollars"))
            if market_yes_bid is not None and market_yes_ask is not None:
                yes_probability = midpoint(market_yes_bid, market_yes_ask)
                market_mid_probability = yes_probability
                best_yes_bid = market_yes_bid
                best_yes_ask = market_yes_ask
                source = "market_quote_midpoint"
            else:
                last_price = to_decimal(forecast_input.market_json.get("last_price_dollars"))
                if last_price is not None:
                    yes_probability = last_price
                    source = "last_price"

        if yes_probability is None:
            return None

        yes_probability = clamp_probability(yes_probability)
        if market_mid_probability is not None:
            market_mid_probability = clamp_probability(market_mid_probability)
        features["source"] = source
        features["yes_probability"] = _decimal_feature(yes_probability)
        features["market_mid_probability"] = _decimal_feature(market_mid_probability)

        return ForecastOutput(
            ticker=forecast_input.ticker,
            forecasted_at=forecast_input.captured_at,
            model_name=self.model_name,
            yes_probability=yes_probability,
            market_mid_probability=market_mid_probability,
            best_yes_bid=best_yes_bid,
            best_yes_ask=best_yes_ask,
            feature_json=features,
        )


def _decimal_feature(value: Decimal | None) -> str | None:
    return format(value, "f") if value is not None else None

