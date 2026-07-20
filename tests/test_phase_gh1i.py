from kalshi_predictor.config import Settings
from kalshi_predictor.phase_gh1i import calibrate_two_sided_row


def test_gh1i_uses_current_thresholds_without_changing_them() -> None:
    settings = Settings(opportunity_max_spread="0.10")
    row = {
        "spread": "0.08", "yes_depth_5": "8", "no_depth_5": "10",
        "yes_buy_1_fully_executable": True,
    }
    result = calibrate_two_sided_row(row, settings=settings)
    assert result["calibration"]["ranking_advance"] is True
    assert result["calibration"]["risk_preferred_advance"] is False
    assert result["calibration"]["risk_executable_advance"] is True
    assert settings.opportunity_max_spread == __import__("decimal").Decimal("0.10")
