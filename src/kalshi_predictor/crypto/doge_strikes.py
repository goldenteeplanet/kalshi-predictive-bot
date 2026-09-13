"""Exact DOGE custom-strike metadata; no CF benchmark or trading certification."""

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from kalshi_predictor.microstructure.provenance import aware, bound_close_time

_NUMBER = r"[0-9]+\.[0-9]{7}"
_RULE = re.compile(
    r"If there is a 60 second average of CF Benchmarks' Dogecoin Real-Time Index "
    r"\(DOGEUSD_RTI\) before (?P<hour>1[0-2]|[1-9]) (?P<ampm>AM|PM) (?P<zone>EDT|EST) "
    r"is (?P<operator>above|below|between) (?P<bounds>" + _NUMBER + r"(?:-" + _NUMBER + r")?) "
    r"at (?P=hour) (?P=ampm) (?P=zone) on (?P<month>[A-Z][a-z]{2}) "
    r"(?P<day>[1-9]|[12][0-9]|3[01]), (?P<year>[0-9]{4}), then the market resolves to Yes\."
)
_MONTHS = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()


@dataclass(frozen=True)
class DogeStrike:
    ticker: str
    event_ticker: str
    operator: str
    floor: Decimal | None
    cap: Decimal | None
    close_time: datetime
    primary_rule: str
    primary_rule_sha256: str
    metadata_sha256: str
    benchmark: str = "DOGEUSD_RTI_60_SECOND_AVERAGE_NOT_RECONSTRUCTED"


def parse_doge_strike(market: dict, *, cutoff: datetime) -> DogeStrike:
    """Validate exact captured grammar; range semantics remain unsupported for binding."""
    cutoff = aware(cutoff)
    close = bound_close_time(market)
    if market.get("market_type") != "binary" or market.get("status") not in {"open", "active"}:
        raise ValueError("DOGE_ACTIVE_BINARY_REQUIRED")
    if close <= cutoff:
        raise ValueError("DOGE_TARGET_NOT_FUTURE")
    local = close.astimezone(ZoneInfo("America/New_York"))
    event = (
        f"KXDOGE-{local.year % 100:02d}{_MONTHS[local.month - 1].upper()}"
        f"{local.day:02d}{local.hour:02d}"
    )
    ticker = market.get("ticker")
    if (
        market.get("event_ticker") != event
        or not isinstance(ticker, str)
        or not ticker.startswith(event + "-")
        or not re.fullmatch(r"[A-Z0-9.-]+", ticker)
        or market.get("series_ticker", "KXDOGE") != "KXDOGE"
        or market.get("strike_type") != "custom"
    ):
        raise ValueError("DOGE_EXACT_EVENT_IDENTITY_REQUIRED")
    custom = market.get("custom_strike")
    if type(custom) is not dict or set(custom) != {"strike_type", "floor_strike", "cap_strike"}:
        raise ValueError("DOGE_CUSTOM_SCHEMA_REQUIRED")
    if any(type(value) is not str for value in custom.values()):
        raise ValueError("DOGE_STRING_STRIKES_REQUIRED")
    operator = custom["strike_type"]
    if operator not in {"greater", "less", "between"}:
        raise ValueError("DOGE_UNKNOWN_OPERATOR")
    values = {}
    for key in ("floor_strike", "cap_strike"):
        raw = custom[key]
        if raw and not re.fullmatch(_NUMBER, raw):
            raise ValueError("DOGE_EXACT_DECIMAL_REQUIRED")
        value = Decimal(raw) if raw else None
        if value is not None and (not value.is_finite() or value <= 0):
            raise ValueError("DOGE_POSITIVE_FINITE_STRIKE_REQUIRED")
        # Captured custom schema carries null top-level bounds. Do not resolve ambiguity.
        if market.get(key) is not None:
            raise ValueError("DOGE_CONTRADICTORY_TOP_LEVEL_STRIKE")
        values[key] = value
    floor, cap = values["floor_strike"], values["cap_strike"]
    if not (
        operator == "greater"
        and floor is not None
        and cap is None
        or operator == "less"
        and floor is None
        and cap is not None
        or operator == "between"
        and floor is not None
        and cap is not None
        and floor < cap
    ):
        raise ValueError("DOGE_OPERATOR_BOUND_CONFLICT")
    rule = market.get("rules_primary")
    if not isinstance(rule, str):
        raise ValueError("DOGE_EXACT_PRIMARY_RULE_REQUIRED")
    match = _RULE.fullmatch(rule)
    if match is None:
        raise ValueError("DOGE_EXACT_PRIMARY_RULE_REQUIRED")
    expected_bounds = (
        custom["floor_strike"] + "-" + custom["cap_strike"]
        if operator == "between"
        else custom["floor_strike"] or custom["cap_strike"]
    )
    hour = int(match["hour"]) % 12 + (12 if match["ampm"] == "PM" else 0)
    if (
        match["operator"] != {"greater": "above", "less": "below", "between": "between"}[operator]
        or match["bounds"] != expected_bounds
        or match["month"] != _MONTHS[local.month - 1]
        or int(match["day"]) != local.day
        or int(match["year"]) != local.year
        or hour != local.hour
        or match["zone"] != local.tzname()
        or local.minute
        or local.second
        or local.microsecond
    ):
        raise ValueError("DOGE_RULE_METADATA_MISMATCH")
    encoded = json.dumps(market, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return DogeStrike(
        ticker,
        event,
        operator,
        floor,
        cap,
        close,
        rule,
        hashlib.sha256(rule.encode()).hexdigest(),
        hashlib.sha256(encoded).hexdigest(),
    )
