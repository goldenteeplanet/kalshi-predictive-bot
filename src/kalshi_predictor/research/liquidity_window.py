"""Time-to-close liquidity-window policy for sibling-vector capture."""

from __future__ import annotations

from datetime import datetime
from typing import Any

WINDOWS = (
    ("LE_1H", 0.0, 1.0),
    ("1_2H", 1.0, 2.0),
    ("2_4H", 2.0, 4.0),
    ("4_8H", 4.0, 8.0),
    ("8_24H", 8.0, 24.0),
    ("GT_24H", 24.0, None),
)


def window_for_hours(hours_to_close: float) -> str:
    for name, lower, upper in WINDOWS:
        if hours_to_close >= lower and (upper is None or hours_to_close <= upper):
            return name
    return "PAST_CLOSE"


def in_recommended_window(
    close_time: datetime, now: datetime, recommendation: dict[str, Any]
) -> bool:
    if close_time.tzinfo is None and now.tzinfo is not None:
        close_time = close_time.replace(tzinfo=now.tzinfo)
    elif now.tzinfo is None and close_time.tzinfo is not None:
        now = now.replace(tzinfo=close_time.tzinfo)
    hours = (close_time - now).total_seconds() / 3600.0
    lower = float(recommendation.get("lower_hours", 0.0))
    upper = recommendation.get("upper_hours")
    return hours >= lower and (upper is None or hours <= float(upper))
