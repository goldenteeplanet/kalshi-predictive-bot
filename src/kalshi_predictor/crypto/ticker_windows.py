from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

CRYPTO_TICKER_CLOSE_RE = re.compile(
    r"-(?P<year>\d{2})(?P<month>JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)"
    r"(?P<day>\d{2})(?P<hour>\d{2})(?:-|$)"
)

MONTHS_BY_CODE = {
    "JAN": 1,
    "FEB": 2,
    "MAR": 3,
    "APR": 4,
    "MAY": 5,
    "JUN": 6,
    "JUL": 7,
    "AUG": 8,
    "SEP": 9,
    "OCT": 10,
    "NOV": 11,
    "DEC": 12,
}


def crypto_ticker_close_time_utc(ticker: Any) -> datetime | None:
    """Return a Kalshi crypto ticker close hour parsed as UTC when encoded."""
    match = CRYPTO_TICKER_CLOSE_RE.search(str(ticker or "").upper())
    if match is None:
        return None
    month = MONTHS_BY_CODE.get(match.group("month"))
    if month is None:
        return None
    try:
        close_time = datetime(
            2000 + int(match.group("year")),
            month,
            int(match.group("day")),
            int(match.group("hour")),
            tzinfo=ZoneInfo("America/New_York"),
        )
    except ValueError:
        return None
    return close_time.astimezone(UTC)


def crypto_ticker_window_is_expired(ticker: Any, *, now: datetime) -> bool:
    close_time = crypto_ticker_close_time_utc(ticker)
    return close_time is not None and close_time <= now
