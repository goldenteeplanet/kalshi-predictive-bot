from __future__ import annotations

from datetime import UTC, timedelta
from decimal import Decimal
from typing import Any

from kalshi_predictor.active_universe import is_active_market_status, is_inactive_market_status
from kalshi_predictor.config import Settings
from kalshi_predictor.crypto.ticker_windows import crypto_ticker_close_time_utc
from kalshi_predictor.data.schema import Market, MarketRanking
from kalshi_predictor.utils.decimals import decimal_to_str, to_decimal
from kalshi_predictor.utils.time import parse_datetime, utc_now

CURRENT_WINDOW = "CURRENT_WINDOW"
EXPIRED_WINDOW_EXCLUDED = "EXPIRED_WINDOW_EXCLUDED"
MARKET_CLOSED_OR_SETTLED = "MARKET_CLOSED_OR_SETTLED"
MARKET_CLOSE_TOO_NEAR = "MARKET_CLOSE_TOO_NEAR"
MARKET_NOT_IN_CATALOG = "MARKET_NOT_IN_CATALOG"
MARKET_NOT_OPEN = "MARKET_NOT_OPEN"


def current_market_window_status(
    market: Market | None,
    *,
    settings: Settings,
    ranking: MarketRanking | None = None,
    now: Any | None = None,
) -> dict[str, Any]:
    """Classify whether an exact market is current enough for paper entry."""

    resolved_now = _aware_utc(now) or utc_now()
    if market is None:
        return _status_payload(
            MARKET_NOT_IN_CATALOG,
            now=resolved_now,
            current=False,
            diagnostic_only=True,
            reason="Exact market row is missing from the local catalog.",
        )

    lifecycle = str(getattr(market, "status", "") or "").strip()
    close_time = _market_close_time(market)
    expected_expiration_time = _aware_utc(getattr(market, "expected_expiration_time", None))
    expiration_time = _aware_utc(getattr(market, "expiration_time", None))
    settlement_time = _aware_utc(getattr(market, "settlement_ts", None))
    final_entry_cutoff_time = _final_entry_cutoff(close_time, settings)
    minutes_to_close = (
        Decimal(str((close_time - resolved_now).total_seconds())) / Decimal("60")
        if close_time is not None
        else None
    )

    base = {
        "lifecycle_status": lifecycle or None,
        "market_close_time": close_time.isoformat() if close_time else None,
        "expected_expiration_time": (
            expected_expiration_time.isoformat() if expected_expiration_time else None
        ),
        "expiration_time": expiration_time.isoformat() if expiration_time else None,
        "settlement_ts": settlement_time.isoformat() if settlement_time else None,
        "final_entry_cutoff_time": (
            final_entry_cutoff_time.isoformat() if final_entry_cutoff_time else None
        ),
        "minutes_to_close": decimal_to_str(minutes_to_close),
    }

    if _lifecycle_closed(lifecycle) or settlement_time is not None or getattr(market, "result", None):
        return _status_payload(
            MARKET_CLOSED_OR_SETTLED,
            now=resolved_now,
            current=False,
            diagnostic_only=True,
            reason="Market lifecycle is closed, finalized, settled, expired, or already has a result.",
            **base,
        )
    if close_time is not None and close_time <= resolved_now:
        return _status_payload(
            EXPIRED_WINDOW_EXCLUDED,
            now=resolved_now,
            current=False,
            diagnostic_only=True,
            reason="Market close_time is in the past.",
            **base,
        )
    if expected_expiration_time is not None and expected_expiration_time <= resolved_now:
        return _status_payload(
            EXPIRED_WINDOW_EXCLUDED,
            now=resolved_now,
            current=False,
            diagnostic_only=True,
            reason="Market expected_expiration_time is in the past.",
            **base,
        )
    if expiration_time is not None and expiration_time <= resolved_now:
        return _status_payload(
            EXPIRED_WINDOW_EXCLUDED,
            now=resolved_now,
            current=False,
            diagnostic_only=True,
            reason="Market expiration_time is in the past.",
            **base,
        )
    if final_entry_cutoff_time is not None and final_entry_cutoff_time <= resolved_now:
        return _status_payload(
            MARKET_CLOSE_TOO_NEAR,
            now=resolved_now,
            current=False,
            diagnostic_only=False,
            reason="Market is inside the configured final paper-entry cutoff.",
            **base,
        )
    if ranking is not None:
        ranking_minutes = to_decimal(getattr(ranking, "time_to_close_minutes", None))
        if (
            ranking_minutes is not None
            and ranking_minutes < settings.opportunity_min_time_to_close_minutes
        ):
            return _status_payload(
                MARKET_CLOSE_TOO_NEAR,
                now=resolved_now,
                current=False,
                diagnostic_only=False,
                reason="Ranking time_to_close_minutes is inside the configured final paper-entry cutoff.",
                **base,
            )
    if lifecycle and not is_active_market_status(lifecycle):
        return _status_payload(
            MARKET_NOT_OPEN,
            now=resolved_now,
            current=False,
            diagnostic_only=False,
            reason="Market lifecycle is not open, active, or trading.",
            **base,
        )
    return _status_payload(
        CURRENT_WINDOW,
        now=resolved_now,
        current=True,
        diagnostic_only=False,
        reason="Market is current and before the configured paper-entry cutoff.",
        **base,
    )


def _status_payload(
    status: str,
    *,
    now: Any,
    current: bool,
    diagnostic_only: bool,
    reason: str,
    **extra: Any,
) -> dict[str, Any]:
    return {
        "window_status": status,
        "current_window_status": status,
        "current_window_eligible": current,
        "current_positive_ev_eligible": current,
        "diagnostic_only": diagnostic_only,
        "expired_window_excluded": status == EXPIRED_WINDOW_EXCLUDED,
        "window_status_reason": reason,
        "now": now.isoformat() if hasattr(now, "isoformat") else str(now),
        **extra,
    }


def _market_close_time(market: Market) -> Any | None:
    close_time = _aware_utc(getattr(market, "close_time", None))
    if close_time is not None:
        return close_time
    parsed = crypto_ticker_close_time_utc(str(getattr(market, "ticker", "") or ""))
    return _aware_utc(parsed)


def _final_entry_cutoff(close_time: Any | None, settings: Settings) -> Any | None:
    if close_time is None:
        return None
    minutes = to_decimal(settings.opportunity_min_time_to_close_minutes) or Decimal("0")
    return close_time - timedelta(minutes=float(minutes))


def _aware_utc(value: Any) -> Any | None:
    if value is None:
        return None
    dt = value if hasattr(value, "astimezone") else parse_datetime(value)
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _lifecycle_closed(status: str) -> bool:
    lowered = status.lower()
    if any(token in lowered for token in ("closed", "settled", "final", "expired", "resolved")):
        return True
    return is_inactive_market_status(status)
