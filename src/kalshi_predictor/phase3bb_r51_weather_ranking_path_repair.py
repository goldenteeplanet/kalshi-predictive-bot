from __future__ import annotations

import csv
import json
import shlex
from collections import Counter
from dataclasses import dataclass
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
    _parse_report_stats,
    _stdout,
    _target_payload,
)
from kalshi_predictor.utils.time import utc_now

PHASE3BB_R51_VERSION = "phase3bb_r51_weather_ranking_path_repair_v1"
DEFAULT_OUTPUT_DIR = Path("reports/phase3bb_r51")
DEFAULT_REPORTS_DIR = Path("reports")
DEFAULT_PER_PROBE_TIMEOUT_SECONDS = 60
DEFAULT_REPAIR_TIMEOUT_SECONDS = 300

WEATHER_RANKING_PATH_REPORT_PATHS = (
    "reports/phase3bb_r2/weather_funnel.json",
    "reports/phase3bb_r2/weather_candidates.csv",
    "reports/phase3ba_r2/weather_ranking_activation.json",
    "reports/phase3ba_r2/weather_opportunity_rows.csv",
    "reports/phase3ba_r3/weather_paper_gate.json",
    "reports/phase3az_r12_weather/weather_activation_preview.json",
    "reports/weather_opportunities.md",
)


@dataclass(frozen=True)
class Phase3BBR51WeatherRankingPathRepairArtifacts:
    output_dir: Path
    executive_summary_path: Path
    markdown_path: Path
    json_path: Path
    probe_csv_path: Path
    checks_csv_path: Path
    path_rows_csv_path: Path
    skip_reasons_csv_path: Path
    report_freshness_csv_path: Path
    operator_command_path: Path
    next_actions_path: Path
    manifest_path: Path


