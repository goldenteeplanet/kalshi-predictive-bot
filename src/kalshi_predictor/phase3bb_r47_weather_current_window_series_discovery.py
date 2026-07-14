from __future__ import annotations

import base64
import csv
import json
import re
import shlex
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
from kalshi_predictor.phase3bb_r36_cloud_scheduler_install_handoff import (
    RUNNER_SCRIPT_NAME,
    SCHEDULER_SERVICE_NAME,
    SCHEDULER_TIMER_NAME,
)
from kalshi_predictor.phase3bb_r44_weather_catalog_hook_runtime_verification import (
    _first_line,
    _mark_executable,
    _stdout,
    _target_payload,
)
from kalshi_predictor.utils.time import utc_now

PHASE3BB_R47_VERSION = "phase3bb_r47_weather_current_window_series_discovery_v1"
DEFAULT_OUTPUT_DIR = Path("reports/phase3bb_r47")
DEFAULT_REPORTS_DIR = Path("reports")
DEFAULT_PER_PROBE_TIMEOUT_SECONDS = 60

WEATHER_CATALOG_JOB_ID = "weather_current_catalog_refresh"
WEATHER_FAST_LANE_JOB_ID = "weather_fast_lane"

FORBIDDEN_REPAIR_FRAGMENTS = (
    "accelerate-learning",
    "autopilot-once",
    "cancel-order",
    "create-paper-trade",
    "demo-order",
    "live-order",
    "paper-trade-create",
    "place-order",
    "replace-order",
    "submit-order",
    "systemctl start",
    "systemctl restart",
    "systemctl enable --now",
)


@dataclass(frozen=True)
class Phase3BBR47WeatherCurrentWindowSeriesDiscoveryArtifacts:
    output_dir: Path
    executive_summary_path: Path
    markdown_path: Path
    json_path: Path
    probe_csv_path: Path
    checks_csv_path: Path
    series_csv_path: Path
    linkability_csv_path: Path
    runner_patch_path: Path
    operator_command_path: Path
    next_actions_path: Path
    manifest_path: Path


def write_phase3bb_r47_weather_current_window_series_discovery_report(
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
    scheduler_service_name: str = SCHEDULER_SERVICE_NAME,
    scheduler_timer_name: str = SCHEDULER_TIMER_NAME,
    current_window_lookback_hours: int = 3,
    fresh_window_hours: int = 24,
    match_tolerance_hours: int = 3,
    apply: bool = False,
    backup_first: bool = False,
    per_probe_timeout_seconds: int = DEFAULT_PER_PROBE_TIMEOUT_SECONDS,
    probe_runner: ProbeRunner | None = None,
) -> Phase3BBR47WeatherCurrentWindowSeriesDiscoveryArtifacts:
    payload = build_phase3bb_r47_weather_current_window_series_discovery(
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
        scheduler_service_name=scheduler_service_name,
        scheduler_timer_name=scheduler_timer_name,
        current_window_lookback_hours=current_window_lookback_hours,
        fresh_window_hours=fresh_window_hours,
        match_tolerance_hours=match_tolerance_hours,
        apply=apply,
        backup_first=backup_first,
        per_probe_timeout_seconds=per_probe_timeout_seconds,
        probe_runner=probe_runner,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    executive_summary_path = output_dir / "EXECUTIVE_SUMMARY.md"
    markdown_path = output_dir / "weather_current_window_series_discovery.md"
    json_path = output_dir / "weather_current_window_series_discovery.json"
    probe_csv_path = output_dir / "remote_probe_results.csv"
    checks_csv_path = output_dir / "linkability_checks.csv"
    series_csv_path = output_dir / "current_weather_series.csv"
    linkability_csv_path = output_dir / "linkability_rows.csv"
    runner_patch_path = output_dir / f"{RUNNER_SCRIPT_NAME}.phase3bb_r47.draft"
    operator_command_path = output_dir / "operator_next_command.sh"
    next_actions_path = output_dir / "NEXT_ACTIONS.md"
    manifest_path = output_dir / "MANIFEST.sha256"

    executive_summary_path.write_text(_render_executive_summary(payload), encoding="utf-8")
    markdown_path.write_text(_render_markdown(payload), encoding="utf-8")
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")
    _write_rows_csv(probe_csv_path, payload["remote_probe_results"])
    _write_rows_csv(checks_csv_path, payload["linkability_checks"])
    _write_rows_csv(series_csv_path, payload["current_weather_series"])
    _write_rows_csv(linkability_csv_path, payload["linkability_rows"])
    runner_patch_path.write_text(payload["patched_runner_script"], encoding="utf-8")
    _mark_executable(runner_patch_path)
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
            series_csv_path,
            linkability_csv_path,
            runner_patch_path,
            operator_command_path,
            next_actions_path,
        ],
    )
    return Phase3BBR47WeatherCurrentWindowSeriesDiscoveryArtifacts(
        output_dir=output_dir,
        executive_summary_path=executive_summary_path,
        markdown_path=markdown_path,
        json_path=json_path,
        probe_csv_path=probe_csv_path,
        checks_csv_path=checks_csv_path,
        series_csv_path=series_csv_path,
        linkability_csv_path=linkability_csv_path,
        runner_patch_path=runner_patch_path,
        operator_command_path=operator_command_path,
        next_actions_path=next_actions_path,
        manifest_path=manifest_path,
    )


