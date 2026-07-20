import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import httpx

from kalshi_predictor.phase_nyc_w9 import run_nyc_w9_cycle


TICKER = "KXTEMPNYCH-26JUL1618-T80.99"
MARKET = {
    "ticker": TICKER, "series_ticker": "KXTEMPNYCH",
    "event_ticker": "KXTEMPNYCH-26JUL1618", "status": "open",
    "strike_type": "greater", "floor_strike": 80.99, "cap_strike": None,
    "close_time": "2026-07-16T22:00:00Z",
    "rules_primary": "The Weather Company coordinates KNYC",
}


def test_nyc_w9_pins_then_waits_for_exact_observation(tmp_path: Path) -> None:
    def kalshi_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/markets":
            return httpx.Response(200, json={"markets": [MARKET]})
        return httpx.Response(200, json={"market": MARKET})

    def nws_handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"features": []})

    with (
        httpx.Client(transport=httpx.MockTransport(kalshi_handler), base_url="https://k") as kc,
        httpx.Client(transport=httpx.MockTransport(nws_handler), base_url="https://n") as nc,
    ):
        first = run_nyc_w9_cycle(
            reports_dir=tmp_path, output_dir=tmp_path / "phase_nyc_w9",
            user_agent="test@example.com", max_adjustment=Decimal("0.10"),
            now=datetime(2026, 7, 16, 21, 0, tzinfo=timezone.utc),
            kalshi_client=kc, nws_client=nc,
        )
        assert json.loads(first.read_text())["status"] == "PINNED_WAITING_FOR_TARGET"
        second = run_nyc_w9_cycle(
            reports_dir=tmp_path, output_dir=tmp_path / "phase_nyc_w9",
            user_agent="test@example.com", max_adjustment=Decimal("0.10"),
            now=datetime(2026, 7, 16, 22, 5, tzinfo=timezone.utc),
            kalshi_client=kc, nws_client=nc,
        )
    report = json.loads(second.read_text())
    assert report["status"] == "WAITING_FOR_EXACT_KNYC_OBSERVATION"
    assert report["pinned_tickers"] == [TICKER]
    assert report["database_writes"] == 0
    assert report["execution_enabled"] is False


def test_nyc_w9_retries_timeout_without_resetting_state(tmp_path: Path) -> None:
    output = tmp_path / "phase_nyc_w9"
    output.mkdir()
    state = {"completed_windows": [], "pinned_tickers": [], "pinned_target_utc_time": None}
    (output / "nyc_w9_state.json").write_text(json.dumps(state))

    def timeout(_: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("quiet API")

    with httpx.Client(transport=httpx.MockTransport(timeout), base_url="https://k") as client:
        path = run_nyc_w9_cycle(
            reports_dir=tmp_path, output_dir=output, user_agent="test@example.com",
            max_adjustment=Decimal("0.10"), kalshi_client=client,
        )
    assert json.loads(path.read_text())["status"] == "EXTERNAL_DATA_RETRY"
    assert json.loads((output / "nyc_w9_state.json").read_text()) == state


def test_nyc_w9_blocks_corrupt_state_instead_of_rolling_over(tmp_path: Path) -> None:
    output = tmp_path / "phase_nyc_w9"
    output.mkdir()
    (output / "nyc_w9_state.json").write_text("{broken")
    path = run_nyc_w9_cycle(
        reports_dir=tmp_path, output_dir=output, user_agent="test@example.com",
        max_adjustment=Decimal("0.10"),
    )
    report = json.loads(path.read_text())
    assert report["status"] == "STATE_INTEGRITY_BLOCKED"
    assert report["state_reset"] is False
    assert (output / "nyc_w9_state.json").read_text() == "{broken"


def test_nyc_w9_preserves_stale_pin_for_manual_review(tmp_path: Path) -> None:
    output = tmp_path / "phase_nyc_w9"
    output.mkdir()
    state = {
        "completed_windows": [], "pinned_tickers": [TICKER],
        "pinned_target_utc_time": "2026-07-16T22:00:00+00:00",
    }
    (output / "nyc_w9_state.json").write_text(json.dumps(state))
    path = run_nyc_w9_cycle(
        reports_dir=tmp_path, output_dir=output, user_agent="test@example.com",
        max_adjustment=Decimal("0.10"),
        now=datetime(2026, 7, 17, 5, 0, tzinfo=timezone.utc),
    )
    assert json.loads(path.read_text())["status"] == "STALE_PIN_REQUIRES_REVIEW"
    assert json.loads((output / "nyc_w9_state.json").read_text())["pinned_tickers"] == [TICKER]
