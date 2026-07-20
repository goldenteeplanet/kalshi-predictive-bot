"""Scheduled entry point for the NYC-W9 exact live-window feed."""

from pathlib import Path

from kalshi_predictor.config import get_settings
from kalshi_predictor.phase_nyc_w9 import run_nyc_w9_cycle


settings = get_settings()
print(run_nyc_w9_cycle(
    reports_dir=Path("reports"), output_dir=Path("reports/phase_nyc_w9"),
    user_agent=settings.kalshi_user_agent,
    max_adjustment=settings.weather_v2_max_adjustment,
))
