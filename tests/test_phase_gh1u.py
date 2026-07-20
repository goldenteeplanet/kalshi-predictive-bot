from datetime import timedelta

from kalshi_predictor.phase_gh1t import _has_required_lead_time
from kalshi_predictor.utils.time import utc_now


def test_gh1u_rejects_market_below_minimum_lead_time() -> None:
    market = {"close_time": (utc_now() + timedelta(minutes=4)).isoformat()}
    assert _has_required_lead_time(market, minimum_close_minutes=5) is False
