from __future__ import annotations

import json
import time
from datetime import datetime, time as clock_time, timezone
from pathlib import Path
from typing import Any, Callable

from kalshi_predictor.config import Settings
from kalshi_predictor.phase_gh1f import run_gh1f_monitor
from kalshi_predictor.utils.time import utc_now

MonitorFn = Callable[..., Path]


def run_gh1g_census(
    *,
    settings: Settings,
    output_dir: Path,
    series: list[str],
    windows_utc: list[str],
    poll_cycles: int,
    poll_interval_seconds: float,
    max_markets_per_series: int,
    max_quoted_per_category: int,
    stream_max_seconds: float,
    now_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    monitor_fn: MonitorFn = run_gh1f_monitor,
) -> Path:
    parsed_windows = [_parse_window(value) for value in windows_utc]
    output_dir.mkdir(parents=True, exist_ok=True)
    cycles: list[dict[str, Any]] = []
    certification_report: str | None = None
    for cycle in range(1, poll_cycles + 1):
        now = now_fn().astimezone(timezone.utc)
        active_window = _active_window(now, parsed_windows, windows_utc)
        row: dict[str, Any] = {
            "cycle": cycle,
            "checked_at": now.isoformat(),
            "active_window": active_window,
            "status": "OUTSIDE_ACTIVE_WINDOW" if active_window is None else "SCANNED",
            "comparison_triggered": False,
        }
        if active_window is not None:
            cycle_dir = output_dir / f"cycle_{cycle:03d}"
            monitor_path = monitor_fn(
                settings=settings,
                output_dir=cycle_dir,
                series=series,
                cycles=1,
                interval_seconds=0,
                max_markets_per_series=max_markets_per_series,
                max_quoted_per_category=max_quoted_per_category,
                stream_max_seconds=stream_max_seconds,
            )
            monitor = json.loads(monitor_path.read_text(encoding="utf-8"))
            row["monitor_report"] = str(monitor_path)
            row["comparisons_triggered"] = monitor.get("comparisons_triggered", 0)
            row["comparison_triggered"] = bool(monitor.get("comparisons_triggered"))
            if row["comparison_triggered"]:
                certification_report = str(monitor_path)
                cycles.append(row)
                break
        cycles.append(row)
        if cycle < poll_cycles and poll_interval_seconds > 0:
            time.sleep(poll_interval_seconds)
    report = {
        "phase": "GH-1G",
        "generated_at": utc_now().isoformat(),
        "mode": "READ_ONLY_TIME_WINDOWED_CENSUS",
        "execution_enabled": False,
        "database_writes": 0,
        "orders_submitted": 0,
        "series": series,
        "windows_utc": windows_utc,
        "poll_cycles_requested": poll_cycles,
        "poll_cycles_completed": len(cycles),
        "certification_triggered": certification_report is not None,
        "certification_report": certification_report,
        "cycle_results": cycles,
    }
    path = output_dir / "gh1g_time_windowed_liquidity_census.json"
    path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    return path


def _parse_window(value: str) -> tuple[clock_time, clock_time]:
    try:
        start_raw, end_raw = value.split("-", 1)
        return clock_time.fromisoformat(start_raw), clock_time.fromisoformat(end_raw)
    except ValueError as exc:
        raise ValueError(f"Invalid UTC window {value!r}; expected HH:MM-HH:MM.") from exc


def _active_window(
    now: datetime,
    parsed: list[tuple[clock_time, clock_time]],
    labels: list[str],
) -> str | None:
    current = now.time().replace(tzinfo=None)
    for label, (start, end) in zip(labels, parsed, strict=True):
        inside = start <= current <= end if start <= end else current >= start or current <= end
        if inside:
            return label
    return None
