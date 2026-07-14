from __future__ import annotations

import csv
import json
import shlex
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.phase3bb_acceleration import (
    _metadata,
    _metadata_lines,
    _read_json,
    _safety_flags,
    _write_manifest,
)
from kalshi_predictor.phase3bb_r12_cloud_bootstrap import (
    CloudBootstrapTarget,
    ProbeRunner,
    RemoteProbe,
    RemoteProbeResult,
    _json_from_probe,
    _resolve_target,
    _result_payload,
    _run_ssh_probe,
)
from kalshi_predictor.phase3bb_r44_weather_catalog_hook_runtime_verification import (
    _first_line,
    _mark_executable,
    _stdout,
    _target_payload,
)
from kalshi_predictor.utils.time import utc_now

PHASE3BB_R53_VERSION = "phase3bb_r53_weather_current_window_cadence_v1"
DEFAULT_OUTPUT_DIR = Path("reports/phase3bb_r53")
DEFAULT_REPORTS_DIR = Path("reports")
DEFAULT_PER_PROBE_TIMEOUT_SECONDS = 60

WINDOW_ROW_FIELDS = [
    "ticker",
    "market_title",
    "status",
    "target_time",
    "minutes_until_target",
    "window_role",
    "has_link",
    "link_target_time",
    "link_target_matches_window",
    "has_snapshot",
    "snapshot_fresh",
    "latest_snapshot_at",
    "has_source_forecast",
    "source_forecast_fresh",
    "source_forecast_at",
    "has_weather_feature",
    "weather_feature_fresh",
    "weather_feature_at",
    "has_current_forecast",
    "latest_forecast_at",
    "has_current_ranking",
    "latest_ranking_at",
    "estimated_edge",
    "opportunity_score",
    "first_window_blocker",
]


@dataclass(frozen=True)
class Phase3BBR53WeatherCurrentWindowCadenceArtifacts:
    output_dir: Path
    executive_summary_path: Path
    markdown_path: Path
    json_path: Path
    rows_csv_path: Path
    checks_csv_path: Path
    probe_csv_path: Path
    operator_command_path: Path
    next_actions_path: Path
    manifest_path: Path


