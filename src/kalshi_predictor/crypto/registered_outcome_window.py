"""One fixed first-attempt window, bound to the pre-capture protocol hash."""

from datetime import datetime, timedelta


def outcome_window(plan: dict) -> tuple[datetime, datetime]:
    """Legacy plans retain ten minutes; future plans can explicitly defer finality.

    This is not a retry policy or a prediction of when the exchange finalizes.
    The request budget, finality checks and one-minute attempt duration are fixed.
    """
    delay = plan.get("outcome_delay_minutes", 10)
    if type(delay) is not int or not 10 <= delay <= 180:
        raise ValueError("REGISTERED_OUTCOME_DELAY_REQUIRED")
    target = datetime.fromisoformat(plan["target_at"].replace("Z", "+00:00"))
    if target.utcoffset() is None:
        raise ValueError("AWARE_OUTCOME_TARGET_REQUIRED")
    start = target + timedelta(minutes=delay)
    return start, start + timedelta(minutes=1)
