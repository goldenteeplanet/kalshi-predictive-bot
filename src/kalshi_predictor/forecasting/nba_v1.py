from kalshi_predictor.forecasting.sports_v1 import SportsV1Forecaster


class NBAV1Forecaster(SportsV1Forecaster):
    model_name = "nba_v1"

    def __init__(self, settings=None) -> None:
        super().__init__(
            settings=settings,
            league="NBA",
            model_name=self.model_name,
            max_adjustment_field="nba_v1_max_adjustment",
        )