def write_phase3bb_r53_weather_current_window_cadence_report(
    session: Session,
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    reports_dir: Path = DEFAULT_REPORTS_DIR,
    settings: Settings | None = None,
    command_args: list[str] | None = None,
    ssh_target: str | None = None,
    identity_file: str | None = None,
    app_path: str | None = None,
    env_path: str | None = None,
    db_path: str | None = None,
    series_ticker: str = "KXTEMPNYCH",
    location_key: str = "new_york",
    fresh_window_hours: int = 24,
    match_tolerance_hours: int = 3,
    snapshot_fresh_minutes: int = 20,
    min_minutes_before_target: int = 10,
    limit: int = 500,
    per_probe_timeout_seconds: int = DEFAULT_PER_PROBE_TIMEOUT_SECONDS,
    probe_runner: ProbeRunner | None = None,
) -> Phase3BBR53WeatherCurrentWindowCadenceArtifacts:
    payload = build_phase3bb_r53_weather_current_window_cadence(
        session,
        output_dir=output_dir,
        reports_dir=reports_dir,
        settings=settings,
        command_args=command_args,
        ssh_target=ssh_target,
        identity_file=identity_file,
        app_path=app_path,
        env_path=env_path,
        db_path=db_path,
        series_ticker=series_ticker,
        location_key=location_key,
        fresh_window_hours=fresh_window_hours,
        match_tolerance_hours=match_tolerance_hours,
        snapshot_fresh_minutes=snapshot_fresh_minutes,
        min_minutes_before_target=min_minutes_before_target,
        limit=limit,
        per_probe_timeout_seconds=per_probe_timeout_seconds,
        probe_runner=probe_runner,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    executive_summary_path = output_dir / "EXECUTIVE_SUMMARY.md"
    markdown_path = output_dir / "weather_current_window_cadence.md"
    json_path = output_dir / "weather_current_window_cadence.json"
    rows_csv_path = output_dir / "window_rows.csv"
    checks_csv_path = output_dir / "cadence_checks.csv"
    probe_csv_path = output_dir / "remote_probe_results.csv"
    operator_command_path = output_dir / "operator_next_command.sh"
    next_actions_path = output_dir / "NEXT_ACTIONS.md"
    manifest_path = output_dir / "MANIFEST.sha256"

    executive_summary_path.write_text(_render_executive_summary(payload), encoding="utf-8")
    markdown_path.write_text(_render_markdown(payload), encoding="utf-8")
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")
    _write_rows_csv(rows_csv_path, payload["window_rows"], WINDOW_ROW_FIELDS)
    _write_rows_csv(checks_csv_path, payload["cadence_checks"])
    _write_probe_csv(probe_csv_path, payload["remote_probe_results"])
    operator_command_path.write_text(_render_operator_command(payload), encoding="utf-8")
    _mark_executable(operator_command_path)
    next_actions_path.write_text(_render_next_actions(payload), encoding="utf-8")
    _write_manifest(
        manifest_path,
        [
            executive_summary_path,
            markdown_path,
            json_path,
            rows_csv_path,
            checks_csv_path,
            probe_csv_path,
            operator_command_path,
            next_actions_path,
        ],
    )
    return Phase3BBR53WeatherCurrentWindowCadenceArtifacts(
        output_dir=output_dir,
        executive_summary_path=executive_summary_path,
        markdown_path=markdown_path,
        json_path=json_path,
        rows_csv_path=rows_csv_path,
        checks_csv_path=checks_csv_path,
        probe_csv_path=probe_csv_path,
        operator_command_path=operator_command_path,
        next_actions_path=next_actions_path,
        manifest_path=manifest_path,
    )


def build_phase3bb_r53_weather_current_window_cadence(
    session: Session,
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    reports_dir: Path = DEFAULT_REPORTS_DIR,
    settings: Settings | None = None,
    command_args: list[str] | None = None,
    ssh_target: str | None = None,
    identity_file: str | None = None,
    app_path: str | None = None,
    env_path: str | None = None,
    db_path: str | None = None,
    series_ticker: str = "KXTEMPNYCH",
    location_key: str = "new_york",
    fresh_window_hours: int = 24,
    match_tolerance_hours: int = 3,
    snapshot_fresh_minutes: int = 20,
    min_minutes_before_target: int = 10,
    limit: int = 500,
    per_probe_timeout_seconds: int = DEFAULT_PER_PROBE_TIMEOUT_SECONDS,
    probe_runner: ProbeRunner | None = None,
) -> dict[str, Any]:
    resolved = settings or get_settings()
    now = utc_now()
    metadata = _metadata(
        session,
        settings=resolved,
        generated_at=now.isoformat(),
        command_args=command_args or [],
        output_dir=output_dir,
    )
    metadata["command_arguments"] = {
        "command": "kalshi-bot phase3bb-r53-weather-current-window-cadence-preview-narrowing-repair",
        "argv": command_args or [],
    }
    r11_context = _read_json(reports_dir / "phase3bb_r11" / "codex_cloud_context.json")
    target = _resolve_target(
        r11_context,
        ssh_target=ssh_target,
        identity_file=identity_file,
        app_path=app_path,
        env_path=env_path,
        db_path=db_path,
    )
    runner = probe_runner or _run_ssh_probe
    probes = _probes(
        target,
        series_ticker=series_ticker,
        location_key=location_key,
        fresh_window_hours=fresh_window_hours,
        match_tolerance_hours=match_tolerance_hours,
        snapshot_fresh_minutes=snapshot_fresh_minutes,
        min_minutes_before_target=min_minutes_before_target,
        limit=limit,
        timeout_seconds=per_probe_timeout_seconds,
    )
    results = [runner(probe, target) for probe in probes]
    parsed = _parse_probe_outputs(results)
    summary = _summary(parsed)
    checks = _cadence_checks(parsed, summary)
    decision = _decision(summary, checks)
    safety = {
        **_safety_flags(),
        "paper_only": True,
        "diagnostic_only": True,
        "weather_current_window_cadence_repair": True,
        "ssh_read_only_commands_executed": len(results),
        "ssh_write_capable_commands_executed": 0,
        "runs_missing_link_apply": False,
        "runs_weather_forecast": False,
        "runs_weather_fast_lane": False,
        "creates_paper_trades": False,
        "places_exchange_orders": False,
        "submits_cancels_replaces_orders": False,
        "starts_or_stops_services": False,
        "starts_or_stops_r5": False,
        "thresholds_lowered": False,
        "secrets_printed": False,
    }
    return {
        **metadata,
        "phase": "3BB-R53-WEATHER-CURRENT-WINDOW-CADENCE-PREVIEW-NARROWING-REPAIR",
        "phase_version": PHASE3BB_R53_VERSION,
        "mode": "PAPER_ONLY_CURRENT_WEATHER_WINDOW_CADENCE_DIAGNOSTIC",
        "reports_dir": str(reports_dir),
        "cloud_target": _target_payload(target),
        "parameters": {
            "series_ticker": series_ticker,
            "location_key": location_key,
            "fresh_window_hours": fresh_window_hours,
            "match_tolerance_hours": match_tolerance_hours,
            "snapshot_fresh_minutes": snapshot_fresh_minutes,
            "min_minutes_before_target": min_minutes_before_target,
            "limit": limit,
        },
        "remote_probe_results": [_result_payload(result) for result in results],
        "parsed_current_window_state": parsed,
        "summary": summary,
        "cadence_checks": checks,
        "decision": decision,
        "window_rows": parsed.get("window_rows", []),
        "next_operator_command": decision["operator_next_command"],
        "safety_flags": safety,
        "live_or_demo_execution": False,
        "order_submission_cancel_replace": False,
        "paper_trade_creation": False,
        "thresholds_lowered": False,
    }


def _probes(
    target: CloudBootstrapTarget,
    *,
    series_ticker: str,
    location_key: str,
    fresh_window_hours: int,
    match_tolerance_hours: int,
    snapshot_fresh_minutes: int,
    min_minutes_before_target: int,
    limit: int,
    timeout_seconds: int,
) -> list[RemoteProbe]:
    app = shlex.quote(target.app_path)
    env = shlex.quote(target.env_path)
    writer_cmd = f"cd {app} && set -a && . {env} && set +a && .venv/bin/kalshi-bot db-writer-monitor --json"
    return [
        RemoteProbe("remote_time_utc", "date -u +%Y-%m-%dT%H:%M:%SZ", timeout_seconds),
        RemoteProbe("db_writer_monitor", writer_cmd, timeout_seconds),
        RemoteProbe(
            "command_registry",
            (
                f"cd {app} && for cmd in "
                "db-writer-monitor sync-markets market-legs-parse "
                "phase3az-r12-weather-activation-preview "
                "phase3az-r12-weather-missing-link-apply "
                "phase3bb-r47-weather-current-window-series-discovery-linkability-repair "
                "phase3bb-r48-weather-feature-refresh-runtime-verification "
                "phase3bb-r51-weather-ranking-path-repair "
                "phase3bb-r52-weather-ev-fair-value-diagnostic "
                "phase3bb-r8-unified-paper-gate; do "
                ".venv/bin/kalshi-bot \"$cmd\" --help >/dev/null || exit 30; "
                "done; echo COMMAND_REGISTRY_OK"
            ),
            timeout_seconds,
        ),
        RemoteProbe(
            "r12_preview_json",
            f"cd {app} && cat reports/phase3az_r12_weather/weather_activation_preview.json 2>/dev/null || true",
            timeout_seconds,
        ),
        RemoteProbe(
            "weather_current_window_state",
            _weather_current_window_state_command(
                target.db_path,
                series_ticker=series_ticker,
                location_key=location_key,
                fresh_window_hours=fresh_window_hours,
                match_tolerance_hours=match_tolerance_hours,
                snapshot_fresh_minutes=snapshot_fresh_minutes,
                min_minutes_before_target=min_minutes_before_target,
                limit=limit,
            ),
            timeout_seconds,
        ),
    ]


def _weather_current_window_state_command(
    db_path: str,
    *,
    series_ticker: str,
    location_key: str,
    fresh_window_hours: int,
    match_tolerance_hours: int,
    snapshot_fresh_minutes: int,
    min_minutes_before_target: int,
    limit: int,
) -> str:
    script = f"""
import json
import sqlite3
from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

db_path = {db_path!r}
series_ticker = {series_ticker!r}
location_key = {location_key!r}
fresh_window_hours = {int(fresh_window_hours)!r}
match_tolerance_hours = {int(match_tolerance_hours)!r}
snapshot_fresh_minutes = {int(snapshot_fresh_minutes)!r}
min_minutes_before_target = {int(min_minutes_before_target)!r}
limit = {int(limit)!r}
now = datetime.now(timezone.utc)
fresh_since = now - timedelta(hours=max(fresh_window_hours, 0))

def parse_dt(value):
    if value is None:
        return None
    text = str(value)
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            parsed = datetime.fromisoformat(text.replace(" ", "T"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)

def iso(value):
    return value.isoformat() if value else None

def dec(value):
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None

def dstr(value):
    if value is None:
        return None
    return format(value.quantize(Decimal("0.0001")), "f")

def minutes_until(value):
    parsed = parse_dt(value)
    if parsed is None:
        return None
    return round((parsed - now).total_seconds() / 60, 3)

def latest_by_ticker(rows):
    latest = {{}}
    for row in rows:
        ticker = str(row.get("ticker") or "")
        if ticker and ticker not in latest:
            latest[ticker] = row
    return latest

def nearest_time(rows, target, time_key, freshness_key):
    target_dt = parse_dt(target)
    if target_dt is None:
        return None
    best = None
    best_distance = None
    for row in rows:
        candidate_dt = parse_dt(row.get(time_key))
        fresh_dt = parse_dt(row.get(freshness_key))
        if candidate_dt is None:
            continue
        distance = abs((candidate_dt - target_dt).total_seconds()) / 3600
        if distance > match_tolerance_hours:
            continue
        if fresh_dt is not None and fresh_dt < fresh_since:
            continue
        if best_distance is None or distance < best_distance:
            best = row
            best_distance = distance
    return best

def target_from_market(row):
    for key in ("close_time", "expected_expiration_time", "expiration_time", "settlement_ts"):
        parsed = parse_dt(row.get(key))
        if parsed is not None:
            return parsed
    return None

def target_key(value):
    parsed = parse_dt(value)
    return iso(parsed)

def within_window(left, right):
    left_dt = parse_dt(left)
    right_dt = parse_dt(right)
    if left_dt is None or right_dt is None:
        return False
    return abs((left_dt - right_dt).total_seconds()) <= max(60, match_tolerance_hours * 3600)

payload = {{
    "ok": False,
    "error": None,
    "db_path": db_path,
    "generated_at": iso(now),
    "fresh_since": iso(fresh_since),
    "selected_target_time": None,
    "rows": [],
    "audit": {{}},
    "summary": {{}},
}}
try:
    conn = sqlite3.connect(f"file:{{db_path}}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    markets_raw = [dict(row) for row in conn.execute(
        '''
        select ticker, title, subtitle, status, series_ticker, close_time,
               expected_expiration_time, expiration_time, settlement_ts
        from markets
        where (series_ticker = ? or ticker like ?)
          and (status is null or lower(status) not in ('closed','settled','expired','inactive'))
        order by close_time desc, expected_expiration_time desc, expiration_time desc, ticker asc
        limit ?
        ''',
        (series_ticker, series_ticker + "%", limit),
    ).fetchall()]
    markets = []
    for market in markets_raw:
        target_time = target_from_market(market)
        if target_time is None:
            continue
        market["target_time"] = iso(target_time)
        markets.append(market)
    future_markets = [market for market in markets if parse_dt(market.get("target_time")) and parse_dt(market.get("target_time")) >= now]
    future_times = sorted({{target_key(market.get("target_time")) for market in future_markets if target_key(market.get("target_time"))}})
    selected_target = future_times[0] if future_times else None
    selected_markets = [market for market in future_markets if selected_target and target_key(market.get("target_time")) == selected_target]
    tickers = [str(market.get("ticker")) for market in selected_markets if market.get("ticker")]

    links = {{}}
    snapshots = {{}}
    forecasts = {{}}
    rankings = {{}}
    features_by_location = {{location_key: []}}
    source_by_location = {{location_key: []}}
    if tickers:
        placeholders = ",".join("?" for _ in tickers)
        links = latest_by_ticker([dict(row) for row in conn.execute(
            f'''
            select id, ticker, location_key, weather_metric, target_operator,
                   target_value, target_time, detected_at
            from weather_market_links
            where ticker in ({{placeholders}})
            order by ticker, detected_at desc, id desc
            ''',
            tickers,
        ).fetchall()])
        snapshots = latest_by_ticker([dict(row) for row in conn.execute(
            f'''
            select id, ticker, captured_at, status, best_yes_bid, best_yes_ask,
                   best_no_bid, best_no_ask, spread, raw_orderbook_json
            from market_snapshots
            where ticker in ({{placeholders}})
            order by ticker, captured_at desc, id desc
            ''',
            tickers,
        ).fetchall()])
        forecasts = latest_by_ticker([dict(row) for row in conn.execute(
            f'''
            select id, ticker, forecasted_at, model_name, yes_probability,
                   market_mid_probability, best_yes_bid, best_yes_ask
            from forecasts
            where ticker in ({{placeholders}}) and model_name = 'weather_v2'
            order by ticker, forecasted_at desc, id desc
            ''',
            tickers,
        ).fetchall()])
        rankings = latest_by_ticker([dict(row) for row in conn.execute(
            f'''
            select id, ticker, ranked_at, forecast_model, best_side, best_price,
                   estimated_edge, opportunity_score, spread, liquidity, reason
            from market_rankings
            where ticker in ({{placeholders}}) and forecast_model = 'weather_v2'
            order by ticker, ranked_at desc, id desc
            ''',
            tickers,
        ).fetchall()])
    for row in conn.execute(
        '''
        select id, location_key, source, generated_at, target_time,
               temperature_f, precipitation_probability, weather_confidence_score
        from weather_features
        where location_key = ?
        order by generated_at desc, id desc
        limit 50000
        ''',
        (location_key,),
    ).fetchall():
        row_dict = dict(row)
        features_by_location.setdefault(str(row_dict.get("location_key")), []).append(row_dict)
    for row in conn.execute(
        '''
        select id, location_key, source, forecast_generated_at, forecast_time,
               temperature_f, precipitation_probability
        from weather_forecasts
        where location_key = ?
        order by forecast_generated_at desc, id desc
        limit 50000
        ''',
        (location_key,),
    ).fetchall():
        row_dict = dict(row)
        source_by_location.setdefault(str(row_dict.get("location_key")), []).append(row_dict)

    rows = []
    blocker_counts = Counter()
    for market in selected_markets:
        ticker = str(market.get("ticker"))
        target_time = market.get("target_time")
        link = links.get(ticker)
        snapshot = snapshots.get(ticker)
        forecast = forecasts.get(ticker)
        ranking = rankings.get(ticker)
        row_location = str((link or {{}}).get("location_key") or location_key)
        feature = nearest_time(features_by_location.get(row_location, []), target_time, "target_time", "generated_at")
        source = nearest_time(source_by_location.get(row_location, []), target_time, "forecast_time", "forecast_generated_at")
        snapshot_at = snapshot.get("captured_at") if snapshot else None
        forecast_at = forecast.get("forecasted_at") if forecast else None
        ranking_at = ranking.get("ranked_at") if ranking else None
        snapshot_dt = parse_dt(snapshot_at)
        forecast_dt = parse_dt(forecast_at)
        ranking_dt = parse_dt(ranking_at)
        snapshot_fresh = snapshot_dt is not None and (now - snapshot_dt).total_seconds() / 60 <= snapshot_fresh_minutes
        source_fresh = source is not None and parse_dt(source.get("forecast_generated_at")) is not None and parse_dt(source.get("forecast_generated_at")) >= fresh_since
        feature_fresh = feature is not None and parse_dt(feature.get("generated_at")) is not None and parse_dt(feature.get("generated_at")) >= fresh_since
        has_current_forecast = bool(forecast_dt and snapshot_dt and forecast_dt >= snapshot_dt)
        has_current_ranking = bool(ranking_dt and forecast_dt and ranking_dt >= forecast_dt)
        link_target_matches = bool(link and within_window(link.get("target_time"), target_time))
        edge = dec(ranking.get("estimated_edge")) if ranking else None
        if link is None:
            blocker = "MISSING_WEATHER_LINK"
        elif not link_target_matches:
            blocker = "STALE_TARGET_TIME_LINK"
        elif snapshot is None:
            blocker = "SNAPSHOT_MISSING"
        elif not snapshot_fresh:
            blocker = "SNAPSHOT_STALE"
        elif source is None:
            blocker = "SOURCE_FORECAST_MISSING"
        elif not source_fresh:
            blocker = "SOURCE_FORECAST_STALE"
        elif feature is None:
            blocker = "FEATURE_MISSING_FOR_TARGET_WINDOW"
        elif not feature_fresh:
            blocker = "FEATURE_STALE"
        elif not has_current_forecast:
            blocker = "FORECAST_MISSING"
        elif not has_current_ranking:
            blocker = "RANKING_MISSING"
        elif edge is not None and edge > Decimal("0"):
            blocker = "POSITIVE_EV_READY_FOR_PAPER_GATE"
        else:
            blocker = "EV_NOT_POSITIVE"
        blocker_counts[blocker] += 1
        rows.append({{
            "ticker": ticker,
            "market_title": market.get("title"),
            "status": market.get("status"),
            "target_time": target_time,
            "minutes_until_target": minutes_until(target_time),
            "window_role": "SELECTED_NEXT_LIVE_WINDOW",
            "has_link": link is not None,
            "link_target_time": iso(parse_dt(link.get("target_time"))) if link else None,
            "link_target_matches_window": link_target_matches,
            "has_snapshot": snapshot is not None,
            "snapshot_fresh": snapshot_fresh,
            "latest_snapshot_at": snapshot_at,
            "has_source_forecast": source is not None,
            "source_forecast_fresh": source_fresh,
            "source_forecast_at": source.get("forecast_generated_at") if source else None,
            "has_weather_feature": feature is not None,
            "weather_feature_fresh": feature_fresh,
            "weather_feature_at": feature.get("generated_at") if feature else None,
            "has_current_forecast": has_current_forecast,
            "latest_forecast_at": forecast_at,
            "has_current_ranking": has_current_ranking,
            "latest_ranking_at": ranking_at,
            "estimated_edge": dstr(edge),
            "opportunity_score": dstr(dec(ranking.get("opportunity_score")) if ranking else None),
            "first_window_blocker": blocker,
        }})

    expired_markets = [market for market in markets if parse_dt(market.get("target_time")) and parse_dt(market.get("target_time")) < now]
    latest_expired_target = max((parse_dt(market.get("target_time")) for market in expired_markets), default=None)
    payload["ok"] = True
    payload["selected_target_time"] = selected_target
    payload["rows"] = rows
    payload["audit"] = {{
        "series_ticker": series_ticker,
        "location_key": location_key,
        "active_series_market_rows": len(markets),
        "future_series_market_rows": len(future_markets),
        "expired_series_market_rows": len(expired_markets),
        "latest_expired_target_time": iso(latest_expired_target),
        "future_target_times": future_times[:10],
    }}
    selected_minutes = minutes_until(selected_target)
    payload["summary"] = {{
        "selected_target_time": selected_target,
        "selected_minutes_until_target": selected_minutes,
        "selected_window_market_rows": len(rows),
        "selected_window_linked_rows": sum(1 for row in rows if row["has_link"] and row["link_target_matches_window"]),
        "selected_window_missing_link_rows": sum(1 for row in rows if not row["has_link"]),
        "selected_window_stale_link_rows": sum(1 for row in rows if row["has_link"] and not row["link_target_matches_window"]),
        "selected_window_snapshot_rows": sum(1 for row in rows if row["has_snapshot"]),
        "selected_window_fresh_snapshot_rows": sum(1 for row in rows if row["snapshot_fresh"]),
        "selected_window_source_forecast_rows": sum(1 for row in rows if row["has_source_forecast"]),
        "selected_window_feature_rows": sum(1 for row in rows if row["has_weather_feature"]),
        "selected_window_forecast_rows": sum(1 for row in rows if row["has_current_forecast"]),
        "selected_window_ranking_rows": sum(1 for row in rows if row["has_current_ranking"]),
        "selected_window_positive_ev_rows": sum(1 for row in rows if row["first_window_blocker"] == "POSITIVE_EV_READY_FOR_PAPER_GATE"),
        "selected_window_non_positive_ev_rows": sum(1 for row in rows if row["first_window_blocker"] == "EV_NOT_POSITIVE"),
        "selected_window_too_close_to_expiry": selected_minutes is not None and selected_minutes < min_minutes_before_target,
        "first_window_blocker_counts": dict(sorted(blocker_counts.items())),
    }}
except Exception as exc:
    payload["error"] = str(exc)
print(json.dumps(payload, sort_keys=True))
"""
    return "python3 - <<'PY'\n" + script.strip() + "\nPY"


def _parse_probe_outputs(results: list[RemoteProbeResult]) -> dict[str, Any]:
    by_name = {result.name: result for result in results}
    writer = _json_from_probe(by_name.get("db_writer_monitor"))
    if not isinstance(writer, dict):
        writer = {}
    state = _json_from_probe(by_name.get("weather_current_window_state"))
    if not isinstance(state, dict):
        state = {}
    r12_preview = _json_from_probe(by_name.get("r12_preview_json"))
    if not isinstance(r12_preview, dict):
        r12_preview = {}
    rows = state.get("rows") if isinstance(state.get("rows"), list) else []
    summary = state.get("summary") if isinstance(state.get("summary"), dict) else {}
    audit = state.get("audit") if isinstance(state.get("audit"), dict) else {}
    return {
        "remote_time_utc": _first_line(_stdout(by_name.get("remote_time_utc"))),
        "writer": writer,
        "writer_status": writer.get("status") or "UNKNOWN",
        "writer_safe_to_start_write": bool(writer.get("safe_to_start_write")) if writer else False,
        "writer_current_pid": writer.get("current_writer_pid"),
        "command_registry_ok": "COMMAND_REGISTRY_OK" in _stdout(by_name.get("command_registry")),
        "state_ok": bool(state.get("ok")),
        "state_error": state.get("error"),
        "r12_preview_available": bool(r12_preview),
        "r12_preview_summary": r12_preview.get("summary") if isinstance(r12_preview.get("summary"), dict) else {},
        "state_summary": summary,
        "audit": audit,
        "window_rows": rows,
        "failed_probe_names": [result.name for result in results if not result.ok],
    }


def _summary(parsed: dict[str, Any]) -> dict[str, Any]:
    state = parsed.get("state_summary") if isinstance(parsed.get("state_summary"), dict) else {}
    audit = parsed.get("audit") if isinstance(parsed.get("audit"), dict) else {}
    rows = parsed.get("window_rows") if isinstance(parsed.get("window_rows"), list) else []
    blockers = Counter(str(row.get("first_window_blocker") or "UNKNOWN") for row in rows)
    summary = {
        "remote_time_utc": parsed.get("remote_time_utc"),
        "writer_status": parsed.get("writer_status"),
        "writer_safe_to_start_write": bool(parsed.get("writer_safe_to_start_write")),
        "command_registry_ok": bool(parsed.get("command_registry_ok")),
        "state_ok": bool(parsed.get("state_ok")),
        "active_series_market_rows": _int_or_zero(audit.get("active_series_market_rows")),
        "future_series_market_rows": _int_or_zero(audit.get("future_series_market_rows")),
        "expired_series_market_rows": _int_or_zero(audit.get("expired_series_market_rows")),
        "latest_expired_target_time": audit.get("latest_expired_target_time"),
        "selected_target_time": state.get("selected_target_time"),
        "selected_minutes_until_target": state.get("selected_minutes_until_target"),
        "selected_window_market_rows": _int_or_zero(state.get("selected_window_market_rows")),
        "selected_window_linked_rows": _int_or_zero(state.get("selected_window_linked_rows")),
        "selected_window_missing_link_rows": _int_or_zero(state.get("selected_window_missing_link_rows")),
        "selected_window_stale_link_rows": _int_or_zero(state.get("selected_window_stale_link_rows")),
        "selected_window_snapshot_rows": _int_or_zero(state.get("selected_window_snapshot_rows")),
        "selected_window_fresh_snapshot_rows": _int_or_zero(state.get("selected_window_fresh_snapshot_rows")),
        "selected_window_source_forecast_rows": _int_or_zero(state.get("selected_window_source_forecast_rows")),
        "selected_window_feature_rows": _int_or_zero(state.get("selected_window_feature_rows")),
        "selected_window_forecast_rows": _int_or_zero(state.get("selected_window_forecast_rows")),
        "selected_window_ranking_rows": _int_or_zero(state.get("selected_window_ranking_rows")),
        "selected_window_positive_ev_rows": _int_or_zero(state.get("selected_window_positive_ev_rows")),
        "selected_window_non_positive_ev_rows": _int_or_zero(state.get("selected_window_non_positive_ev_rows")),
        "selected_window_too_close_to_expiry": bool(state.get("selected_window_too_close_to_expiry")),
        "first_window_blocker": _first_blocker(dict(blockers)),
        "first_window_blocker_counts": dict(blockers),
    }
    r12_summary = parsed.get("r12_preview_summary") or {}
    summary["r12_global_rows_safe_to_link"] = _int_or_zero(r12_summary.get("rows_safe_to_link"))
    summary["r12_global_rows_safe_to_relink"] = _int_or_zero(r12_summary.get("rows_safe_to_relink"))
    summary["r12_global_first_blocker"] = r12_summary.get("first_blocker")
    return summary


def _cadence_checks(parsed: dict[str, Any], summary: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        _check("remote_probes_completed", not parsed.get("failed_probe_names"), f"failed={','.join(parsed.get('failed_probe_names') or []) or 'none'}."),
        _check("command_registry_ok", bool(summary.get("command_registry_ok")), "Required weather commands are registered on the cloud."),
        _check("current_window_state_readable", bool(summary.get("state_ok")), f"error={parsed.get('state_error')}."),
        _check("selected_live_target_found", bool(summary.get("selected_target_time")), "A future weather target window was selected."),
        _check(
            "selected_window_not_too_close_to_expiry",
            bool(summary.get("selected_target_time")) and not bool(summary.get("selected_window_too_close_to_expiry")),
            f"minutes_until_target={summary.get('selected_minutes_until_target')}.",
        ),
        _check(
            "preview_narrowing_active",
            summary.get("selected_window_market_rows", 0) <= max(summary.get("future_series_market_rows", 0), 1),
            "R53 decision is based on the selected next live weather window, not all expired lookback rows.",
        ),
    ]


def _decision(summary: dict[str, Any], checks: list[dict[str, Any]]) -> dict[str, Any]:
    failed = [row for row in checks if not row["passed"]]
    writer_safe = bool(summary.get("writer_safe_to_start_write"))
    rows = _int_or_zero(summary.get("selected_window_market_rows"))
    missing_links = _int_or_zero(summary.get("selected_window_missing_link_rows"))
    stale_links = _int_or_zero(summary.get("selected_window_stale_link_rows"))
    snapshot_rows = _int_or_zero(summary.get("selected_window_snapshot_rows"))
    feature_rows = _int_or_zero(summary.get("selected_window_feature_rows"))
    forecast_rows = _int_or_zero(summary.get("selected_window_forecast_rows"))
    ranking_rows = _int_or_zero(summary.get("selected_window_ranking_rows"))
    positive_rows = _int_or_zero(summary.get("selected_window_positive_ev_rows"))
    first_blocker = summary.get("first_window_blocker") or "NO_SELECTED_WEATHER_WINDOW"
    if not summary.get("command_registry_ok"):
        status = "WEATHER_CURRENT_WINDOW_COMMAND_REGISTRY_INCOMPLETE"
        reason = "The cloud is missing one or more registered weather commands."
        command = "kalshi-bot phase3bb-r12-cloud-bootstrap-verification --output-dir reports/phase3bb_r12 --reports-dir reports"
        next_step = "Phase 3BB-R12 - Cloud Bootstrap Verification"
        blocker = "COMMAND_REGISTRY_MISSING"
        writer_required = False
    elif not summary.get("state_ok"):
        status = "WEATHER_CURRENT_WINDOW_STATE_UNREADABLE"
        reason = "R53 could not inspect the remote weather DB state."
        command = "kalshi-bot phase3bb-r53-weather-current-window-cadence-preview-narrowing-repair --output-dir reports/phase3bb_r53 --reports-dir reports"
        next_step = "Phase 3BB-R53 - Repair Weather Current Window Probe"
        blocker = "REMOTE_STATE_UNREADABLE"
        writer_required = False
    elif not summary.get("selected_target_time"):
        status = "WAIT_FOR_NEXT_WEATHER_WINDOW_OR_REFRESH_CATALOG"
        reason = "No future weather target window was present in the active KXTEMPNYCH catalog."
        command = (
            "kalshi-bot db-writer-monitor --json\n"
            "kalshi-bot sync-markets --status open --limit 100 --max-pages 3 --series-ticker KXTEMPNYCH\n"
            "kalshi-bot market-legs-parse --refresh --limit 1500\n"
            "kalshi-bot phase3bb-r53-weather-current-window-cadence-preview-narrowing-repair "
            "--output-dir reports/phase3bb_r53 --reports-dir reports"
        )
        next_step = "Phase 3BB-R53 - Refresh Catalog Then Recheck Current Window"
        blocker = "NO_FUTURE_WEATHER_WINDOW"
        writer_required = True
    elif summary.get("selected_window_too_close_to_expiry"):
        status = "WEATHER_CURRENT_WINDOW_TOO_CLOSE_TO_EXPIRY"
        reason = "Do not start forecast/ranking work when the selected target is too close to expiry."
        command = (
            "kalshi-bot phase3bb-r47-weather-current-window-series-discovery-linkability-repair "
            "--output-dir reports/phase3bb_r47 --reports-dir reports"
        )
        next_step = "Phase 3BB-R47 - Refresh Next Weather Window"
        blocker = "TARGET_WINDOW_TOO_CLOSE_TO_EXPIRY"
        writer_required = False
    elif missing_links > 0:
        status = "WEATHER_CURRENT_WINDOW_LINK_APPLY_NEEDED"
        reason = "The selected next live weather window has missing weather_market_links."
        command = (
            "kalshi-bot phase3az-r12-weather-missing-link-apply --output-dir "
            "reports/phase3az_r12_weather --limit 2000 --fresh-window-hours 24 "
            "--match-tolerance-hours 3 --max-records 25 --apply --backup-first"
        )
        next_step = "Phase 3AZ-R12 - Apply Current Weather Missing Links"
        blocker = "MISSING_WEATHER_LINK"
        writer_required = True
    elif stale_links > 0:
        status = "WEATHER_CURRENT_WINDOW_STALE_LINK_REPAIR_NEEDED"
        reason = "The selected live weather window has link rows whose target time does not match the market text."
        command = (
            "kalshi-bot phase3az-r12-weather-activation-preview --output-dir reports/phase3az_r12_weather "
            "--limit 2000 --fresh-window-hours 24 --match-tolerance-hours 3"
        )
        next_step = "Phase 3AZ-R12 - Current Weather Relink Preview"
        blocker = "STALE_TARGET_TIME_LINK"
        writer_required = False
    elif snapshot_rows < rows:
        status = "WEATHER_CURRENT_WINDOW_SNAPSHOT_MISSING"
        reason = "Selected live weather links exist but exact market snapshots/orderbooks are missing."
        command = (
            "kalshi-bot phase3bb-r51-weather-ranking-path-repair "
            "--output-dir reports/phase3bb_r51 --reports-dir reports"
        )
        next_step = "Phase 3BB-R51 - Weather Ranking Path Repair"
        blocker = "SNAPSHOT_MISSING"
        writer_required = True
    elif feature_rows < rows:
        status = "WEATHER_CURRENT_WINDOW_FEATURE_REFRESH_NEEDED"
        reason = "Selected live weather rows are linked, but fresh source/features are missing."
        command = "kalshi-bot phase3bb-r48-weather-feature-refresh-runtime-verification --output-dir reports/phase3bb_r48 --reports-dir reports"
        next_step = "Phase 3BB-R48 - Weather Feature Refresh Runtime Verification"
        blocker = "FEATURE_MISSING_FOR_TARGET_WINDOW"
        writer_required = True
    elif forecast_rows < rows or ranking_rows < rows:
        status = "WEATHER_CURRENT_WINDOW_RANKING_PATH_NEEDED"
        reason = "Selected live weather rows have links/source inputs but need forecast/ranking completion."
        command = (
            "kalshi-bot phase3bb-r51-weather-ranking-path-repair "
            "--output-dir reports/phase3bb_r51 --reports-dir reports"
        )
        next_step = "Phase 3BB-R51 - Weather Ranking Path Repair"
        blocker = "FORECAST_OR_RANKING_MISSING"
        writer_required = True
    elif positive_rows > 0:
        status = "WEATHER_CURRENT_WINDOW_POSITIVE_EV_REFRESH_PAPER_GATE"
        reason = "Selected live weather rows have positive EV; refresh the unified paper gate."
        command = "kalshi-bot phase3bb-r8-unified-paper-gate --output-dir reports/phase3bb_r8 --reports-dir reports"
        next_step = "Phase 3BB-R8 - Unified Paper Gate"
        blocker = "PAPER_GATE_REFRESH_NEEDED"
        writer_required = False
    elif failed:
        status = "WEATHER_CURRENT_WINDOW_CADENCE_CHECK_WARNING"
        reason = f"First failing cadence check: {failed[0]['check']}."
        command = "kalshi-bot phase3bb-r40-cloud-scheduler-runtime-monitor --output-dir reports/phase3bb_r40 --reports-dir reports"
        next_step = "Phase 3BB-R40 - Cloud Scheduler Runtime Monitor"
        blocker = failed[0]["check"].upper()
        writer_required = False
    else:
        status = "WEATHER_CURRENT_WINDOW_EV_NOT_POSITIVE"
        reason = "Selected live weather rows are ranked, but EV is not positive."
        command = (
            "kalshi-bot phase3bb-r52-weather-ev-fair-value-diagnostic "
            "--output-dir reports/phase3bb_r52 --reports-dir reports"
        )
        next_step = "Phase 3BB-R52 - Weather EV / Fair-Value Diagnostic"
        blocker = first_blocker or "EV_NOT_POSITIVE"
        writer_required = False
    blocked_by_writer = bool(writer_required and not writer_safe)
    if blocked_by_writer:
        command = "kalshi-bot db-writer-monitor --json"
        next_step = "Wait for writer gate to clear, then rerun R53 recommended command."
    return {
        "status": status,
        "first_hard_blocker": blocker,
        "primary_reason": reason,
        "selected_target_time": summary.get("selected_target_time"),
        "selected_minutes_until_target": summary.get("selected_minutes_until_target"),
        "writer_required": writer_required,
        "blocked_by_writer": blocked_by_writer,
        "writer_safe_to_start_write": writer_safe,
        "operator_next_command": command,
        "next_codex_step": next_step,
        "paper_trades_allowed": False,
        "live_demo_orders_allowed": False,
    }


def _first_blocker(counts: dict[str, int]) -> str | None:
    order = (
        "MISSING_WEATHER_LINK",
        "STALE_TARGET_TIME_LINK",
        "SNAPSHOT_MISSING",
        "SNAPSHOT_STALE",
        "SOURCE_FORECAST_MISSING",
        "SOURCE_FORECAST_STALE",
        "FEATURE_MISSING_FOR_TARGET_WINDOW",
        "FEATURE_STALE",
        "FORECAST_MISSING",
        "RANKING_MISSING",
        "EV_NOT_POSITIVE",
        "POSITIVE_EV_READY_FOR_PAPER_GATE",
    )
    for key in order:
        if _int_or_zero(counts.get(key)) > 0:
            return key
    return next(iter(counts), None)


def _check(name: str, passed: bool, detail: str) -> dict[str, Any]:
    return {"check": name, "passed": bool(passed), "detail": detail}


def _int_or_zero(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _render_executive_summary(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    decision = payload["decision"]
    lines = _metadata_lines(payload, "# Phase 3BB-R53 Weather Current Window Cadence")
    lines.extend(
        [
            f"- Live/demo execution: `{payload['live_or_demo_execution']}`",
            f"- Order submission/cancel/replace: `{payload['order_submission_cancel_replace']}`",
            f"- Paper trade creation: `{payload['paper_trade_creation']}`",
            f"- Thresholds lowered: `{payload['thresholds_lowered']}`",
            "",
            "## Result",
            "",
            f"- Status: `{decision['status']}`",
            f"- First hard blocker: `{decision['first_hard_blocker']}`",
            f"- Selected target time: `{summary.get('selected_target_time')}`",
            f"- Minutes until target: `{summary.get('selected_minutes_until_target')}`",
            f"- Selected window market rows: `{summary['selected_window_market_rows']}`",
            f"- Missing links in selected window: `{summary['selected_window_missing_link_rows']}`",
            f"- Forecast rows in selected window: `{summary['selected_window_forecast_rows']}`",
            f"- Ranking rows in selected window: `{summary['selected_window_ranking_rows']}`",
            f"- Positive EV rows in selected window: `{summary['selected_window_positive_ev_rows']}`",
            f"- Writer safe: `{summary['writer_safe_to_start_write']}`",
            "",
            "## Why",
            "",
            decision["primary_reason"],
            "",
            "R53 narrows weather work to the next live target window and keeps expired lookback rows as audit context only.",
            "",
            "## Next",
            "",
            "```bash",
            decision["operator_next_command"],
            "```",
            "",
            "No paper trades, live/demo orders, service starts/stops, threshold changes, or fake evidence were run.",
        ]
    )
    return "\n".join(lines) + "\n"


def _render_markdown(payload: dict[str, Any]) -> str:
    rows = payload["window_rows"]
    summary = payload["summary"]
    decision = payload["decision"]
    lines = [
        "# Weather Current Window Cadence",
        "",
        f"Status: `{decision['status']}`",
        f"Selected target: `{summary.get('selected_target_time')}`",
        f"First blocker: `{decision['first_hard_blocker']}`",
        "",
        "| Ticker | Target | Min Left | Link | Snapshot | Feature | Forecast | Ranking | Edge | Blocker |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for row in rows[:30]:
        lines.append(
            "| "
            + " | ".join(
                [
                    str(row.get("ticker") or ""),
                    str(row.get("target_time") or ""),
                    str(row.get("minutes_until_target") or ""),
                    str(row.get("has_link")),
                    str(row.get("has_snapshot")),
                    str(row.get("has_weather_feature")),
                    str(row.get("has_current_forecast")),
                    str(row.get("has_current_ranking")),
                    str(row.get("estimated_edge") or ""),
                    str(row.get("first_window_blocker") or ""),
                ]
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "## Guardrails",
            "",
            "- Paper/read-only diagnostic.",
            "- No paper trades.",
            "- No live/demo orders.",
            "- No threshold lowering.",
        ]
    )
    return "\n".join(lines) + "\n"


def _render_next_actions(payload: dict[str, Any]) -> str:
    decision = payload["decision"]
    return "\n".join(
        [
            "# Next Actions",
            "",
            f"Status: `{decision['status']}`",
            f"First hard blocker: `{decision['first_hard_blocker']}`",
            f"Selected target: `{decision.get('selected_target_time')}`",
            f"Blocked by writer: `{decision['blocked_by_writer']}`",
            "",
            "```bash",
            decision["operator_next_command"],
            "```",
            "",
            "Guardrails:",
            "- Run writer-capable commands only after `db-writer-monitor` is clear.",
            "- Do not create paper trades unless a downstream paper-ready gate opens.",
            "- Do not submit/cancel/replace live or demo orders.",
            "- Do not lower EV, confidence, liquidity, spread, settlement, or risk thresholds.",
        ]
    ) + "\n"


def _render_operator_command(payload: dict[str, Any]) -> str:
    return "#!/usr/bin/env bash\nset -euo pipefail\n" + payload["decision"]["operator_next_command"] + "\n"


def _write_rows_csv(path: Path, rows: list[dict[str, Any]], fields: list[str] | None = None) -> None:
    if fields is None:
        fields = sorted({key for row in rows for key in row})
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _write_probe_csv(path: Path, results: list[dict[str, Any]]) -> None:
    fields = ["name", "ok", "exit_code", "duration_seconds", "stdout_tail", "stderr_tail", "command"]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for result in results:
            writer.writerow(result)
