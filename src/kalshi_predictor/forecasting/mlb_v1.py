from kalshi_predictor.forecasting.sports_v1 import SportsV1Forecaster


class MLBV1Forecaster(SportsV1Forecaster):
    model_name = "mlb_v1"

    def __init__(self, settings=None) -> None:
        super().__init__(
            settings=settings,
            league="MLB",
            model_name=self.model_name,
            max_adjustment_field="mlb_v1_max_adjustment",
        )

