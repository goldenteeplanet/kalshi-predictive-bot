from kalshi_predictor.forecasting.sports_v1 import SportsV1Forecaster


class NFLV1Forecaster(SportsV1Forecaster):
    model_name = "nfl_v1"

    def __init__(self, settings=None) -> None:
        super().__init__(
            settings=settings,
            league="NFL",
            model_name=self.model_name,
            max_adjustment_field="nfl_v1_max_adjustment",
        )

