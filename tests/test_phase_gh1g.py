import json
from datetime import datetime, timezone
from pathlib import Path

from kalshi_predictor.config import Settings
from kalshi_predictor.phase_gh1g import run_gh1g_census


def test_gh1g_outside_window_never_calls_monitor(tmp_path: Path) -> None:
    def forbidden_monitor(**_kwargs):
        raise AssertionError("monitor must not run outside an active window")

    path = run_gh1g_census(
        settings=Settings(), output_dir=tmp_path, series=["KXBTC"],
        windows_utc=["13:00-14:00"], poll_cycles=1, poll_interval_seconds=0,
        max_markets_per_series=1, max_quoted_per_category=1, stream_max_seconds=1,
        now_fn=lambda: datetime(2026, 7, 15, 15, 0, tzinfo=timezone.utc),
        monitor_fn=forbidden_monitor,
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["cycle_results"][0]["status"] == "OUTSIDE_ACTIVE_WINDOW"
    assert payload["database_writes"] == 0


def test_gh1g_stops_after_first_certification(tmp_path: Path) -> None:
    def certified_monitor(**kwargs):
        path = Path(kwargs["output_dir"]) / "gh1f_demo_quote_monitor.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"comparisons_triggered": 1}), encoding="utf-8")
        return path

    path = run_gh1g_census(
        settings=Settings(), output_dir=tmp_path, series=["KXBTC"],
        windows_utc=["00:00-23:59"], poll_cycles=3, poll_interval_seconds=0,
        max_markets_per_series=1, max_quoted_per_category=1, stream_max_seconds=1,
        now_fn=lambda: datetime(2026, 7, 15, 15, 0, tzinfo=timezone.utc),
        monitor_fn=certified_monitor,
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["certification_triggered"] is True
    assert payload["poll_cycles_completed"] == 1
