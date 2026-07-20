from __future__ import annotations

import json
from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import Any

from kalshi_predictor.utils.time import utc_now

MIN_WINDOWS = 3


def write_multi_window_report(*, reports_dir: Path, output_dir: Path) -> Path:
    reports = _load_reports(reports_dir)
    windows: dict[str, dict[str, Any]] = {}
    positive_rows: list[dict[str, Any]] = []
    for source_path, payload in reports:
        window_id = _window_id(payload)
        if not window_id:
            continue
        evaluations = payload.get("immediate_evaluations", [])
        positive = [row for row in evaluations if Decimal(str(row.get("executable_edge") or "0")) > 0]
        windows[window_id] = {
            "window_id": window_id,
            "source_path": str(source_path),
            "evaluated": len(evaluations),
            "positive_edge": len(positive),
            "advanced": sum(bool(row.get("advance")) for row in evaluations),
            "minimum_time_to_close_minutes": min(
                (Decimal(str(row["time_to_close_minutes"])) for row in evaluations
                 if row.get("time_to_close_minutes") is not None), default=None,
            ),
        }
        for row in positive:
            blockers = list(row.get("blockers") or [])
            positive_rows.append({
                "window_id": window_id, "ticker": row.get("ticker"),
                "model_name": row.get("model_name"), "executable_edge": row.get("executable_edge"),
                "opportunity_score": row.get("opportunity_score"),
                "liquidity_score": row.get("liquidity_score"), "spread": row.get("spread"),
                "time_to_close_minutes": row.get("time_to_close_minutes"),
                "blockers": blockers, "advance": bool(row.get("advance")),
                "remaining_blocker_count": len(blockers),
            })
    blocker_counts = Counter(blocker for row in positive_rows for blocker in row["blockers"])
    advanced = [row for row in positive_rows if row["advance"]]
    near_misses = sorted(
        (row for row in positive_rows if not row["advance"]),
        key=lambda row: (row["remaining_blocker_count"], -Decimal(str(row["executable_edge"]))),
    )
    ordered_windows = [windows[key] for key in sorted(windows)]
    report = {
        "phase": "GH-1V", "generated_at": utc_now().isoformat(),
        "mode": "READ_ONLY_MULTI_WINDOW_NEAR_MISS_ATTRIBUTION",
        "database_writes": 0, "execution_enabled": False, "thresholds_changed": False,
        "windows": ordered_windows, "positive_edge_rows": positive_rows,
        "near_misses": near_misses, "advanced_candidates": advanced,
        "summary": {
            "distinct_windows": len(ordered_windows), "minimum_windows": MIN_WINDOWS,
            "multi_window_complete": len(ordered_windows) >= MIN_WINDOWS,
            "evaluated": sum(row["evaluated"] for row in ordered_windows),
            "positive_edge": len(positive_rows), "advanced": len(advanced),
            "positive_edge_blocker_counts": dict(sorted(blocker_counts.items())),
            "execution_remains_disabled": True,
        },
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "gh1v_fresh_near_miss_multi_window_watch.json"
    path.write_text(json.dumps(report, indent=2, default=str, sort_keys=True), encoding="utf-8")
    return path


def _load_reports(reports_dir: Path) -> list[tuple[Path, dict[str, Any]]]:
    rows = []
    for path in sorted(reports_dir.glob("phase_gh1u*/gh1u_lead_time_atomic_activation.json")):
        try:
            rows.append((path, json.loads(path.read_text(encoding="utf-8"))))
        except (OSError, json.JSONDecodeError):
            continue
    return rows


def _window_id(payload: dict[str, Any]) -> str | None:
    tickers = payload.get("pinned_tickers", {}).get("weather_v2", [])
    if not tickers:
        tickers = payload.get("pinned_tickers", {}).get("crypto_v2", [])
    ticker = str(tickers[0]) if tickers else ""
    return ticker.rsplit("-", 1)[0] if "-" in ticker else None
