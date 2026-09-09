"""Bounded request originals retain count, clock, shape and exact input guards."""

from datetime import timedelta

import pytest
from test_coinbase_source_semantics import NOW, mutate_original, source

from kalshi_predictor.overnight_paper import crypto_source as cs


def test_request_is_closed_utc_minute_grid_and_cannot_expand_resource_limit():
    params = cs.coinbase_candle_window(NOW + timedelta(seconds=29), minutes=180)
    assert params == {
        "granularity": 60,
        "start": (NOW - timedelta(minutes=180)).isoformat(),
        "end": NOW.isoformat(),
    }
    for minutes in (True, 0, 2, 301, 3.0):
        with pytest.raises(ValueError, match="WINDOW_MINUTES"):
            cs.coinbase_candle_window(NOW, minutes=minutes)


def test_range_selects_only_in_window_originals_and_binds_request_identity():
    value = source()
    original = cs.verify_coinbase_source(value, decision_at=NOW, now=NOW)
    value["body"]["candles"]["params"] = cs.coinbase_candle_window(NOW, minutes=3)
    mutate_original(
        value,
        "candles",
        lambda rows: rows + [[int(NOW.timestamp()) - 240, 69999, 70001, 70000, 70000, 2]],
    )
    checked = cs.verify_coinbase_source(value, decision_at=NOW, now=NOW)
    assert checked["inputs"]["closed_candles"] == original["inputs"]["closed_candles"]
    assert checked["inputs"]["excluded_outside_request_row_indices"] == [3]
    assert checked["inputs"]["requested_end_at"] == NOW.isoformat()
    assert checked["input_sha256"] != original["input_sha256"]
    mutate_original(value, "candles", lambda rows: rows[:-1] + [rows[-1][:4] + [80000, 2]])
    with pytest.raises(ValueError, match="CANDLE_OHLC"):
        cs.verify_coinbase_source(value, decision_at=NOW, now=NOW)


@pytest.mark.parametrize(
    "params",
    [
        {"granularity": 60.0},
        {"granularity": 60, "start": NOW.isoformat()},
        {
            "granularity": 60,
            "start": (NOW - timedelta(minutes=301)).isoformat(),
            "end": NOW.isoformat(),
        },
        {
            "granularity": 60,
            "start": (NOW - timedelta(minutes=3)).isoformat(),
            "end": (NOW + timedelta(minutes=1)).isoformat(),
        },
        {
            "granularity": 60,
            "start": (NOW - timedelta(minutes=3, seconds=1)).isoformat(),
            "end": NOW.isoformat(),
        },
        {"granularity": 60, "start": "2026-09-08T00:57:00", "end": NOW.isoformat()},
    ],
)
def test_invalid_ranges_fail(params):
    value = source()
    value["body"]["candles"]["params"] = params
    with pytest.raises(ValueError):
        cs.verify_coinbase_source(value, decision_at=NOW, now=NOW)


def test_bounded_request_does_not_accept_350_response_rows():
    value = source()
    value["body"]["candles"]["params"] = cs.coinbase_candle_window(NOW)
    mutate_original(value, "candles", lambda rows: [rows[0]] * 350)
    with pytest.raises(ValueError, match="CANDLE_COUNT"):
        cs.verify_coinbase_source(value, decision_at=NOW, now=NOW)