def write_phase3bb_r51_weather_ranking_path_repair_report(
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
    current_window_lookback_hours: int = 3,
    fresh_window_hours: int = 24,
    match_tolerance_hours: int = 3,
    run_repair: bool = True,
    repair_timeout_seconds: int = DEFAULT_REPAIR_TIMEOUT_SECONDS,
    per_probe_timeout_seconds: int = DEFAULT_PER_PROBE_TIMEOUT_SECONDS,
    probe_runner: ProbeRunner | None = None,
) -> Phase3BBR51WeatherRankingPathRepairArtifacts:
    payload = build_phase3bb_r51_weather_ranking_path_repair(
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
        current_window_lookback_hours=current_window_lookback_hours,
        fresh_window_hours=fresh_window_hours,
        match_tolerance_hours=match_tolerance_hours,
        run_repair=run_repair,
        repair_timeout_seconds=repair_timeout_seconds,
        per_probe_timeout_seconds=per_probe_timeout_seconds,
        probe_runner=probe_runner,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    executive_summary_path = output_dir / "EXECUTIVE_SUMMARY.md"
    markdown_path = output_dir / "weather_ranking_path_repair.md"
    json_path = output_dir / "weather_ranking_path_repair.json"
    probe_csv_path = output_dir / "remote_probe_results.csv"
    checks_csv_path = output_dir / "ranking_path_checks.csv"
    path_rows_csv_path = output_dir / "weather_ranking_path_rows.csv"
    skip_reasons_csv_path = output_dir / "forecast_skip_reasons.csv"
    report_freshness_csv_path = output_dir / "weather_ranking_path_report_freshness.csv"
    operator_command_path = output_dir / "operator_next_command.sh"
    next_actions_path = output_dir / "NEXT_ACTIONS.md"
    manifest_path = output_dir / "MANIFEST.sha256"

    executive_summary_path.write_text(_render_executive_summary(payload), encoding="utf-8")
    markdown_path.write_text(_render_markdown(payload), encoding="utf-8")
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")
    _write_probe_csv(probe_csv_path, payload["remote_probe_results"])
    _write_rows_csv(checks_csv_path, payload["ranking_path_checks"])
    _write_rows_csv(path_rows_csv_path, payload["weather_ranking_path_rows"])
    _write_rows_csv(skip_reasons_csv_path, payload["forecast_skip_reasons"])
    _write_rows_csv(report_freshness_csv_path, payload["weather_ranking_path_report_freshness"])
    operator_command_path.write_text(_render_operator_command(payload), encoding="utf-8")
    _mark_executable(operator_command_path)
    next_actions_path.write_text(_render_next_actions(payload), encoding="utf-8")
    _write_manifest(
        manifest_path,
        [
            executive_summary_path,
            markdown_path,
            json_path,
            probe_csv_path,
            checks_csv_path,
            path_rows_csv_path,
            skip_reasons_csv_path,
            report_freshness_csv_path,
            operator_command_path,
            next_actions_path,
        ],
    )
    return Phase3BBR51WeatherRankingPathRepairArtifacts(
        output_dir=output_dir,
        executive_summary_path=executive_summary_path,
        markdown_path=markdown_path,
        json_path=json_path,
        probe_csv_path=probe_csv_path,
        checks_csv_path=checks_csv_path,
        path_rows_csv_path=path_rows_csv_path,
        skip_reasons_csv_path=skip_reasons_csv_path,
        report_freshness_csv_path=report_freshness_csv_path,
        operator_command_path=operator_command_path,
        next_actions_path=next_actions_path,
        manifest_path=manifest_path,
    )


def build_phase3bb_r51_weather_ranking_path_repair(
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
    current_window_lookback_hours: int = 3,
    fresh_window_hours: int = 24,
    match_tolerance_hours: int = 3,
    run_repair: bool = True,
    repair_timeout_seconds: int = DEFAULT_REPAIR_TIMEOUT_SECONDS,
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
        "command": "kalshi-bot phase3bb-r51-weather-ranking-path-repair",
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
    initial_results = [
        runner(probe, target)
        for probe in _initial_probes(
            target,
            current_window_lookback_hours=current_window_lookback_hours,
            fresh_window_hours=fresh_window_hours,
            match_tolerance_hours=match_tolerance_hours,
            timeout_seconds=per_probe_timeout_seconds,
        )
    ]
    initial = _parse_initial_probe_outputs(initial_results)
    repair_policy = _should_run_repair(initial, run_repair=run_repair)
    repair_results: list[RemoteProbeResult] = []
    if repair_policy["run"]:
        repair_results.extend(
            runner(probe, target)
            for probe in _repair_probes(target, timeout_seconds=repair_timeout_seconds)
        )
    final_results = [
        runner(probe, target)
        for probe in _final_probes(
            target,
            current_window_lookback_hours=current_window_lookback_hours,
            fresh_window_hours=fresh_window_hours,
            match_tolerance_hours=match_tolerance_hours,
            timeout_seconds=per_probe_timeout_seconds,
        )
    ]
    results = initial_results + repair_results + final_results
    parsed = _parse_final_probe_outputs(results, initial=initial, repair_policy=repair_policy)
    checks = _ranking_path_checks(parsed)
    decision = _decision(checks, parsed)
    safety = {
        **_safety_flags(),
        "paper_only": True,
        "weather_ranking_path_repair": True,
        "ssh_read_only_commands_executed": len(initial_results) + len(final_results),
        "ssh_write_capable_commands_executed": len(repair_results),
        "runs_weather_snapshot_capture": "weather_snapshot_capture" in parsed.get("repair_probe_names", []),
        "runs_weather_forecast": "weather_forecast_run" in parsed.get("repair_probe_names", []),
        "runs_weather_fast_lane": "weather_fast_lane_run" in parsed.get("repair_probe_names", []),
        "runs_missing_link_apply": False,
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
        "phase": "3BB-R51-WEATHER-RANKING-PATH-REPAIR",
        "phase_version": PHASE3BB_R51_VERSION,
        "mode": "PAPER_ONLY_WEATHER_RANKING_PATH_REPAIR",
        "reports_dir": str(reports_dir),
        "r11_context_available": bool(r11_context),
        "cloud_target": _target_payload(target),
        "remote_probe_results": [_result_payload(result) for result in results],
        "parsed_ranking_path_state": parsed,
        "ranking_path_checks": checks,
        "ranking_path_decision": decision,
        "weather_ranking_path_rows": parsed.get("post_rows") or parsed.get("pre_rows") or [],
        "forecast_skip_reasons": _skip_reason_rows(parsed),
        "weather_ranking_path_report_freshness": parsed.get("weather_ranking_path_report_freshness") or [],
        "next_operator_command": decision["operator_next_command"],
        "safety_flags": safety,
        "live_or_demo_execution": False,
        "order_submission_cancel_replace": False,
        "paper_trade_creation": False,
        "thresholds_lowered": False,
    }


def _initial_probes(
    target: CloudBootstrapTarget,
    *,
    current_window_lookback_hours: int,
    fresh_window_hours: int,
    match_tolerance_hours: int,
    timeout_seconds: int,
) -> list[RemoteProbe]:
    app = shlex.quote(target.app_path)
    env = shlex.quote(target.env_path)
    writer_cmd = f"cd {app} && set -a && . {env} && set +a && .venv/bin/kalshi-bot db-writer-monitor --json"
    return [
        RemoteProbe("remote_time_utc", "date -u +%Y-%m-%dT%H:%M:%SZ", timeout_seconds),
        RemoteProbe("db_writer_monitor_pre", writer_cmd, timeout_seconds),
        RemoteProbe(
            "command_registry",
            (
                f"cd {app} && for cmd in "
                "snapshot forecast phase3bb-r2-weather-fast-lane "
                "phase3bb-r47-weather-current-window-series-discovery-linkability-repair "
                "phase3bb-r49-weather-missing-link-apply-after-feature-refresh; do "
                ".venv/bin/kalshi-bot \"$cmd\" --help >/dev/null || exit 30; "
                "done; echo COMMAND_REGISTRY_OK"
            ),
            timeout_seconds,
        ),
        RemoteProbe(
            "weather_ranking_path_state_pre",
            _weather_ranking_path_state_command(
                target.db_path,
                current_window_lookback_hours=current_window_lookback_hours,
                fresh_window_hours=fresh_window_hours,
                match_tolerance_hours=match_tolerance_hours,
            ),
            timeout_seconds,
        ),
    ]


def _repair_probes(target: CloudBootstrapTarget, *, timeout_seconds: int) -> list[RemoteProbe]:
    app = shlex.quote(target.app_path)
    env = shlex.quote(target.env_path)
    timeout_value = int(timeout_seconds)
    prefix = f"cd {app} && set -a && . {env} && set +a && "
    return [
        RemoteProbe(
            "weather_snapshot_capture",
            (
                prefix
                + f"timeout {timeout_value} .venv/bin/kalshi-bot snapshot "
                "--status open --limit 100 --max-pages 3 --series-ticker KXTEMPNYCH"
            ),
            timeout_value + 10,
        ),
        RemoteProbe(
            "weather_forecast_run",
            (
                prefix
                + f"timeout {timeout_value} .venv/bin/kalshi-bot forecast --model weather_v2 --limit 500"
            ),
            timeout_value + 10,
        ),
        RemoteProbe(
            "weather_fast_lane_run",
            (
                prefix
                + f"timeout {timeout_value} .venv/bin/kalshi-bot phase3bb-r2-weather-fast-lane "
                "--output-dir reports/phase3bb_r2 --reports-dir reports"
            ),
            timeout_value + 10,
        ),
    ]


def _final_probes(
    target: CloudBootstrapTarget,
    *,
    current_window_lookback_hours: int,
    fresh_window_hours: int,
    match_tolerance_hours: int,
    timeout_seconds: int,
) -> list[RemoteProbe]:
    app = shlex.quote(target.app_path)
    env = shlex.quote(target.env_path)
    writer_cmd = f"cd {app} && set -a && . {env} && set +a && .venv/bin/kalshi-bot db-writer-monitor --json"
    report_list = " ".join(shlex.quote(path) for path in WEATHER_RANKING_PATH_REPORT_PATHS)
    return [
        RemoteProbe("db_writer_monitor_post", writer_cmd, timeout_seconds),
        RemoteProbe(
            "weather_ranking_path_state_post",
            _weather_ranking_path_state_command(
                target.db_path,
                current_window_lookback_hours=current_window_lookback_hours,
                fresh_window_hours=fresh_window_hours,
                match_tolerance_hours=match_tolerance_hours,
            ),
            timeout_seconds,
        ),
        RemoteProbe("weather_funnel_json", f"cd {app} && cat reports/phase3bb_r2/weather_funnel.json 2>/dev/null || true", timeout_seconds),
        RemoteProbe("weather_ranking_activation_json", f"cd {app} && cat reports/phase3ba_r2/weather_ranking_activation.json 2>/dev/null || true", timeout_seconds),
        RemoteProbe(
            "weather_ranking_path_report_stats",
            (
                f"cd {app} && for p in {report_list}; do "
                "if [ -e \"$p\" ]; then stat -c '%n|%Y|%s' \"$p\"; "
                "else echo \"$p|MISSING|0\"; fi; done"
            ),
            timeout_seconds,
        ),
    ]


def _weather_ranking_path_state_command(
    db_path: str,
    *,
    current_window_lookback_hours: int,
    fresh_window_hours: int,
    match_tolerance_hours: int,
) -> str:
    script = f"""
import json
import sqlite3
from collections import Counter
from datetime import datetime, timedelta, timezone

db_path = {db_path!r}
current_window_lookback_hours = {int(current_window_lookback_hours)!r}
fresh_window_hours = {int(fresh_window_hours)!r}
match_tolerance_hours = {int(match_tolerance_hours)!r}
now = datetime.now(timezone.utc)
current_since = now - timedelta(hours=max(current_window_lookback_hours, 0))
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

def age_minutes(value):
    parsed = parse_dt(value)
    if parsed is None:
        return None
    return max(0, round((now - parsed).total_seconds() / 60, 3))

def distance_hours(left, right):
    left_dt = parse_dt(left)
    right_dt = parse_dt(right)
    if left_dt is None or right_dt is None:
        return None
    return abs((left_dt - right_dt).total_seconds()) / 3600

def latest_by_ticker(rows, time_key):
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

payload = {{
    "ok": False,
    "error": None,
    "db_path": db_path,
    "generated_at": iso(now),
    "current_since": iso(current_since),
    "fresh_since": iso(fresh_since),
    "selected_target_time": None,
    "rows": [],
    "summary": {{}},
    "skip_reason_counts": {{}},
}}
try:
    conn = sqlite3.connect(f"file:{{db_path}}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    links_raw = [dict(row) for row in conn.execute(
        '''
        select id, ticker, location_key, detected_at, weather_metric, target_operator,
               target_value, target_time, confidence, reason
        from weather_market_links
        where target_time is not null
        order by target_time desc, detected_at desc, id desc
        limit 5000
        '''
    ).fetchall()]
    eligible_links = []
    for link in links_raw:
        target_time = parse_dt(link.get("target_time"))
        if target_time is None or target_time < current_since:
            continue
        link["_parsed_target_time"] = target_time
        eligible_links.append(link)
    future_target_times = sorted({{iso(link["_parsed_target_time"]) for link in eligible_links if link["_parsed_target_time"] >= now}})
    selected_target_time = future_target_times[0] if future_target_times else None
    selected_links = [
        link for link in eligible_links
        if selected_target_time is not None and iso(link["_parsed_target_time"]) == selected_target_time
    ]
    if not selected_links:
        selected_links = eligible_links
    payload["selected_target_time"] = selected_target_time
    latest_links = {{}}
    for link in selected_links:
        latest_links.setdefault(str(link.get("ticker")), link)
        if len(latest_links) >= 200:
            break
    links = list(latest_links.values())
    tickers = [str(link.get("ticker")) for link in links if link.get("ticker")]
    locations = sorted({{str(link.get("location_key") or "unknown") for link in links}})

    markets = {{}}
    snapshots = {{}}
    forecasts = {{}}
    rankings = {{}}
    skips = {{}}
    if tickers:
        placeholders = ",".join("?" for _ in tickers)
        markets = {{str(row["ticker"]): dict(row) for row in conn.execute(
            f'''
            select ticker, series_ticker, title, subtitle, status, close_time,
                   expected_expiration_time, expiration_time, settlement_ts
            from markets where ticker in ({{placeholders}})
            ''',
            tickers,
        ).fetchall()}}
        snapshots = latest_by_ticker([dict(row) for row in conn.execute(
            f'''
            select ticker, captured_at, status, best_yes_bid, best_yes_ask,
                   best_no_bid, best_no_ask, last_price_dollars, raw_orderbook_json
            from market_snapshots where ticker in ({{placeholders}})
            order by ticker, captured_at desc, id desc
            ''',
            tickers,
        ).fetchall()], "captured_at")
        forecasts = latest_by_ticker([dict(row) for row in conn.execute(
            f'''
            select id, ticker, forecasted_at, model_name, yes_probability,
                   market_mid_probability, best_yes_bid, best_yes_ask
            from forecasts
            where ticker in ({{placeholders}}) and model_name = 'weather_v2'
            order by ticker, forecasted_at desc, id desc
            ''',
            tickers,
        ).fetchall()], "forecasted_at")
        rankings = latest_by_ticker([dict(row) for row in conn.execute(
            f'''
            select id, ticker, ranked_at, forecast_model, best_side, best_price,
                   estimated_edge, opportunity_score, spread, liquidity, reason
            from market_rankings
            where ticker in ({{placeholders}}) and forecast_model = 'weather_v2'
            order by ticker, ranked_at desc, id desc
            ''',
            tickers,
        ).fetchall()], "ranked_at")
        skips = latest_by_ticker([dict(row) for row in conn.execute(
            f'''
            select ticker, skipped_at, reason, required_data, available_data
            from forecast_skip_log
            where ticker in ({{placeholders}}) and model_name = 'weather_v2'
            order by ticker, skipped_at desc, id desc
            ''',
            tickers,
        ).fetchall()], "skipped_at")

    features_by_location = {{loc: [] for loc in locations}}
    source_by_location = {{loc: [] for loc in locations}}
    if locations:
        placeholders = ",".join("?" for _ in locations)
        for row in conn.execute(
            f'''
            select id, location_key, source, generated_at, target_time,
                   temperature_f, precipitation_probability, weather_confidence_score
            from weather_features
            where location_key in ({{placeholders}})
            order by location_key, generated_at desc, target_time desc
            limit 5000
            ''',
            locations,
        ).fetchall():
            row_dict = dict(row)
            features_by_location.setdefault(str(row_dict.get("location_key")), []).append(row_dict)
        for row in conn.execute(
            f'''
            select id, location_key, source, forecast_generated_at, forecast_time,
                   temperature_f, precipitation_probability
            from weather_forecasts
            where location_key in ({{placeholders}})
            order by location_key, forecast_generated_at desc, forecast_time desc
            limit 5000
            ''',
            locations,
        ).fetchall():
            row_dict = dict(row)
            source_by_location.setdefault(str(row_dict.get("location_key")), []).append(row_dict)

    rows = []
    blocker_counts = Counter()
    skip_counts = Counter()
    for link in links:
        ticker = str(link.get("ticker"))
        target_time = parse_dt(link.get("target_time"))
        market = markets.get(ticker)
        snapshot = snapshots.get(ticker)
        forecast = forecasts.get(ticker)
        ranking = rankings.get(ticker)
        skip = skips.get(ticker)
        location = str(link.get("location_key") or "unknown")
        feature = nearest_time(features_by_location.get(location, []), link.get("target_time"), "target_time", "generated_at")
        source = nearest_time(source_by_location.get(location, []), link.get("target_time"), "forecast_time", "forecast_generated_at")
        snapshot_at = snapshot.get("captured_at") if snapshot else None
        forecast_at = forecast.get("forecasted_at") if forecast else None
        ranking_at = ranking.get("ranked_at") if ranking else None
        snapshot_fresh = snapshot_at is not None and (age_minutes(snapshot_at) or 999999) <= 15
        source_fresh = source is not None and parse_dt(source.get("forecast_generated_at")) is not None and parse_dt(source.get("forecast_generated_at")) >= fresh_since
        feature_fresh = feature is not None and parse_dt(feature.get("generated_at")) is not None and parse_dt(feature.get("generated_at")) >= fresh_since
        has_current_forecast = bool(forecast_at and snapshot_at and parse_dt(forecast_at) >= parse_dt(snapshot_at))
        has_current_ranking = bool(ranking_at and forecast_at and parse_dt(ranking_at) >= parse_dt(forecast_at))
        status = str((market or {{}}).get("status") or "").lower()
        target_expired = bool(target_time and target_time < now)
        target_minutes_until = None if target_time is None else round((target_time - now).total_seconds() / 60, 3)
        if target_expired:
            blocker = "EXPIRED_TARGET_WINDOW"
        elif market is None:
            blocker = "MARKET_ROW_MISSING"
        elif status not in ("active", "open"):
            blocker = "MARKET_NOT_ACTIVE"
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
        else:
            blocker = "RANKING_PRESENT"
        if skip:
            skip_counts[str(skip.get("reason") or "UNKNOWN")] += 1
        blocker_counts[blocker] += 1
        rows.append({{
            "ticker": ticker,
            "location_key": location,
            "target_time": iso(target_time),
            "target_minutes_until": target_minutes_until,
            "target_window_state": "EXPIRED" if target_expired else "LIVE_OR_FUTURE",
            "market_status": (market or {{}}).get("status"),
            "market_title": (market or {{}}).get("title"),
            "has_snapshot": snapshot is not None,
            "snapshot_fresh": snapshot_fresh,
            "latest_snapshot_at": snapshot_at,
            "snapshot_age_minutes": age_minutes(snapshot_at),
            "has_source_forecast": source is not None,
            "source_forecast_fresh": source_fresh,
            "source_forecast_at": source.get("forecast_generated_at") if source else None,
            "has_weather_feature": feature is not None,
            "weather_feature_fresh": feature_fresh,
            "weather_feature_at": feature.get("generated_at") if feature else None,
            "weather_feature_target_time": feature.get("target_time") if feature else None,
            "has_current_forecast": has_current_forecast,
            "latest_forecast_at": forecast_at,
            "has_current_ranking": has_current_ranking,
            "latest_ranking_at": ranking_at,
            "forecast_skip_reason": skip.get("reason") if skip else None,
            "forecast_skip_at": skip.get("skipped_at") if skip else None,
            "first_path_blocker": blocker,
        }})
    payload["ok"] = True
    payload["rows"] = rows
    payload["summary"] = {{
        "selected_target_time": selected_target_time,
        "current_weather_links": len(rows),
        "target_expired_rows": blocker_counts["EXPIRED_TARGET_WINDOW"],
        "live_or_future_rows": sum(1 for row in rows if row["target_window_state"] == "LIVE_OR_FUTURE"),
        "snapshot_rows": sum(1 for row in rows if row["has_snapshot"]),
        "fresh_snapshot_rows": sum(1 for row in rows if row["snapshot_fresh"]),
        "source_forecast_rows": sum(1 for row in rows if row["has_source_forecast"]),
        "fresh_source_forecast_rows": sum(1 for row in rows if row["source_forecast_fresh"]),
        "weather_feature_rows": sum(1 for row in rows if row["has_weather_feature"]),
        "fresh_weather_feature_rows": sum(1 for row in rows if row["weather_feature_fresh"]),
        "forecast_rows": sum(1 for row in rows if row["has_current_forecast"]),
        "ranking_rows": sum(1 for row in rows if row["has_current_ranking"]),
        "first_path_blocker_counts": dict(sorted(blocker_counts.items())),
    }}
    payload["skip_reason_counts"] = dict(sorted(skip_counts.items()))
except Exception as exc:
    payload["error"] = str(exc)
print(json.dumps(payload, sort_keys=True))
"""
    return "python3 - <<'PY'\n" + script.strip() + "\nPY"


def _parse_initial_probe_outputs(results: list[RemoteProbeResult]) -> dict[str, Any]:
    by_name = {result.name: result for result in results}
    writer = _json_from_probe(by_name.get("db_writer_monitor_pre"))
    state = _json_from_probe(by_name.get("weather_ranking_path_state_pre"))
    if not isinstance(writer, dict):
        writer = {}
    if not isinstance(state, dict):
        state = {}
    state_summary = state.get("summary") if isinstance(state.get("summary"), dict) else {}
    return {
        "remote_time_utc": _first_line(_stdout(by_name.get("remote_time_utc"))),
        "writer_pre_status": writer.get("status") or "UNKNOWN",
        "writer_pre_safe_to_start_write": bool(writer.get("safe_to_start_write")) if writer else False,
        "writer_pre_current_pid": writer.get("current_writer_pid"),
        "command_registry_ok": "COMMAND_REGISTRY_OK" in _stdout(by_name.get("command_registry")),
        "pre_state_ok": bool(state.get("ok")),
        "pre_state_error": state.get("error"),
        "pre_summary": state_summary,
        "pre_rows": state.get("rows") if isinstance(state.get("rows"), list) else [],
        "pre_skip_reason_counts": state.get("skip_reason_counts") if isinstance(state.get("skip_reason_counts"), dict) else {},
        "failed_initial_probe_names": [result.name for result in results if not result.ok],
    }


def _should_run_repair(parsed: dict[str, Any], *, run_repair: bool) -> dict[str, Any]:
    summary = parsed.get("pre_summary") or {}
    current_rows = _int_or_zero(summary.get("current_weather_links"))
    live_rows = _int_or_zero(summary.get("live_or_future_rows"))
    if not run_repair:
        return {"run": False, "reason": "Disabled by --no-run-repair."}
    if not parsed.get("writer_pre_safe_to_start_write"):
        return {"run": False, "reason": "Writer gate is not clear."}
    if not parsed.get("command_registry_ok"):
        return {"run": False, "reason": "Required cloud command registry check failed."}
    if not parsed.get("pre_state_ok"):
        return {"run": False, "reason": "Could not inspect current weather ranking path state."}
    if current_rows <= 0:
        return {"run": False, "reason": "No current weather link rows were found."}
    if live_rows <= 0:
        return {"run": False, "reason": "All current-lookback weather link rows are expired target windows."}
    return {"run": True, "reason": "Writer clear and live weather rows exist."}


def _parse_final_probe_outputs(
    results: list[RemoteProbeResult],
    *,
    initial: dict[str, Any],
    repair_policy: dict[str, Any],
) -> dict[str, Any]:
    by_name = {result.name: result for result in results}
    writer_post = _json_from_probe(by_name.get("db_writer_monitor_post"))
    post_state = _json_from_probe(by_name.get("weather_ranking_path_state_post"))
    funnel = _json_from_probe(by_name.get("weather_funnel_json"))
    ranking = _json_from_probe(by_name.get("weather_ranking_activation_json"))
    if not isinstance(writer_post, dict):
        writer_post = {}
    if not isinstance(post_state, dict):
        post_state = {}
    if not isinstance(funnel, dict):
        funnel = {}
    if not isinstance(ranking, dict):
        ranking = {}
    post_summary = post_state.get("summary") if isinstance(post_state.get("summary"), dict) else {}
    funnel_summary = funnel.get("summary") if isinstance(funnel.get("summary"), dict) else {}
    ranking_summary = ranking.get("summary") if isinstance(ranking.get("summary"), dict) else {}
    repair_names = ["weather_snapshot_capture", "weather_forecast_run", "weather_fast_lane_run"]
    repair_results = {name: by_name.get(name) for name in repair_names}
    return {
        **initial,
        "repair_should_run": bool(repair_policy.get("run")),
        "repair_skip_reason": repair_policy.get("reason"),
        "repair_probe_names": [name for name, result in repair_results.items() if result is not None],
        "snapshot_capture_ok": bool(repair_results["weather_snapshot_capture"] and repair_results["weather_snapshot_capture"].ok),
        "snapshot_capture_stdout_tail": _tail(_stdout(repair_results["weather_snapshot_capture"])),
        "snapshot_capture_stderr_tail": _tail(repair_results["weather_snapshot_capture"].stderr if repair_results["weather_snapshot_capture"] else ""),
        "forecast_run_ok": bool(repair_results["weather_forecast_run"] and repair_results["weather_forecast_run"].ok),
        "forecast_run_stdout_tail": _tail(_stdout(repair_results["weather_forecast_run"])),
        "forecast_run_stderr_tail": _tail(repair_results["weather_forecast_run"].stderr if repair_results["weather_forecast_run"] else ""),
        "fast_lane_run_ok": bool(repair_results["weather_fast_lane_run"] and repair_results["weather_fast_lane_run"].ok),
        "fast_lane_run_stdout_tail": _tail(_stdout(repair_results["weather_fast_lane_run"])),
        "fast_lane_run_stderr_tail": _tail(repair_results["weather_fast_lane_run"].stderr if repair_results["weather_fast_lane_run"] else ""),
        "writer_post_status": writer_post.get("status") or "UNKNOWN",
        "writer_post_safe_to_start_write": bool(writer_post.get("safe_to_start_write")) if writer_post else False,
        "writer_post_current_pid": writer_post.get("current_writer_pid"),
        "post_state_ok": bool(post_state.get("ok")),
        "post_state_error": post_state.get("error"),
        "post_summary": post_summary,
        "post_rows": post_state.get("rows") if isinstance(post_state.get("rows"), list) else [],
        "post_skip_reason_counts": post_state.get("skip_reason_counts") if isinstance(post_state.get("skip_reason_counts"), dict) else {},
        "weather_funnel_json_ok": bool(funnel),
        "weather_funnel_status": funnel.get("status"),
        "weather_funnel_summary": funnel_summary,
        "weather_ranking_activation_json_ok": bool(ranking),
        "weather_ranking_activation_status": ranking.get("status"),
        "weather_ranking_activation_summary": ranking_summary,
        "weather_ranking_path_report_freshness": _parse_report_stats(_stdout(by_name.get("weather_ranking_path_report_stats"))),
        "failed_final_probe_names": [
            result.name
            for result in results
            if result.name not in {"weather_snapshot_capture", "weather_forecast_run", "weather_fast_lane_run"}
            and not result.ok
        ],
        "failed_repair_probe_names": [
            name for name, result in repair_results.items() if result is not None and not result.ok
        ],
    }


def _ranking_path_checks(parsed: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        _check("initial_remote_probes_completed", not parsed.get("failed_initial_probe_names"), f"failed={','.join(parsed.get('failed_initial_probe_names') or []) or 'none'}."),
        _check("command_registry_ok", bool(parsed.get("command_registry_ok")), "Required registered cloud commands are available."),
        _check("pre_path_state_readable", bool(parsed.get("pre_state_ok")), f"error={parsed.get('pre_state_error')}."),
        _check("repair_policy_recorded", parsed.get("repair_skip_reason") is not None, str(parsed.get("repair_skip_reason"))),
        _check(
            "repair_succeeded_or_cleanly_skipped",
            (not parsed.get("repair_should_run") and not parsed.get("repair_probe_names"))
            or (
                bool(parsed.get("snapshot_capture_ok"))
                and bool(parsed.get("forecast_run_ok"))
                and bool(parsed.get("fast_lane_run_ok"))
            ),
            f"repair={parsed.get('repair_probe_names')} failed={parsed.get('failed_repair_probe_names')}.",
        ),
        _check("final_remote_probes_completed", not parsed.get("failed_final_probe_names"), f"failed={','.join(parsed.get('failed_final_probe_names') or []) or 'none'}."),
        _check("post_path_state_readable", bool(parsed.get("post_state_ok")), f"error={parsed.get('post_state_error')}."),
    ]


def _decision(checks: list[dict[str, Any]], parsed: dict[str, Any]) -> dict[str, Any]:
    failed = [row for row in checks if not row["passed"]]
    pre = parsed.get("pre_summary") or {}
    post = parsed.get("post_summary") or {}
    current_rows = _int_or_zero(post.get("current_weather_links") or pre.get("current_weather_links"))
    live_rows = _int_or_zero(post.get("live_or_future_rows") or pre.get("live_or_future_rows"))
    expired_rows = _int_or_zero(post.get("target_expired_rows") or pre.get("target_expired_rows"))
    forecast_rows = _int_or_zero(post.get("forecast_rows") or pre.get("forecast_rows"))
    ranking_rows = _int_or_zero(post.get("ranking_rows") or pre.get("ranking_rows"))
    snapshot_rows = _int_or_zero(post.get("snapshot_rows") or pre.get("snapshot_rows"))
    fresh_feature_rows = _int_or_zero(post.get("fresh_weather_feature_rows") or pre.get("fresh_weather_feature_rows"))
    blocker_counts = post.get("first_path_blocker_counts") or pre.get("first_path_blocker_counts") or {}
    first_blocker = _first_blocker(blocker_counts)
    if current_rows == 0:
        status = "WEATHER_RANKING_PATH_NO_CURRENT_LINK_ROWS"
        reason = "No current-lookback weather link rows are available for weather_v2."
        next_step = "Phase 3BB-R47 - Weather Current Window Series Discovery And Linkability Repair"
        command = (
            "kalshi-bot phase3bb-r47-weather-current-window-series-discovery-linkability-repair "
            "--output-dir reports/phase3bb_r47 --reports-dir reports"
        )
        first_blocker = "NO_CURRENT_WEATHER_LINKS"
    elif live_rows == 0 and expired_rows > 0:
        status = "WEATHER_RANKING_PATH_TARGET_WINDOW_EXPIRED"
        reason = "The linked weather rows are now expired target windows; do not forecast them."
        next_step = "Phase 3BB-R47 - Weather Current Window Series Discovery And Linkability Repair"
        command = (
            "kalshi-bot phase3bb-r47-weather-current-window-series-discovery-linkability-repair "
            "--output-dir reports/phase3bb_r47 --reports-dir reports"
        )
        first_blocker = "EXPIRED_TARGET_WINDOW"
    elif not parsed.get("writer_pre_safe_to_start_write"):
        status = "WAIT_FOR_WRITER_CLEAR"
        reason = "Writer gate was busy; weather ranking path repair did not run."
        next_step = "Phase 3BB-R51 - Retry Weather Ranking Path Repair"
        command = "kalshi-bot db-writer-monitor --json"
        first_blocker = "ACTIVE_WRITER"
    elif failed:
        status = "BLOCKED_WEATHER_RANKING_PATH_REPAIR"
        reason = f"First failing check: {failed[0]['check']}."
        next_step = "Phase 3BB-R51 - Repair R51 Probe Or Cloud Command"
        command = (
            "kalshi-bot phase3bb-r51-weather-ranking-path-repair "
            "--output-dir reports/phase3bb_r51 --reports-dir reports"
        )
        first_blocker = failed[0]["check"].upper()
    elif ranking_rows > 0:
        status = "WEATHER_RANKING_PATH_REPAIRED"
        reason = "Weather rows now have current weather_v2 rankings."
        next_step = "Phase 3BB-R8 - Unified Paper Gate Across Categories"
        command = "kalshi-bot phase3bb-r8-unified-paper-gate --output-dir reports/phase3bb_r8 --reports-dir reports"
        first_blocker = "PAPER_GATE_REFRESH_NEEDED"
    elif forecast_rows > 0:
        status = "WEATHER_FORECAST_CREATED_RANKING_STILL_MISSING"
        reason = "Weather forecasts exist after repair, but opportunity rankings are still missing."
        next_step = "Phase 3BB-R2 - Weather Fast-Lane Paper Funnel"
        command = "kalshi-bot phase3bb-r2-weather-fast-lane --output-dir reports/phase3bb_r2 --reports-dir reports"
        first_blocker = first_blocker or "RANKING_MISSING"
    elif snapshot_rows <= 0:
        status = "WEATHER_RANKING_PATH_SNAPSHOT_MISSING"
        reason = "Weather links exist but no exact snapshots/orderbooks exist for those tickers."
        next_step = "Phase 3BB-R47 - Weather Current Window Series Discovery And Linkability Repair"
        command = (
            "kalshi-bot phase3bb-r47-weather-current-window-series-discovery-linkability-repair "
            "--output-dir reports/phase3bb_r47 --reports-dir reports"
        )
        first_blocker = first_blocker or "SNAPSHOT_MISSING"
    elif fresh_feature_rows <= 0:
        status = "WEATHER_RANKING_PATH_FEATURE_MISSING"
        reason = "Weather links have market snapshots but no fresh feature row for the linked target window."
        next_step = "Phase 3BB-R48 - Weather Feature Refresh Runtime Verification"
        command = "kalshi-bot phase3bb-r48-weather-feature-refresh-runtime-verification --output-dir reports/phase3bb_r48 --reports-dir reports"
        first_blocker = first_blocker or "FEATURE_MISSING_FOR_TARGET_WINDOW"
    else:
        status = "WEATHER_RANKING_PATH_FORECAST_STILL_MISSING"
        reason = "Inputs exist but weather_v2 did not create current forecasts."
        next_step = "Phase 3BB-R51 - Inspect Forecast Skip Reasons"
        command = (
            "kalshi-bot phase3bb-r51-weather-ranking-path-repair "
            "--output-dir reports/phase3bb_r51 --reports-dir reports --no-run-repair"
        )
        first_blocker = first_blocker or "FORECAST_MISSING"
    return {
        "status": status,
        "verification_passed": not failed and status != "WAIT_FOR_WRITER_CLEAR",
        "failed_check_count": len(failed),
        "first_failed_check": failed[0]["check"] if failed else None,
        "primary_reason": reason,
        "first_weather_path_blocker": first_blocker,
        "current_weather_rows": current_rows,
        "live_or_future_rows": live_rows,
        "target_expired_rows": expired_rows,
        "snapshot_rows": snapshot_rows,
        "fresh_weather_feature_rows": fresh_feature_rows,
        "forecast_rows": forecast_rows,
        "ranking_rows": ranking_rows,
        "repair_run_attempted": bool(parsed.get("repair_probe_names")),
        "snapshot_capture_ok": bool(parsed.get("snapshot_capture_ok")),
        "forecast_run_ok": bool(parsed.get("forecast_run_ok")),
        "fast_lane_run_ok": bool(parsed.get("fast_lane_run_ok")),
        "writer_pre_safe_to_start_write": bool(parsed.get("writer_pre_safe_to_start_write")),
        "writer_post_safe_to_start_write": bool(parsed.get("writer_post_safe_to_start_write")),
        "will_create_paper_trades": False,
        "will_submit_live_or_demo_orders": False,
        "operator_next_command": command,
        "next_codex_step": next_step,
    }


def _first_blocker(counts: dict[str, Any]) -> str | None:
    order = (
        "EXPIRED_TARGET_WINDOW",
        "MARKET_ROW_MISSING",
        "MARKET_NOT_ACTIVE",
        "SNAPSHOT_MISSING",
        "SNAPSHOT_STALE",
        "SOURCE_FORECAST_MISSING",
        "SOURCE_FORECAST_STALE",
        "FEATURE_MISSING_FOR_TARGET_WINDOW",
        "FEATURE_STALE",
        "FORECAST_MISSING",
        "RANKING_MISSING",
        "RANKING_PRESENT",
    )
    for blocker in order:
        if _int_or_zero(counts.get(blocker)) > 0:
            return blocker
    return None


def _skip_reason_rows(parsed: dict[str, Any]) -> list[dict[str, Any]]:
    counts = parsed.get("post_skip_reason_counts") or parsed.get("pre_skip_reason_counts") or {}
    return [{"reason": reason, "count": count} for reason, count in sorted(counts.items())]


def _render_executive_summary(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# Phase 3BB-R51 Weather Ranking Path Repair")
    decision = payload["ranking_path_decision"]
    lines.extend(
        [
            "",
            "## Result",
            "",
            f"- Status: `{decision['status']}`",
            f"- Verification passed: `{decision['verification_passed']}`",
            f"- Reason: {decision['primary_reason']}",
            f"- First weather path blocker: `{decision['first_weather_path_blocker']}`",
            f"- Current weather rows: `{decision['current_weather_rows']}`",
            f"- Live/future rows: `{decision['live_or_future_rows']}`",
            f"- Expired target rows: `{decision['target_expired_rows']}`",
            f"- Snapshot rows: `{decision['snapshot_rows']}`",
            f"- Fresh feature rows: `{decision['fresh_weather_feature_rows']}`",
            f"- Forecast rows: `{decision['forecast_rows']}`",
            f"- Ranking rows: `{decision['ranking_rows']}`",
            f"- Repair run attempted: `{decision['repair_run_attempted']}`",
            "",
            "## Safety",
            "",
            "- Paper trade creation: `False`",
            "- Live/demo order submission/cancel/replace: `False`",
            "- Missing-link apply run by this phase: `False`",
            "- Forecast/snapshot/fast-lane run only when writer gate is clear and rows are still live.",
            "",
            "## Next",
            "",
            f"- Next Codex step: {decision['next_codex_step']}",
            "",
            "```bash",
            decision["operator_next_command"],
            "```",
            "",
        ]
    )
    return "\n".join(lines)


def _render_markdown(payload: dict[str, Any]) -> str:
    lines = _render_executive_summary(payload).splitlines()
    parsed = payload["parsed_ranking_path_state"]
    lines.extend(["", "## Checks", ""])
    lines.extend(_table(payload["ranking_path_checks"], ["check", "passed", "detail"]))
    lines.extend(["", "## Pre Summary", ""])
    lines.extend(_table([parsed.get("pre_summary") or {}], _summary_fields()))
    lines.extend(["", "## Post Summary", ""])
    lines.extend(_table([parsed.get("post_summary") or {}], _summary_fields()))
    lines.extend(["", "## Forecast Skip Reasons", ""])
    lines.extend(_table(payload["forecast_skip_reasons"], ["reason", "count"]))
    lines.extend(["", "## Path Rows", ""])
    lines.extend(
        _table(
            payload["weather_ranking_path_rows"][:25],
            [
                "ticker",
                "target_time",
                "target_window_state",
                "has_snapshot",
                "snapshot_fresh",
                "has_weather_feature",
                "weather_feature_fresh",
                "has_current_forecast",
                "has_current_ranking",
                "forecast_skip_reason",
                "first_path_blocker",
            ],
        )
    )
    lines.extend(["", "## Repair Output", ""])
    lines.extend(["### Snapshot Capture", "```text", str(parsed.get("snapshot_capture_stdout_tail") or ""), "```"])
    lines.extend(["### Forecast", "```text", str(parsed.get("forecast_run_stdout_tail") or ""), "```"])
    lines.extend(["### Fast Lane", "```text", str(parsed.get("fast_lane_run_stdout_tail") or ""), "```"])
    lines.extend(["", "## Report Freshness", ""])
    lines.extend(_table(payload["weather_ranking_path_report_freshness"], ["path", "status", "mtime_epoch", "size_bytes"]))
    return "\n".join(lines)


def _summary_fields() -> list[str]:
    return [
        "current_weather_links",
        "live_or_future_rows",
        "target_expired_rows",
        "snapshot_rows",
        "fresh_snapshot_rows",
        "source_forecast_rows",
        "fresh_source_forecast_rows",
        "weather_feature_rows",
        "fresh_weather_feature_rows",
        "forecast_rows",
        "ranking_rows",
        "first_path_blocker_counts",
    ]


def _render_next_actions(payload: dict[str, Any]) -> str:
    decision = payload["ranking_path_decision"]
    return "\n".join(
        [
            "# Next Actions",
            "",
            f"Status: `{decision['status']}`",
            f"Next Codex step: {decision['next_codex_step']}",
            "",
            "```bash",
            decision["operator_next_command"],
            "```",
            "",
            "Guardrails:",
            "- Use cloud shell/web console or SSH into the droplet for cloud commands.",
            "- Plain local WSL commands without SSH hit the local DB, not the cloud DB.",
            "- Do not forecast expired weather target windows.",
            "- Do not create paper trades unless a downstream paper-ready gate opens.",
            "- Do not submit/cancel/replace live or demo orders.",
            "",
        ]
    )


def _render_operator_command(payload: dict[str, Any]) -> str:
    command = payload["ranking_path_decision"]["operator_next_command"]
    return "\n".join(["#!/usr/bin/env bash", "set -euo pipefail", command, ""])


def _write_probe_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = ["name", "ok", "exit_code", "duration_seconds", "command", "stdout", "stderr"]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _write_rows_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = sorted({key for row in rows for key in row.keys()}) if rows else ["empty"]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _table(rows: list[dict[str, Any]], fields: list[str]) -> list[str]:
    if not rows:
        return ["_No rows._"]
    lines = [
        "| " + " | ".join(fields) + " |",
        "| " + " | ".join("---" for _ in fields) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(_cell(row.get(field)) for field in fields) + " |")
    return lines


def _check(check: str, passed: bool, detail: str) -> dict[str, Any]:
    return {"check": check, "passed": bool(passed), "detail": detail}


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        value = json.dumps(value, sort_keys=True)
    return str(value).replace("|", "\\|").replace("\n", " ")


def _tail(text: str, *, lines: int = 40) -> str:
    if not text:
        return ""
    return "\n".join(text.splitlines()[-lines:])


def _int_or_zero(value: Any) -> int:
    try:
        if value is None or value == "":
            return 0
        return int(value)
    except (TypeError, ValueError):
        return 0
