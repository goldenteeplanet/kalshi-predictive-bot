from kalshi_predictor.forecasting.sports_v1 import SportsV1Forecaster


class NHLV1Forecaster(SportsV1Forecaster):
    model_name = "nhl_v1"

    def __init__(self, settings=None) -> None:
        super().__init__(
            settings=settings,
            league="NHL",
            model_name=self.model_name,
            max_adjustment_field="nhl_v1_max_adjustment",
        )

