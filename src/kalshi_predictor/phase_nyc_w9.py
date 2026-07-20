"""NYC-W9 exact live-window certification producer for NYC-W8."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx

from kalshi_predictor.phase_gh1h import PRODUCTION_PUBLIC_REST_URL
from kalshi_predictor.phase_nyc_w3 import write_nyc_w3_report
from kalshi_predictor.phase_nyc_w4 import write_nyc_w4_report
from kalshi_predictor.phase_nyc_w7 import write_shadow_runtime_report
from kalshi_predictor.phase_nyc_w8 import write_nyc_w8_report
from kalshi_predictor.utils.time import utc_now
from kalshi_predictor.weather.temperature_contracts import parse_point_temperature_ticker


SERIES = "KXTEMPNYCH"
MIN_PIN_LEAD = timedelta(minutes=20)
TARGET_WINDOWS = 3
MAX_PIN_AGE_AFTER_TARGET = timedelta(hours=6)


class StateIntegrityError(RuntimeError):
    pass


def run_nyc_w9_cycle(
    *, reports_dir: Path, output_dir: Path, user_agent: str,
    max_adjustment: Decimal, market_limit: int = 100,
    now: datetime | None = None, kalshi_client: httpx.Client | None = None,
    nws_client: httpx.Client | None = None,
) -> Path:
    """Run one bounded pin-or-certify cycle; never writes application data."""
    try:
        return _run_nyc_w9_cycle(
            reports_dir=reports_dir, output_dir=output_dir, user_agent=user_agent,
            max_adjustment=max_adjustment, market_limit=market_limit, now=now,
            kalshi_client=kalshi_client, nws_client=nws_client,
        )
    except StateIntegrityError as exc:
        return _write_error_report(output_dir, "STATE_INTEGRITY_BLOCKED", exc)
    except (httpx.HTTPError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        return _write_error_report(output_dir, "EXTERNAL_DATA_RETRY", exc)


def _run_nyc_w9_cycle(
    *, reports_dir: Path, output_dir: Path, user_agent: str,
    max_adjustment: Decimal, market_limit: int = 100,
    now: datetime | None = None, kalshi_client: httpx.Client | None = None,
    nws_client: httpx.Client | None = None,
) -> Path:
    current = (now or utc_now()).astimezone(timezone.utc)
    output_dir.mkdir(parents=True, exist_ok=True)
    state_path = output_dir / "nyc_w9_state.json"
    state = _load_state(state_path)
    w8_path = write_nyc_w8_report(
        reports_dir=reports_dir, output_dir=reports_dir / "phase_nyc_w8"
    )
    w8 = json.loads(w8_path.read_text(encoding="utf-8"))
    if w8["summary"]["live_shadow_census_passed"]:
        return _write_cycle_report(output_dir, state, "COMPLETE", w8)

    owned = kalshi_client is None
    active_kalshi = kalshi_client or httpx.Client(
        base_url=PRODUCTION_PUBLIC_REST_URL, timeout=15.0
    )
    try:
        if not state.get("pinned_tickers"):
            tickers, target = _discover_next_ladder(
                active_kalshi, current, set(state.get("completed_windows", [])), market_limit
            )
            if not tickers:
                return _write_cycle_report(output_dir, state, "WAITING_FOR_UPCOMING_LADDER", w8)
            state["pinned_tickers"] = tickers
            state["pinned_target_utc_time"] = target.isoformat()
            state["pinned_at"] = current.isoformat()
            _write_state(state_path, state)
            return _write_cycle_report(output_dir, state, "PINNED_WAITING_FOR_TARGET", w8)

        target = datetime.fromisoformat(str(state["pinned_target_utc_time"]).replace("Z", "+00:00"))
        if current < target:
            return _write_cycle_report(output_dir, state, "PINNED_WAITING_FOR_TARGET", w8)
        if current > target + MAX_PIN_AGE_AFTER_TARGET:
            return _write_cycle_report(output_dir, state, "STALE_PIN_REQUIRES_REVIEW", w8)

        slug = target.strftime("%Y%m%dT%H%M%SZ")
        certification_dir = reports_dir / f"phase_nyc_w3b_w9_{slug}"
        certification_path = write_nyc_w3_report(
            output_dir=certification_dir, user_agent=user_agent,
            market_limit=len(state["pinned_tickers"]), tolerance_minutes=15,
            exact_tickers=list(state["pinned_tickers"]), kalshi_client=active_kalshi,
            nws_client=nws_client,
        )
        certification = json.loads(certification_path.read_text(encoding="utf-8"))
        summary = certification["summary"]
        if summary["alignment_passed"] != len(state["pinned_tickers"]):
            state["last_alignment_blockers"] = summary["alignment_blocker_counts"]
            _write_state(state_path, state)
            return _write_cycle_report(output_dir, state, "WAITING_FOR_EXACT_KNYC_OBSERVATION", w8)

        preview_dir = reports_dir / f"phase_nyc_w4_w9_{slug}"
        preview_path = write_nyc_w4_report(
            certification_path=certification_path, output_dir=preview_dir,
            max_adjustment=max_adjustment, market_limit=len(state["pinned_tickers"]),
            kalshi_client=active_kalshi,
        )
        preview = json.loads(preview_path.read_text(encoding="utf-8"))
        if preview["summary"]["rows_blocked"]:
            state["last_preview_blocked"] = preview["summary"]["rows_blocked"]
            _write_state(state_path, state)
            return _write_cycle_report(output_dir, state, "W4_PREVIEW_BLOCKED", w8)

        write_shadow_runtime_report(
            reports_dir=reports_dir, output_dir=reports_dir / f"phase_nyc_w7_live_{slug}",
            max_adjustment=max_adjustment, source_paths=[preview_path],
        )
        w8_path = write_nyc_w8_report(
            reports_dir=reports_dir, output_dir=reports_dir / "phase_nyc_w8"
        )
        w8 = json.loads(w8_path.read_text(encoding="utf-8"))
        completed = list(state.get("completed_windows", []))
        completed.append(str(state["pinned_target_utc_time"]))
        state["completed_windows"] = sorted(set(completed))
        state["pinned_tickers"] = []
        state["pinned_target_utc_time"] = None
        state["last_completed_at"] = current.isoformat()
        _write_state(state_path, state)
        status = "COMPLETE" if w8["summary"]["live_shadow_census_passed"] else "WINDOW_CERTIFIED"
        return _write_cycle_report(output_dir, state, status, w8)
    finally:
        if owned:
            active_kalshi.close()


def _discover_next_ladder(
    client: httpx.Client, now: datetime, completed: set[str], limit: int,
) -> tuple[list[str], datetime | None]:
    response = client.get("/markets", params={
        "limit": limit, "status": "open", "series_ticker": SERIES,
    })
    response.raise_for_status()
    groups: dict[datetime, list[str]] = {}
    for market in response.json().get("markets", []):
        ticker = str(market.get("ticker") or "")
        contract = parse_point_temperature_ticker(ticker)
        if contract is None or contract.target_utc_time < now + MIN_PIN_LEAD:
            continue
        target = contract.target_utc_time.astimezone(timezone.utc)
        if target.isoformat() in completed:
            continue
        groups.setdefault(target, []).append(ticker)
    if not groups:
        return [], None
    target = min(groups)
    return sorted(set(groups[target])), target


def _load_state(path: Path) -> dict[str, Any]:
    if path.exists():
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise StateIntegrityError("NYC-W9 state is unreadable; pinned state was not reset") from exc
        if not isinstance(state, dict) or not isinstance(state.get("pinned_tickers", []), list):
            raise StateIntegrityError("NYC-W9 state schema is invalid; pinned state was not reset")
        return state
    return {"completed_windows": [], "pinned_tickers": [], "pinned_target_utc_time": None}


def _write_state(path: Path, state: dict[str, Any]) -> None:
    _atomic_write_json(path, state)


def _write_cycle_report(
    output_dir: Path, state: dict[str, Any], status: str, w8: dict[str, Any]
) -> Path:
    report = {
        "phase": "NYC-W9", "generated_at": utc_now().isoformat(),
        "mode": "EXACT_LIVE_WINDOW_READ_ONLY_FEED", "status": status,
        "database_writes": 0, "thresholds_changed": False,
        "feature_flag_enabled": False, "execution_enabled": False,
        "pinned_target_utc_time": state.get("pinned_target_utc_time"),
        "pinned_tickers": state.get("pinned_tickers", []),
        "completed_windows": state.get("completed_windows", []),
        "w8_summary": w8.get("summary", {}),
    }
    path = output_dir / "nyc_w9_live_window_feed.json"
    _atomic_write_json(path, report)
    return path


def _write_error_report(output_dir: Path, status: str, exc: Exception) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    report = {
        "phase": "NYC-W9", "generated_at": utc_now().isoformat(),
        "mode": "EXACT_LIVE_WINDOW_READ_ONLY_FEED", "status": status,
        "error_type": type(exc).__name__, "error": str(exc),
        "state_reset": False, "database_writes": 0, "thresholds_changed": False,
        "feature_flag_enabled": False, "execution_enabled": False,
    }
    path = output_dir / "nyc_w9_live_window_feed.json"
    _atomic_write_json(path, report)
    return path


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(path)