def build_phase3bb_r47_weather_current_window_series_discovery(
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
    scheduler_service_name: str = SCHEDULER_SERVICE_NAME,
    scheduler_timer_name: str = SCHEDULER_TIMER_NAME,
    current_window_lookback_hours: int = 3,
    fresh_window_hours: int = 24,
    match_tolerance_hours: int = 3,
    apply: bool = False,
    backup_first: bool = False,
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
        "command": "kalshi-bot phase3bb-r47-weather-current-window-series-discovery-linkability-repair",
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
    probes = _build_remote_probes(
        target,
        scheduler_service_name=scheduler_service_name,
        scheduler_timer_name=scheduler_timer_name,
        current_window_lookback_hours=current_window_lookback_hours,
        fresh_window_hours=fresh_window_hours,
        match_tolerance_hours=match_tolerance_hours,
        timeout_seconds=per_probe_timeout_seconds,
    )
    runner = probe_runner or _run_ssh_probe
    results = [runner(probe, target) for probe in probes]
    parsed = _parse_probe_outputs(results)
    series = parsed.get("current_weather_series") or []
    linkability_rows = parsed.get("linkability_rows") or []
    recommended = _recommended_weather_lane(series)
    repaired_block = _repaired_weather_hook_block(
        series_ticker=recommended.get("series_ticker") or "KXTEMPNYCH",
        location_key=recommended.get("location_key") or "new_york",
    )
    patched_runner = patch_runner_weather_feature_refresh(
        parsed.get("runner_script") or "",
        repaired_block=repaired_block,
    )
    parsed["recommended_series_ticker"] = recommended.get("series_ticker")
    parsed["recommended_location_key"] = recommended.get("location_key")
    parsed["patched_runner_has_feature_refresh"] = _runner_has_weather_feature_refresh(
        patched_runner,
        location_key=recommended.get("location_key") or "new_york",
    )
    parsed["runner_patch_required"] = bool(patched_runner and patched_runner != (parsed.get("runner_script") or ""))
    checks = _linkability_checks(
        parsed,
        patched_runner=patched_runner,
        apply=apply,
        backup_first=backup_first,
    )
    install_result: dict[str, Any] = {
        "attempted": False,
        "ok": False,
        "exit_code": None,
        "stdout": "",
        "stderr": "",
    }
    if apply and _can_apply(checks, parsed):
        install_probe = _build_install_probe(
            target,
            patched_runner=patched_runner,
            backup_first=backup_first,
            timeout_seconds=per_probe_timeout_seconds,
        )
        install_probe_result = runner(install_probe, target)
        install_result = {
            "attempted": True,
            "ok": install_probe_result.ok,
            "exit_code": install_probe_result.exit_code,
            "stdout": install_probe_result.stdout,
            "stderr": install_probe_result.stderr,
            "duration_seconds": install_probe_result.duration_seconds,
            "timed_out": install_probe_result.timed_out,
        }
    elif apply:
        install_result = {
            "attempted": False,
            "ok": False,
            "exit_code": None,
            "stdout": "",
            "stderr": "Apply gate blocked; see linkability_checks.",
        }
    verify_after = parsed.get("runner_has_feature_refresh")
    if install_result.get("attempted") and install_result.get("ok"):
        verify_probe = RemoteProbe(
            "verify_runner_after_r47_install",
            f"cat {shlex.quote(str(_runner_path(target)))} 2>/dev/null || true",
            per_probe_timeout_seconds,
        )
        verify_result = runner(verify_probe, target)
        verify_text = verify_result.stdout if verify_result.ok else ""
        verify_after = _runner_has_weather_feature_refresh(
            verify_text,
            location_key=recommended.get("location_key") or "new_york",
        )
        parsed["runner_script_after_install"] = verify_text
        parsed["runner_has_feature_refresh_after_install"] = verify_after
    decision = _decision(
        checks,
        parsed,
        install_result=install_result,
        apply=apply,
        backup_first=backup_first,
        verify_after=bool(verify_after),
    )
    safety = {
        **_safety_flags(),
        "paper_only": True,
        "diagnostic_only": not bool(install_result.get("attempted")),
        "weather_current_window_series_discovery": True,
        "ssh_read_only_commands_executed": len(probes),
        "ssh_mutating_commands_executed": 1 if install_result.get("attempted") else 0,
        "scheduler_runner_written_to_system": bool(install_result.get("attempted") and install_result.get("ok")),
        "scheduler_timer_started": False,
        "scheduler_service_started": False,
        "scheduler_service_stopped": False,
        "systemctl_start_stop_restart_executed": 0,
        "starts_r5_watcher": False,
        "starts_duplicate_watchers": False,
        "stops_processes": False,
        "runs_refresh_jobs": False,
        "runs_weather_fast_lane": False,
        "remote_db_writes_performed": 0,
        "local_db_writes_performed": 0,
        "creates_paper_trades": False,
        "places_exchange_orders": False,
        "submits_cancels_replaces_orders": False,
        "lowers_thresholds": False,
        "fabricates_evidence": False,
        "uses_fuzzy_or_sibling_matching": False,
        "secrets_printed": False,
    }
    return {
        **metadata,
        "phase": "3BB-R47",
        "phase_version": PHASE3BB_R47_VERSION,
        "mode": "PAPER_ONLY_WEATHER_CURRENT_WINDOW_DISCOVERY_AND_LINKABILITY_REPAIR",
        "cloud_target": _target_payload(target),
        "scheduler_service_name": scheduler_service_name,
        "scheduler_timer_name": scheduler_timer_name,
        "parameters": {
            "current_window_lookback_hours": current_window_lookback_hours,
            "fresh_window_hours": fresh_window_hours,
            "match_tolerance_hours": match_tolerance_hours,
            "apply": apply,
            "backup_first": backup_first,
        },
        "remote_probe_results": [_result_payload(result) for result in results],
        "parsed_linkability_state": parsed,
        "current_weather_series": series,
        "linkability_rows": linkability_rows,
        "recommended_weather_lane": recommended,
        "linkability_checks": checks,
        "install_result": install_result,
        "patched_runner_script": patched_runner,
        "repaired_weather_hook_block": repaired_block,
        "linkability_decision": decision,
        "next_operator_command": decision["operator_next_command"],
        "safety_flags": safety,
        "live_or_demo_execution": False,
        "order_submission": False,
        "order_cancel_replace": False,
        "paper_trade_creation": False,
        "thresholds_lowered": False,
    }


def _build_remote_probes(
    target: CloudBootstrapTarget,
    *,
    scheduler_service_name: str,
    scheduler_timer_name: str,
    current_window_lookback_hours: int,
    fresh_window_hours: int,
    match_tolerance_hours: int,
    timeout_seconds: int,
) -> list[RemoteProbe]:
    app = shlex.quote(target.app_path)
    env = shlex.quote(target.env_path)
    service = shlex.quote(scheduler_service_name)
    timer = shlex.quote(scheduler_timer_name)
    writer_cmd = f"cd {app} && set -a && . {env} && set +a && .venv/bin/kalshi-bot db-writer-monitor --json"
    return [
        RemoteProbe("remote_time_utc", "date -u +%Y-%m-%dT%H:%M:%SZ", timeout_seconds),
        RemoteProbe("scheduler_timer_active", f"systemctl is-active {timer} || true", timeout_seconds),
        RemoteProbe("scheduler_service_active", f"systemctl is-active {service} || true", timeout_seconds),
        RemoteProbe("scheduler_service_show", f"systemctl show {service} -p Result -p ActiveState -p SubState -p ExecMainStatus --no-pager || true", timeout_seconds),
        RemoteProbe("db_writer_monitor_raw", writer_cmd, timeout_seconds),
        RemoteProbe("runner_script", f"cat {shlex.quote(str(_runner_path(target)))} 2>/dev/null || true", timeout_seconds),
        RemoteProbe("weather_activation_preview_json", f"cd {app} && cat reports/phase3az_r12_weather/weather_activation_preview.json 2>/dev/null || true", timeout_seconds),
        RemoteProbe("weather_funnel_json", f"cd {app} && cat reports/phase3bb_r2/weather_funnel.json 2>/dev/null || true", timeout_seconds),
        RemoteProbe(
            "weather_current_window_snapshot",
            _weather_current_window_snapshot_command(
                target.db_path,
                current_window_lookback_hours=current_window_lookback_hours,
                fresh_window_hours=fresh_window_hours,
                match_tolerance_hours=match_tolerance_hours,
            ),
            timeout_seconds,
        ),
        RemoteProbe(
            "command_registry",
            (
                f"cd {app} && for cmd in "
                "sync-markets market-legs-parse ingest-weather build-weather-features "
                "phase3az-r12-weather-activation-preview phase3bb-r2-weather-fast-lane; do "
                ".venv/bin/kalshi-bot \"$cmd\" --help >/dev/null || exit 30; "
                "done; echo COMMAND_REGISTRY_OK"
            ),
            timeout_seconds,
        ),
    ]


def _weather_current_window_snapshot_command(
    db_path: str,
    *,
    current_window_lookback_hours: int,
    fresh_window_hours: int,
    match_tolerance_hours: int,
) -> str:
    script = f"""
import json
import sqlite3
from collections import Counter, defaultdict
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
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)

def iso(value):
    return value.isoformat() if value else None

def series_key(row):
    raw = row.get("series_ticker")
    if raw:
        return str(raw)
    ticker = str(row.get("ticker") or "")
    return ticker.split("-", 1)[0] if "-" in ticker else ticker

def infer_location(row):
    text = " ".join(str(row.get(key) or "") for key in ("ticker", "series_ticker", "title", "subtitle"))
    lowered = text.lower()
    if "nych" in lowered or "new york" in lowered or "nyc" in lowered:
        return "new_york"
    if "kansas city" in lowered or "kcmo" in lowered:
        return "kansas_city"
    if "chicago" in lowered:
        return "chicago"
    if "los angeles" in lowered or "lax" in lowered:
        return "los_angeles"
    return "unknown"

def feature_match(features, target_time):
    if not target_time:
        return None
    best = None
    best_distance = None
    for feature in features:
        feature_target = parse_dt(feature.get("target_time"))
        generated_at = parse_dt(feature.get("generated_at"))
        if feature_target is None or generated_at is None:
            continue
        distance = abs((feature_target - target_time).total_seconds()) / 3600
        if distance > match_tolerance_hours:
            continue
        if generated_at < fresh_since:
            continue
        if best_distance is None or distance < best_distance:
            best = feature
            best_distance = distance
    return best

payload = {{
    "ok": False,
    "error": None,
    "db_path": db_path,
    "snapshot_generated_at": iso(now),
    "current_since": iso(current_since),
    "fresh_since": iso(fresh_since),
    "current_weather_series": [],
    "linkability_rows": [],
    "feature_windows": [],
    "summary": {{}},
}}
try:
    conn = sqlite3.connect(f"file:{{db_path}}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    market_sql = '''
        select ticker, series_ticker, title, subtitle, status, close_time,
               expected_expiration_time, expiration_time, settlement_ts
        from markets
        where lower(coalesce(status,'')) in ('active','open') and (
            ticker like 'KXTEMP%' or coalesce(series_ticker,'') like 'KXTEMP%' or
            ticker like 'KXHIGH%' or coalesce(series_ticker,'') like 'KXHIGH%' or
            ticker like 'KXLOW%' or coalesce(series_ticker,'') like 'KXLOW%' or
            ticker like 'KXRAIN%' or coalesce(series_ticker,'') like 'KXRAIN%' or
            ticker like 'KXWIND%' or coalesce(series_ticker,'') like 'KXWIND%'
        )
        order by close_time desc, ticker
        limit 5000
    '''
    markets = [dict(row) for row in conn.execute(market_sql).fetchall()]
    current_market_candidates = []
    for market in markets:
        target_time = parse_dt(market.get("close_time") or market.get("expected_expiration_time") or market.get("expiration_time") or market.get("settlement_ts"))
        if target_time is not None and target_time >= current_since:
            current_market_candidates.append((market, target_time, infer_location(market)))
    locations = sorted({{location for _, _, location in current_market_candidates if location != "unknown"}})
    tickers = [row["ticker"] for row, _, _ in current_market_candidates]
    links_by_ticker = set()
    if tickers:
        placeholders = ",".join("?" for _ in tickers)
        for row in conn.execute(f"select distinct ticker from weather_market_links where ticker in ({{placeholders}})", tickers):
            links_by_ticker.add(str(row[0]))
    features = []
    feature_aggregates = []
    if locations:
        placeholders = ",".join("?" for _ in locations)
        feature_args = [*locations, fresh_since.replace(tzinfo=None).isoformat(sep=" ")]
        features = [dict(row) for row in conn.execute(
            f'''
            select id, location_key, source, generated_at, target_time, temperature_f,
                   weather_confidence_score
            from weather_features
            where location_key in ({{placeholders}}) and generated_at >= ?
            order by generated_at desc, target_time desc
            limit 5000
            ''',
            feature_args,
        ).fetchall()]
        feature_aggregates = [dict(row) for row in conn.execute(
            f'''
            select location_key, count(*) feature_rows_sampled, max(generated_at) max_generated_at
            from weather_features
            where location_key in ({{placeholders}})
            group by location_key
            ''',
            locations,
        ).fetchall()]
    features_by_location = defaultdict(list)
    feature_max_generated = {{}}
    for feature in features:
        loc = str(feature.get("location_key") or "unknown")
        features_by_location[loc].append(feature)
        generated_at = parse_dt(feature.get("generated_at"))
        if generated_at and (loc not in feature_max_generated or generated_at > feature_max_generated[loc]):
            feature_max_generated[loc] = generated_at
    feature_locations = Counter()
    for aggregate in feature_aggregates:
        loc = str(aggregate.get("location_key") or "unknown")
        feature_locations[loc] = int(aggregate.get("feature_rows_sampled") or 0)
        generated_at = parse_dt(aggregate.get("max_generated_at"))
        if generated_at and (loc not in feature_max_generated or generated_at > feature_max_generated[loc]):
            feature_max_generated[loc] = generated_at
    current_rows = []
    series_stats = defaultdict(lambda: Counter())
    series_samples = {{}}
    blocker_counts = Counter()
    for market, target_time, location in current_market_candidates:
        series = series_key(market)
        has_link = str(market.get("ticker")) in links_by_ticker
        matched_feature = feature_match(features_by_location.get(location, []), target_time)
        feature_generated = parse_dt(matched_feature.get("generated_at")) if matched_feature else None
        latest_generated = feature_max_generated.get(location)
        latest_feature_age_hours = None
        if latest_generated:
            latest_feature_age_hours = round((now - latest_generated).total_seconds() / 3600, 3)
        if has_link:
            blocker = "CURRENT_LINK_EXISTS"
        elif location == "unknown":
            blocker = "LOCATION_UNKNOWN"
        elif matched_feature is None:
            blocker = "FRESH_FEATURE_WINDOW_MISSING"
        else:
            blocker = "READY_FOR_R12_SAFE_LINK_PREVIEW"
        blocker_counts[blocker] += 1
        row = {{
            "ticker": market.get("ticker"),
            "series_ticker": series,
            "location_key": location,
            "status": market.get("status"),
            "target_time": iso(target_time),
            "has_weather_link": has_link,
            "matched_fresh_feature_id": matched_feature.get("id") if matched_feature else None,
            "matched_fresh_feature_target_time": matched_feature.get("target_time") if matched_feature else None,
            "matched_fresh_feature_generated_at": iso(feature_generated),
            "latest_location_feature_generated_at": iso(latest_generated),
            "latest_location_feature_age_hours": latest_feature_age_hours,
            "blocker": blocker,
            "title": market.get("title"),
        }}
        current_rows.append(row)
        series_stats[series]["current_market_rows"] += 1
        series_stats[series]["missing_link_rows"] += 0 if has_link else 1
        series_stats[series]["fresh_feature_window_missing_rows"] += 1 if blocker == "FRESH_FEATURE_WINDOW_MISSING" else 0
        series_stats[series]["ready_for_r12_preview_rows"] += 1 if blocker == "READY_FOR_R12_SAFE_LINK_PREVIEW" else 0
        series_stats[series]["linked_rows"] += 1 if has_link else 0
        series_samples.setdefault(series, row)
    series_rows = []
    for series, stats in series_stats.items():
        sample = series_samples.get(series) or {{}}
        series_rows.append({{
            "series_ticker": series,
            "location_key": sample.get("location_key"),
            "current_market_rows": stats["current_market_rows"],
            "missing_link_rows": stats["missing_link_rows"],
            "linked_rows": stats["linked_rows"],
            "fresh_feature_window_missing_rows": stats["fresh_feature_window_missing_rows"],
            "ready_for_r12_preview_rows": stats["ready_for_r12_preview_rows"],
            "sample_ticker": sample.get("ticker"),
            "max_target_time": max((row["target_time"] for row in current_rows if row["series_ticker"] == series), default=None),
        }})
    series_rows.sort(key=lambda row: (-int(row["current_market_rows"]), str(row["series_ticker"])))
    feature_windows = []
    for loc, count in feature_locations.most_common():
        generated = feature_max_generated.get(loc)
        feature_windows.append({{
            "location_key": loc,
            "feature_rows_sampled": count,
            "max_generated_at": iso(generated),
            "max_generated_age_hours": round((now - generated).total_seconds() / 3600, 3) if generated else None,
        }})
    payload["ok"] = True
    payload["current_weather_series"] = series_rows
    payload["linkability_rows"] = current_rows[:500]
    payload["feature_windows"] = feature_windows
    payload["summary"] = {{
        "active_weather_markets_sampled": len(markets),
        "current_weather_market_rows": len(current_rows),
        "current_series_count": len(series_rows),
        "missing_current_weather_link_rows": sum(1 for row in current_rows if not row["has_weather_link"]),
        "ready_for_r12_safe_link_preview_rows": blocker_counts["READY_FOR_R12_SAFE_LINK_PREVIEW"],
        "fresh_feature_window_missing_rows": blocker_counts["FRESH_FEATURE_WINDOW_MISSING"],
        "current_link_exists_rows": blocker_counts["CURRENT_LINK_EXISTS"],
        "location_unknown_rows": blocker_counts["LOCATION_UNKNOWN"],
        "blocker_counts": dict(sorted(blocker_counts.items())),
    }}
except Exception as exc:
    payload["error"] = str(exc)
print(json.dumps(payload, sort_keys=True))
"""
    return "python3 - <<'PY'\n" + script.strip() + "\nPY"


def _parse_probe_outputs(results: list[RemoteProbeResult]) -> dict[str, Any]:
    by_name = {result.name: result for result in results}
    writer = _json_from_probe(by_name.get("db_writer_monitor_raw"))
    preview = _json_from_probe(by_name.get("weather_activation_preview_json"))
    funnel = _json_from_probe(by_name.get("weather_funnel_json"))
    snapshot = _json_from_probe(by_name.get("weather_current_window_snapshot"))
    if not isinstance(writer, dict):
        writer = {}
    if not isinstance(preview, dict):
        preview = {}
    if not isinstance(funnel, dict):
        funnel = {}
    if not isinstance(snapshot, dict):
        snapshot = {}
    runner_script = _stdout(by_name.get("runner_script"))
    preview_summary = preview.get("summary") if isinstance(preview.get("summary"), dict) else {}
    funnel_summary = funnel.get("summary") if isinstance(funnel.get("summary"), dict) else {}
    return {
        "remote_time_utc": _first_line(_stdout(by_name.get("remote_time_utc"))),
        "scheduler_timer_active_state": _first_line(_stdout(by_name.get("scheduler_timer_active"))),
        "scheduler_service_active_state": _first_line(_stdout(by_name.get("scheduler_service_active"))),
        "scheduler_service_show": _parse_systemd_show(_stdout(by_name.get("scheduler_service_show"))),
        "writer_status": writer.get("status") or "UNKNOWN",
        "writer_safe_to_start_write": bool(writer.get("safe_to_start_write")) if writer else False,
        "runner_script": runner_script,
        "runner_has_feature_refresh": _runner_has_weather_feature_refresh(runner_script),
        "runner_has_weather_catalog_hook": WEATHER_CATALOG_JOB_ID in runner_script,
        "runner_has_weather_fast_lane": WEATHER_FAST_LANE_JOB_ID in runner_script,
        "r12_summary": preview_summary,
        "r2_weather_funnel_summary": funnel_summary,
        "weather_current_window_snapshot_ok": bool(snapshot.get("ok")),
        "weather_current_window_error": snapshot.get("error"),
        "weather_current_window_summary": snapshot.get("summary") or {},
        "current_weather_series": snapshot.get("current_weather_series") or [],
        "linkability_rows": snapshot.get("linkability_rows") or [],
        "feature_windows": snapshot.get("feature_windows") or [],
        "command_registry_ok": "COMMAND_REGISTRY_OK" in _stdout(by_name.get("command_registry")),
        "failed_probe_names": [result.name for result in results if not result.ok],
    }


def _recommended_weather_lane(series_rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not series_rows:
        return {"series_ticker": "KXTEMPNYCH", "location_key": "new_york", "source": "fallback"}
    sorted_rows = sorted(
        series_rows,
        key=lambda row: (
            -int(row.get("current_market_rows") or 0),
            -int(row.get("missing_link_rows") or 0),
            str(row.get("series_ticker") or ""),
        ),
    )
    row = sorted_rows[0]
    return {
        "series_ticker": row.get("series_ticker") or "KXTEMPNYCH",
        "location_key": row.get("location_key") or "new_york",
        "current_market_rows": row.get("current_market_rows"),
        "missing_link_rows": row.get("missing_link_rows"),
        "fresh_feature_window_missing_rows": row.get("fresh_feature_window_missing_rows"),
        "source": "current_weather_series",
    }


def _repaired_weather_hook_block(*, series_ticker: str, location_key: str) -> str:
    series = shlex.quote(series_ticker)
    location = shlex.quote(location_key)
    return "\n".join(
        [
            "# cadence_minutes=30 category=weather-catalog",
            (
                "run_job weather_current_catalog_refresh true bash -lc "
                "'set -euo pipefail; "
                f".venv/bin/kalshi-bot sync-markets --status open --limit 100 --max-pages 3 --series-ticker {series}; "
                ".venv/bin/kalshi-bot market-legs-parse --refresh --limit 1500; "
                f".venv/bin/kalshi-bot ingest-weather --location-key {location}; "
                f".venv/bin/kalshi-bot build-weather-features --location-key {location}; "
                ".venv/bin/kalshi-bot phase3az-r12-weather-activation-preview "
                "--output-dir reports/phase3az_r12_weather --limit 2000 "
                "--fresh-window-hours 24 --match-tolerance-hours 3'"
            ),
        ]
    )


def patch_runner_weather_feature_refresh(runner_script: str, *, repaired_block: str) -> str:
    if not runner_script.strip():
        return repaired_block + "\n"
    pattern = re.compile(
        r"(?ms)^# cadence_minutes=30 category=weather-catalog\n"
        r"run_job weather_current_catalog_refresh .*?"
        r"(?=\n# cadence_minutes=30 category=weather\nrun_job weather_fast_lane|\n# cadence_minutes=|$)"
    )
    if pattern.search(runner_script):
        patched = pattern.sub(repaired_block, runner_script, count=1)
    else:
        anchor = "# cadence_minutes=30 category=weather\nrun_job weather_fast_lane"
        if anchor in runner_script:
            patched = runner_script.replace(anchor, repaired_block + "\n" + anchor, 1)
        else:
            patched = runner_script.rstrip() + "\n\n" + repaired_block + "\n"
    return patched if patched.endswith("\n") else patched + "\n"


def _runner_has_weather_feature_refresh(runner_script: str, *, location_key: str | None = None) -> bool:
    if "ingest-weather --location-key" not in runner_script:
        return False
    if "build-weather-features --location-key" not in runner_script:
        return False
    if location_key and location_key not in runner_script:
        return False
    return True


def _linkability_checks(
    parsed: dict[str, Any],
    *,
    patched_runner: str,
    apply: bool,
    backup_first: bool,
) -> list[dict[str, Any]]:
    failed_probes = parsed.get("failed_probe_names") or []
    service_active = str(parsed.get("scheduler_service_active_state") or "")
    summary = parsed.get("weather_current_window_summary") or {}
    current_rows = int(summary.get("current_weather_market_rows") or 0)
    missing_links = int(summary.get("missing_current_weather_link_rows") or 0)
    stale_features = int(summary.get("fresh_feature_window_missing_rows") or 0)
    forbidden = sorted(
        {fragment for fragment in FORBIDDEN_REPAIR_FRAGMENTS if fragment in patched_runner.lower()}
    )
    checks = [
        _check("remote_probes_completed", not failed_probes, f"failed={','.join(failed_probes) if failed_probes else 'none'}."),
        _check("command_registry_ok", bool(parsed.get("command_registry_ok")), "Required weather commands are registered on the cloud host."),
        _check("weather_current_window_snapshot_ok", bool(parsed.get("weather_current_window_snapshot_ok")), f"error={parsed.get('weather_current_window_error')}."),
        _check("current_weather_series_discovered", bool(parsed.get("current_weather_series")), f"current_rows={current_rows}."),
        _check("current_weather_missing_links_recorded", missing_links >= 0, f"missing_current_weather_link_rows={missing_links}."),
        _check("linkability_blocker_is_explicit", current_rows > 0 and (missing_links > 0 or stale_features > 0), f"current_rows={current_rows} missing_links={missing_links} stale_features={stale_features}."),
        _check("runner_has_weather_catalog_hook", bool(parsed.get("runner_has_weather_catalog_hook")), "weather_current_catalog_refresh exists in the scheduler runner."),
        _check("runner_has_weather_fast_lane", bool(parsed.get("runner_has_weather_fast_lane")), "weather_fast_lane exists after the catalog hook."),
        _check("patched_runner_refreshes_features", _runner_has_weather_feature_refresh(patched_runner), "Patched hook refreshes weather source/features before R12 preview."),
        _check("patched_runner_has_no_forbidden_commands", not forbidden, f"forbidden={','.join(forbidden) if forbidden else 'none'}."),
    ]
    if apply:
        checks.append(_check("apply_requires_backup_first", backup_first, "Remote runner writes require --backup-first."))
        checks.append(
            _check(
                "scheduler_service_inactive_for_runner_write",
                service_active == "inactive",
                f"scheduler_service_active_state={service_active}.",
            )
        )
    return checks


def _decision(
    checks: list[dict[str, Any]],
    parsed: dict[str, Any],
    *,
    install_result: dict[str, Any],
    apply: bool,
    backup_first: bool,
    verify_after: bool,
) -> dict[str, Any]:
    failed = [row for row in checks if not row["passed"]]
    summary = parsed.get("weather_current_window_summary") or {}
    rows_ready = int(summary.get("ready_for_r12_safe_link_preview_rows") or 0)
    patch_required = bool(parsed.get("runner_patch_required"))
    if failed:
        status = "BLOCKED_WEATHER_CURRENT_WINDOW_LINKABILITY_REPAIR"
        reason = f"First failing check: {failed[0]['check']}."
        next_step = "Phase 3BB-R47 - Re-run Weather Current Window Discovery"
        command = (
            "kalshi-bot phase3bb-r47-weather-current-window-series-discovery-linkability-repair "
            "--output-dir reports/phase3bb_r47 --reports-dir reports"
        )
    elif rows_ready > 0:
        status = "WEATHER_LINK_GATE_READY_AFTER_FEATURE_REFRESH"
        reason = "Current weather windows have fresh feature evidence; run the existing R12 link gate/apply path."
        next_step = "Phase 3AZ-R12 - Weather Missing Link Apply"
        command = (
            "kalshi-bot db-writer-monitor --json\n"
            "kalshi-bot phase3az-r12-weather-missing-link-apply "
            "--output-dir reports/phase3az_r12_weather --limit 2000 "
            "--fresh-window-hours 24 --match-tolerance-hours 3 --max-records 25 "
            "--apply --backup-first"
        )
    elif install_result.get("ok") and verify_after:
        status = "WEATHER_FEATURE_REFRESH_HOOK_INSTALLED"
        reason = "The scheduler weather hook now refreshes source/features before R12 linkability preview."
        next_step = "Phase 3BB-R48 - Weather Feature Refresh Runtime Verification"
        command = (
            "kalshi-bot phase3bb-r48-weather-feature-refresh-runtime-verification "
            "--output-dir reports/phase3bb_r48 --reports-dir reports"
        )
    elif patch_required and not apply:
        status = "READY_TO_INSTALL_WEATHER_FEATURE_REFRESH_HOOK"
        reason = "Current weather windows exist, but fresh feature windows are stale/missing before R12 preview."
        next_step = "Phase 3BB-R47 - Apply Weather Feature Refresh Hook"
        command = (
            "kalshi-bot phase3bb-r47-weather-current-window-series-discovery-linkability-repair "
            "--output-dir reports/phase3bb_r47 --reports-dir reports "
            "--apply --backup-first"
        )
    elif not patch_required and verify_after:
        status = "WEATHER_FEATURE_REFRESH_HOOK_ALREADY_PRESENT"
        reason = "The scheduler hook already refreshes weather source/features before R12 preview."
        next_step = "Phase 3BB-R48 - Weather Feature Refresh Runtime Verification"
        command = (
            "kalshi-bot phase3bb-r48-weather-feature-refresh-runtime-verification "
            "--output-dir reports/phase3bb_r48 --reports-dir reports"
        )
    else:
        status = "WEATHER_LINKABILITY_REPAIR_NOT_INSTALLED"
        reason = "The hook was not installed and no immediate safe link rows are present."
        next_step = "Phase 3BB-R47 - Inspect Linkability Repair"
        command = (
            "kalshi-bot phase3bb-r47-weather-current-window-series-discovery-linkability-repair "
            "--output-dir reports/phase3bb_r47 --reports-dir reports"
        )
    return {
        "status": status,
        "review_passed": not failed,
        "failed_check_count": len(failed),
        "first_failed_check": failed[0]["check"] if failed else None,
        "primary_reason": reason,
        "apply_requested": apply,
        "backup_first": backup_first,
        "runner_patch_required": patch_required,
        "runner_repaired_before": parsed.get("runner_has_feature_refresh"),
        "runner_repaired_after": verify_after,
        "install_attempted": install_result.get("attempted"),
        "recommended_series_ticker": parsed.get("recommended_series_ticker"),
        "recommended_location_key": parsed.get("recommended_location_key"),
        "current_weather_market_rows": summary.get("current_weather_market_rows"),
        "missing_current_weather_link_rows": summary.get("missing_current_weather_link_rows"),
        "fresh_feature_window_missing_rows": summary.get("fresh_feature_window_missing_rows"),
        "ready_for_r12_safe_link_preview_rows": summary.get("ready_for_r12_safe_link_preview_rows"),
        "will_create_paper_trades": False,
        "will_submit_live_or_demo_orders": False,
        "operator_next_command": command,
        "next_codex_step": next_step,
    }


def _can_apply(checks: list[dict[str, Any]], parsed: dict[str, Any]) -> bool:
    return all(row["passed"] for row in checks) and bool(parsed.get("runner_patch_required"))


def _build_install_probe(
    target: CloudBootstrapTarget,
    *,
    patched_runner: str,
    backup_first: bool,
    timeout_seconds: int,
) -> RemoteProbe:
    encoded = base64.b64encode(patched_runner.encode("utf-8")).decode("ascii")
    script = f"""
import base64
import os
import pathlib
import shutil
import time

runner = pathlib.Path({str(_runner_path(target))!r})
tmp = runner.with_name(runner.name + ".phase3bb_r47.tmp")
backup = runner.with_name(runner.name + ".phase3bb_r47_" + time.strftime("%Y%m%d%H%M%S") + ".bak")
tmp.write_text(base64.b64decode({encoded!r}).decode("utf-8"), encoding="utf-8")
tmp.chmod(0o755)
if {bool(backup_first)!r} and runner.exists():
    shutil.copy2(runner, backup)
os.replace(tmp, runner)
runner.chmod(0o755)
print("INSTALLED_R47_WEATHER_FEATURE_REFRESH_HOOK")
print(f"runner={{runner}}")
print(f"backup={{backup if backup.exists() else ''}}")
"""
    command = "python3 - <<'PY'\n" + script.strip() + "\nPY"
    return RemoteProbe("install_weather_feature_refresh_hook", command, timeout_seconds)


def _runner_path(target: CloudBootstrapTarget) -> Path:
    return Path(target.app_path) / "scripts" / RUNNER_SCRIPT_NAME


def _parse_systemd_show(text: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for line in text.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        parsed[key] = value
    return parsed


def _check(name: str, passed: bool, detail: str) -> dict[str, Any]:
    return {"check": name, "passed": bool(passed), "detail": detail}


def _write_rows_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    keys: list[str] = []
    for row in rows:
        for key in row:
            if key not in keys:
                keys.append(key)
    with path.open("w", newline="", encoding="utf-8") as handle:
        if not keys:
            handle.write("")
            return
        writer = csv.DictWriter(handle, fieldnames=keys, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _render_executive_summary(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# Phase 3BB-R47 Weather Current Window Series Discovery And Linkability Repair")
    decision = payload["linkability_decision"]
    parsed = payload["parsed_linkability_state"]
    lines.extend(
        [
            "",
            "## Result",
            "",
            f"- Status: `{decision['status']}`",
            f"- Reason: {decision['primary_reason']}",
            f"- Recommended series: `{decision['recommended_series_ticker']}`",
            f"- Recommended location: `{decision['recommended_location_key']}`",
            f"- Current weather rows: `{decision['current_weather_market_rows']}`",
            f"- Missing current weather links: `{decision['missing_current_weather_link_rows']}`",
            f"- Fresh feature window missing rows: `{decision['fresh_feature_window_missing_rows']}`",
            f"- Ready for R12 safe-link preview rows: `{decision['ready_for_r12_safe_link_preview_rows']}`",
            f"- Runner feature refresh before: `{decision['runner_repaired_before']}`",
            f"- Runner feature refresh after: `{decision['runner_repaired_after']}`",
            f"- Runner patch required: `{decision['runner_patch_required']}`",
            f"- Apply requested: `{decision['apply_requested']}`",
            f"- Scheduler service: `{parsed.get('scheduler_service_active_state')}`",
            f"- Scheduler timer: `{parsed.get('scheduler_timer_active_state')}`",
            f"- Writer status: `{parsed.get('writer_status')}`",
            "",
            "## Safety",
            "",
            "- Paper trade creation: `False`",
            "- Live/demo order submission/cancel/replace: `False`",
            "- Scheduler service/timer start or stop by this phase: `0`",
            "- Refresh jobs run directly by this phase: `0`",
            "- Remote DB writes by this phase: `0`",
            "",
            "## Next",
            "",
            f"- Next Codex step: {decision['next_codex_step']}",
            "",
            "```bash",
            decision["operator_next_command"],
            "```",
        ]
    )
    return "\n".join(lines) + "\n"


def _render_markdown(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# Phase 3BB-R47 Linkability Detail")
    decision = payload["linkability_decision"]
    lines.extend(
        [
            "",
            "## Decision",
            "",
            f"- Status: `{decision['status']}`",
            f"- Primary reason: {decision['primary_reason']}",
            "",
            "## Checks",
            "",
            "| Check | Passed | Detail |",
            "|---|---:|---|",
        ]
    )
    for row in payload["linkability_checks"]:
        lines.append(f"| `{row['check']}` | `{row['passed']}` | {row['detail']} |")
    lines.extend(["", "## Current Weather Series", "", "| Series | Location | Current Rows | Missing Links | Stale Features | Ready Rows | Sample |", "|---|---|---:|---:|---:|---:|---|"])
    for row in payload["current_weather_series"]:
        lines.append(
            "| {series_ticker} | {location_key} | {current_market_rows} | {missing_link_rows} | "
            "{fresh_feature_window_missing_rows} | {ready_for_r12_preview_rows} | {sample_ticker} |".format(**row)
        )
    lines.extend(
        [
            "",
            "## Repaired Weather Hook",
            "",
            "```bash",
            payload["repaired_weather_hook_block"],
            "```",
        ]
    )
    return "\n".join(lines) + "\n"


def _render_operator_command(payload: dict[str, Any]) -> str:
    command = payload["linkability_decision"]["operator_next_command"]
    return "#!/usr/bin/env bash\nset -euo pipefail\n" + command + "\n"


def _render_next_actions(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# Phase 3BB-R47 Next Actions")
    decision = payload["linkability_decision"]
    lines.extend(
        [
            "",
            "## Next Operator Action",
            "",
            f"- Status: `{decision['status']}`",
            f"- Reason: {decision['primary_reason']}",
            "",
            "```bash",
            decision["operator_next_command"],
            "```",
            "",
            f"- Next Codex step: {decision['next_codex_step']}",
            "",
            "## Do Not Run",
            "",
            "- Do not start duplicate R5 watchers.",
            "- Do not create paper trades.",
            "- Do not submit/cancel/replace live or demo orders.",
            "- Do not run weather forecasts until R12 linkability opens and links are actually written.",
        ]
    )
    return "\n".join(lines) + "\n"
