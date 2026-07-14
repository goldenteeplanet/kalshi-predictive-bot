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
from kalshi_predictor.phase3bb_r36_cloud_scheduler_install_handoff import (
    SCHEDULER_SERVICE_NAME,
    SCHEDULER_TIMER_NAME,
)
from kalshi_predictor.phase3bb_r44_weather_catalog_hook_runtime_verification import (
    _first_line,
    _parse_report_stats,
    _stdout,
    _target_payload,
)
from kalshi_predictor.utils.time import utc_now

PHASE3BB_R45_VERSION = "phase3bb_r45_weather_freshness_to_ranking_impact_v1"
DEFAULT_OUTPUT_DIR = Path("reports/phase3bb_r45")
DEFAULT_REPORTS_DIR = Path("reports")
DEFAULT_PER_PROBE_TIMEOUT_SECONDS = 45

WEATHER_IMPACT_REPORT_PATHS = (
    "reports/phase3az_r12_weather/weather_activation_preview.json",
    "reports/phase3az_r12_weather/weather_activation_preview.md",
    "reports/phase3az_r12_weather/weather_activation_candidates.csv",
    "reports/phase3az_r12_weather/safe_to_link.csv",
    "reports/phase3az_r12_weather/safe_to_relink.csv",
    "reports/phase3bb_r2/weather_funnel.json",
    "reports/phase3bb_r2/weather_fast_lane.md",
    "reports/phase3bb_r2/weather_candidates.csv",
    "reports/phase3bb_r44/weather_catalog_hook_runtime_verification.json",
    "reports/phase3bb_r40/cloud_scheduler_runtime_monitor.json",
)


@dataclass(frozen=True)
class Phase3BBR45WeatherFreshnessToRankingImpactArtifacts:
    output_dir: Path
    executive_summary_path: Path
    markdown_path: Path
    json_path: Path
    probe_csv_path: Path
    checks_csv_path: Path
    blocker_counts_csv_path: Path
    freshness_rows_csv_path: Path
    operator_command_path: Path
    next_actions_path: Path
    manifest_path: Path


def write_phase3bb_r45_weather_freshness_to_ranking_impact_report(
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
    per_probe_timeout_seconds: int = DEFAULT_PER_PROBE_TIMEOUT_SECONDS,
    probe_runner: ProbeRunner | None = None,
) -> Phase3BBR45WeatherFreshnessToRankingImpactArtifacts:
    payload = build_phase3bb_r45_weather_freshness_to_ranking_impact(
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
        per_probe_timeout_seconds=per_probe_timeout_seconds,
        probe_runner=probe_runner,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    executive_summary_path = output_dir / "EXECUTIVE_SUMMARY.md"
    markdown_path = output_dir / "weather_freshness_to_ranking_impact.md"
    json_path = output_dir / "weather_freshness_to_ranking_impact.json"
    probe_csv_path = output_dir / "remote_probe_results.csv"
    checks_csv_path = output_dir / "impact_checks.csv"
    blocker_counts_csv_path = output_dir / "weather_blocker_counts.csv"
    freshness_rows_csv_path = output_dir / "weather_freshness_rows.csv"
    operator_command_path = output_dir / "operator_next_command.sh"
    next_actions_path = output_dir / "NEXT_ACTIONS.md"
    manifest_path = output_dir / "MANIFEST.sha256"

    executive_summary_path.write_text(_render_executive_summary(payload), encoding="utf-8")
    markdown_path.write_text(_render_markdown(payload), encoding="utf-8")
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")
    _write_probe_csv(probe_csv_path, payload["remote_probe_results"])
    _write_rows_csv(checks_csv_path, payload["impact_checks"])
    _write_rows_csv(blocker_counts_csv_path, payload["weather_blocker_counts"])
    _write_rows_csv(freshness_rows_csv_path, payload["weather_freshness_rows"])
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
            blocker_counts_csv_path,
            freshness_rows_csv_path,
            operator_command_path,
            next_actions_path,
        ],
    )
    return Phase3BBR45WeatherFreshnessToRankingImpactArtifacts(
        output_dir=output_dir,
        executive_summary_path=executive_summary_path,
        markdown_path=markdown_path,
        json_path=json_path,
        probe_csv_path=probe_csv_path,
        checks_csv_path=checks_csv_path,
        blocker_counts_csv_path=blocker_counts_csv_path,
        freshness_rows_csv_path=freshness_rows_csv_path,
        operator_command_path=operator_command_path,
        next_actions_path=next_actions_path,
        manifest_path=manifest_path,
    )


def build_phase3bb_r45_weather_freshness_to_ranking_impact(
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
        "command": "kalshi-bot phase3bb-r45-weather-freshness-to-ranking-impact",
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
        timeout_seconds=per_probe_timeout_seconds,
    )
    runner = probe_runner or _run_ssh_probe
    results = [runner(probe, target) for probe in probes]
    local_r44_payload = _read_json(
        reports_dir / "phase3bb_r44" / "weather_catalog_hook_runtime_verification.json"
    )
    local_r40_payload = _read_json(reports_dir / "phase3bb_r40" / "cloud_scheduler_runtime_monitor.json")
    parsed = _parse_probe_outputs(
        results,
        local_r44_payload=local_r44_payload,
        local_r40_payload=local_r40_payload,
    )
    blocker_counts = _weather_blocker_counts(parsed)
    freshness_rows = _weather_freshness_rows(parsed)
    checks = _impact_checks(parsed)
    decision = _decision(checks, parsed)
    safety = {
        **_safety_flags(),
        "paper_only": True,
        "diagnostic_only": True,
        "freshness_to_ranking_review_only": True,
        "ssh_read_only_commands_executed": len(probes),
        "systemctl_mutating_commands_executed": 0,
        "scheduler_files_written_to_system": False,
        "scheduler_timer_started": False,
        "scheduler_service_started": False,
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
        "secrets_printed": False,
    }
    return {
        **metadata,
        "phase": "3BB-R45-WEATHER-FRESHNESS-TO-RANKING-IMPACT-REVIEW",
        "phase_version": PHASE3BB_R45_VERSION,
        "mode": "PAPER_READ_ONLY_WEATHER_FRESHNESS_TO_RANKING_IMPACT_REVIEW",
        "reports_dir": str(reports_dir),
        "r11_context_available": bool(r11_context),
        "cloud_target": _target_payload(target),
        "scheduler_service_name": scheduler_service_name,
        "scheduler_timer_name": scheduler_timer_name,
        "remote_probe_results": [_result_payload(result) for result in results],
        "parsed_impact_state": parsed,
        "impact_checks": checks,
        "weather_blocker_counts": blocker_counts,
        "weather_freshness_rows": freshness_rows,
        "impact_decision": decision,
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
    timeout_seconds: int,
) -> list[RemoteProbe]:
    app = shlex.quote(target.app_path)
    env = shlex.quote(target.env_path)
    service = shlex.quote(scheduler_service_name)
    timer = shlex.quote(scheduler_timer_name)
    report_list = " ".join(shlex.quote(path) for path in WEATHER_IMPACT_REPORT_PATHS)
    writer_cmd = f"cd {app} && set -a && . {env} && set +a && .venv/bin/kalshi-bot db-writer-monitor --json"
    return [
        RemoteProbe("remote_time_utc", "date -u +%Y-%m-%dT%H:%M:%SZ", timeout_seconds),
        RemoteProbe("scheduler_timer_active", f"systemctl is-active {timer} || true", timeout_seconds),
        RemoteProbe("scheduler_service_active", f"systemctl is-active {service} || true", timeout_seconds),
        RemoteProbe("scheduler_journal_tail", f"journalctl -u {service} -n 140 --no-pager || true", timeout_seconds),
        RemoteProbe("db_writer_monitor_raw", writer_cmd, timeout_seconds),
        RemoteProbe(
            "weather_report_stats",
            (
                f"cd {app} && for p in {report_list}; do "
                "if [ -e \"$p\" ]; then stat -c '%n|%Y|%s' \"$p\"; "
                "else echo \"$p|MISSING|0\"; fi; done"
            ),
            timeout_seconds,
        ),
        RemoteProbe(
            "weather_activation_preview_json",
            f"cd {app} && cat reports/phase3az_r12_weather/weather_activation_preview.json 2>/dev/null || true",
            timeout_seconds,
        ),
        RemoteProbe(
            "weather_funnel_json",
            f"cd {app} && cat reports/phase3bb_r2/weather_funnel.json 2>/dev/null || true",
            timeout_seconds,
        ),
        RemoteProbe(
            "r44_json",
            f"cd {app} && cat reports/phase3bb_r44/weather_catalog_hook_runtime_verification.json 2>/dev/null || true",
            timeout_seconds,
        ),
        RemoteProbe(
            "r40_json",
            f"cd {app} && cat reports/phase3bb_r40/cloud_scheduler_runtime_monitor.json 2>/dev/null || true",
            timeout_seconds,
        ),
        RemoteProbe("weather_db_snapshot", _weather_db_snapshot_command(target.db_path), timeout_seconds),
        RemoteProbe(
            "command_registry",
            (
                f"cd {app} && for cmd in "
                "phase3bb-r40-cloud-scheduler-runtime-monitor "
                "phase3bb-r44-weather-catalog-hook-runtime-verification "
                "phase3bb-r46-cloud-scheduler-weather-writer-gate-repair "
                "phase3bb-r47-weather-current-window-series-discovery-linkability-repair "
                "phase3bb-r48-weather-feature-refresh-runtime-verification "
                "phase3bb-r49-weather-missing-link-apply-after-feature-refresh "
                "phase3bb-r50-weather-post-link-ranking-fast-lane-recheck "
                "phase3bb-r51-weather-ranking-path-repair "
                "phase3bb-r52-weather-ev-fair-value-diagnostic "
                "phase3az-r12-weather-activation-preview "
                "phase3bb-r2-weather-fast-lane; do "
                ".venv/bin/kalshi-bot \"$cmd\" --help >/dev/null || exit 30; "
                "done; echo COMMAND_REGISTRY_OK"
            ),
            timeout_seconds,
        ),
    ]


def _weather_db_snapshot_command(db_path: str) -> str:
    script = f"""
import json
import sqlite3
from collections import Counter
from datetime import datetime, timedelta, timezone

db_path = {db_path!r}
now = datetime.now(timezone.utc)
current_since = now - timedelta(hours=3)

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

def table_columns(conn, name):
    return [row[1] for row in conn.execute(f"PRAGMA table_info({{name}})").fetchall()]

def fetch_table(conn, name, wanted, order_by, limit):
    try:
        columns = table_columns(conn, name)
    except sqlite3.Error:
        return []
    selected = [column for column in wanted if column in columns]
    if not selected:
        return []
    order = order_by if order_by in selected else selected[0]
    sql = f"select {{', '.join(selected)}} from {{name}} order by {{order}} desc limit ?"
    return [dict(zip(selected, row)) for row in conn.execute(sql, (limit,)).fetchall()]

payload = {{
    "db_path": db_path,
    "snapshot_generated_at": iso(now),
    "current_since": iso(current_since),
    "ok": False,
    "error": None,
    "weather_market_links": {{}},
    "weather_features": {{}},
}}
try:
    conn = sqlite3.connect(f"file:{{db_path}}?mode=ro", uri=True)
    links = fetch_table(
        conn,
        "weather_market_links",
        ["ticker", "location_key", "target_time", "detected_at", "confidence"],
        "target_time",
        5000,
    )
    features = fetch_table(
        conn,
        "weather_features",
        ["id", "location_key", "target_time", "generated_at"],
        "generated_at",
        5000,
    )
    parsed_links = [(row, parse_dt(row.get("target_time"))) for row in links]
    parsed_features = [(row, parse_dt(row.get("target_time")), parse_dt(row.get("generated_at"))) for row in features]
    link_locations = Counter(str(row.get("location_key") or "") for row in links)
    feature_locations = Counter(str(row.get("location_key") or "") for row in features)
    payload["ok"] = True
    payload["weather_market_links"] = {{
        "rows_sampled": len(links),
        "target_time_ge_now_minus_3h": sum(1 for _, dt in parsed_links if dt and dt >= current_since),
        "target_time_ge_now": sum(1 for _, dt in parsed_links if dt and dt >= now),
        "min_target_time": iso(min((dt for _, dt in parsed_links if dt), default=None)),
        "max_target_time": iso(max((dt for _, dt in parsed_links if dt), default=None)),
        "dominant_location_key": link_locations.most_common(1)[0][0] if link_locations else None,
        "dominant_location_count": link_locations.most_common(1)[0][1] if link_locations else 0,
        "sample_rows": links[:25],
    }}
    payload["weather_features"] = {{
        "rows_sampled": len(features),
        "target_time_ge_now_minus_3h": sum(1 for _, target, _ in parsed_features if target and target >= current_since),
        "generated_at_ge_now_minus_24h": sum(1 for _, _, generated in parsed_features if generated and generated >= now - timedelta(hours=24)),
        "min_target_time": iso(min((target for _, target, _ in parsed_features if target), default=None)),
        "max_target_time": iso(max((target for _, target, _ in parsed_features if target), default=None)),
        "max_generated_at": iso(max((generated for _, _, generated in parsed_features if generated), default=None)),
        "dominant_location_key": feature_locations.most_common(1)[0][0] if feature_locations else None,
        "dominant_location_count": feature_locations.most_common(1)[0][1] if feature_locations else 0,
        "sample_rows": features[:25],
    }}
except Exception as exc:
    payload["error"] = str(exc)
print(json.dumps(payload, sort_keys=True))
"""
    return "python3 - <<'PY'\n" + script.strip() + "\nPY"


def _parse_probe_outputs(
    results: list[RemoteProbeResult],
    *,
    local_r44_payload: dict[str, Any] | None = None,
    local_r40_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    by_name = {result.name: result for result in results}
    preview_payload = _json_from_probe(by_name.get("weather_activation_preview_json"))
    funnel_payload = _json_from_probe(by_name.get("weather_funnel_json"))
    remote_r44_payload = _json_from_probe(by_name.get("r44_json"))
    remote_r40_payload = _json_from_probe(by_name.get("r40_json"))
    db_snapshot = _json_from_probe(by_name.get("weather_db_snapshot"))
    writer = _json_from_probe(by_name.get("db_writer_monitor_raw"))
    scheduler_journal = _stdout(by_name.get("scheduler_journal_tail"))
    r44_payload = local_r44_payload if local_r44_payload else remote_r44_payload
    r40_payload = local_r40_payload if local_r40_payload else remote_r40_payload
    preview_summary = preview_payload.get("summary") if isinstance(preview_payload, dict) else {}
    if not isinstance(preview_summary, dict):
        preview_summary = {}
    candidate_rows = preview_payload.get("candidate_rows") if isinstance(preview_payload, dict) else []
    if not isinstance(candidate_rows, list):
        candidate_rows = []
    funnel_summary = funnel_payload.get("summary") if isinstance(funnel_payload, dict) else {}
    if not isinstance(funnel_summary, dict):
        funnel_summary = {}
    r44_decision = r44_payload.get("hook_runtime_decision") if isinstance(r44_payload, dict) else {}
    if not isinstance(r44_decision, dict):
        r44_decision = {}
    r44_parsed = r44_payload.get("parsed_hook_runtime_state") if isinstance(r44_payload, dict) else {}
    if not isinstance(r44_parsed, dict):
        r44_parsed = {}
    r40_parsed = r40_payload.get("parsed_runtime_state") if isinstance(r40_payload, dict) else {}
    if not isinstance(r40_parsed, dict):
        r40_parsed = {}
    if not isinstance(db_snapshot, dict):
        db_snapshot = {}
    if not isinstance(writer, dict):
        writer = {}
    report_freshness = _parse_report_stats(_stdout(by_name.get("weather_report_stats")))
    blocker_counter = Counter(str(row.get("blocker") or "UNKNOWN") for row in candidate_rows)
    freshness_counter = Counter(
        _freshness_bucket(row)
        for row in candidate_rows
        if isinstance(row, dict)
    )
    current_linkable_rows = [row for row in candidate_rows if bool(row.get("current_linkable_weather_ticker"))]
    return {
        "remote_time_utc": _first_line(_stdout(by_name.get("remote_time_utc"))),
        "scheduler_timer_active_state": _first_line(_stdout(by_name.get("scheduler_timer_active"))),
        "scheduler_service_active_state": _first_line(_stdout(by_name.get("scheduler_service_active"))),
        "scheduler_last_failure_reason": _scheduler_failure_reason(scheduler_journal),
        "scheduler_busy_writer_seen": "Status: BUSY_WRITER" in scheduler_journal
        or "Database is busy" in scheduler_journal,
        "writer_status": writer.get("status") or "UNKNOWN",
        "writer_safe_to_start_write": bool(writer.get("safe_to_start_write")) if writer else False,
        "weather_report_freshness": report_freshness,
        "weather_activation_preview_json_ok": bool(preview_payload),
        "weather_activation_preview_summary": preview_summary,
        "weather_activation_candidate_rows": len(candidate_rows),
        "weather_activation_blocker_counts": dict(sorted(blocker_counter.items())),
        "weather_activation_freshness_buckets": dict(sorted(freshness_counter.items())),
        "weather_activation_current_linkable_rows": len(current_linkable_rows),
        "weather_activation_current_linkable_sample": current_linkable_rows[:10],
        "weather_funnel_json_ok": bool(funnel_payload),
        "weather_funnel_status": funnel_payload.get("status") if isinstance(funnel_payload, dict) else None,
        "weather_funnel_summary": funnel_summary,
        "r44_json_available": bool(r44_payload),
        "r44_source": "LOCAL_REPORTS_DIR" if local_r44_payload else "REMOTE_REPORTS_DIR",
        "r44_status": r44_decision.get("status"),
        "r44_verification_passed": bool(r44_decision.get("verification_passed")),
        "r44_catalog_hook_run_count": r44_decision.get("weather_catalog_hook_run_count"),
        "r44_weather_fast_lane_run_count": r44_decision.get("weather_fast_lane_run_count"),
        "r44_weather_catalog_sequence": r44_decision.get("weather_catalog_sequence"),
        "r44_weather_funnel_status": r44_decision.get("weather_funnel_status"),
        "r44_scheduler_timer_active_state": r44_parsed.get("scheduler_timer_active_state"),
        "r40_json_available": bool(r40_payload),
        "r40_source": "LOCAL_REPORTS_DIR" if local_r40_payload else "REMOTE_REPORTS_DIR",
        "r40_status": r40_payload.get("runtime_decision", {}).get("status") if isinstance(r40_payload, dict) else None,
        "r40_weather_catalog_hook_job_run_count": r40_parsed.get("weather_catalog_hook_job_run_count"),
        "r40_weather_fast_lane_job_run_count": r40_parsed.get("weather_fast_lane_job_run_count"),
        "r40_weather_catalog_runtime_order_ok": r40_parsed.get("weather_catalog_runtime_order_ok"),
        "weather_db_snapshot_ok": bool(db_snapshot.get("ok")),
        "weather_db_snapshot": db_snapshot,
        "command_registry_ok": bool(by_name.get("command_registry") and by_name["command_registry"].ok),
    }


def _freshness_bucket(row: dict[str, Any]) -> str:
    if bool(row.get("safe_to_link")):
        return "SAFE_TO_LINK"
    if bool(row.get("safe_to_relink")):
        return "SAFE_TO_RELINK"
    if bool(row.get("stale_target_time_link")):
        return "STALE_TARGET_TIME_LINK"
    if bool(row.get("current_linkable_weather_ticker")):
        return "CURRENT_LINKABLE"
    if not row.get("has_existing_link"):
        return "MISSING_EXISTING_LINK"
    return str(row.get("blocker") or "OTHER")


def _scheduler_failure_reason(journal_text: str) -> str:
    if "Status: BUSY_WRITER" in journal_text or "Database is busy" in journal_text:
        return "BUSY_WRITER_DURING_SCHEDULER_RUN"
    if "Failed with result" in journal_text or "status=75" in journal_text:
        return "SCHEDULER_SERVICE_EXITED_NONZERO"
    return ""


def _impact_checks(parsed: dict[str, Any]) -> list[dict[str, Any]]:
    preview_summary = parsed.get("weather_activation_preview_summary") or {}
    funnel_summary = parsed.get("weather_funnel_summary") or {}
    db_links = (parsed.get("weather_db_snapshot") or {}).get("weather_market_links") or {}
    return [
        _check("scheduler_timer_active", parsed.get("scheduler_timer_active_state") == "active", f"timer={parsed.get('scheduler_timer_active_state')}."),
        _check("scheduler_service_state_valid", parsed.get("scheduler_service_active_state") in {"active", "activating", "inactive"}, f"service={parsed.get('scheduler_service_active_state')}."),
        _check("r44_catalog_hook_verified", bool(parsed.get("r44_verification_passed")), f"r44_status={parsed.get('r44_status')}."),
        _check("r44_catalog_before_fast_lane", parsed.get("r44_weather_catalog_sequence") == "CATALOG_THEN_FAST_LANE_VERIFIED", f"sequence={parsed.get('r44_weather_catalog_sequence')}."),
        _check("r12_preview_available", bool(parsed.get("weather_activation_preview_json_ok")), "R12 preview JSON exists and parses."),
        _check("weather_fast_lane_available", bool(parsed.get("weather_funnel_json_ok")), "Weather fast-lane funnel JSON exists and parses."),
        _check("command_registry_ok", bool(parsed.get("command_registry_ok")), "R40/R44/R12/R2 CLI help is registered on the cloud host."),
        _check("db_snapshot_readable", bool(parsed.get("weather_db_snapshot_ok")), "Remote weather link/feature DB snapshot is readable in SQLite read-only mode."),
        _check(
            "safe_link_gate_closed_or_explicit",
            int(preview_summary.get("rows_safe_to_link") or 0) >= 0
            and int(preview_summary.get("rows_safe_to_relink") or 0) >= 0,
            f"rows_safe_to_link={preview_summary.get('rows_safe_to_link')} rows_safe_to_relink={preview_summary.get('rows_safe_to_relink')}.",
        ),
        _check(
            "ranking_impact_explicit",
            funnel_summary.get("current_weather_rows") is not None
            or parsed.get("weather_funnel_status") is not None,
            f"funnel_status={parsed.get('weather_funnel_status')} current_weather_rows={funnel_summary.get('current_weather_rows')}.",
        ),
        _check(
            "current_link_count_recorded",
            db_links.get("target_time_ge_now_minus_3h") is not None,
            f"db_current_links={db_links.get('target_time_ge_now_minus_3h')}.",
        ),
    ]


def _decision(checks: list[dict[str, Any]], parsed: dict[str, Any]) -> dict[str, Any]:
    failed = [row for row in checks if not row["passed"]]
    preview_summary = parsed.get("weather_activation_preview_summary") or {}
    funnel_summary = parsed.get("weather_funnel_summary") or {}
    db_links = (parsed.get("weather_db_snapshot") or {}).get("weather_market_links") or {}
    rows_safe_to_link = int(preview_summary.get("rows_safe_to_link") or 0)
    rows_safe_to_relink = int(preview_summary.get("rows_safe_to_relink") or 0)
    current_linkable = int(preview_summary.get("current_linkable_weather_tickers") or 0)
    current_weather_rows = _int_or_none(funnel_summary.get("current_weather_rows"))
    ranking_rows = _int_or_none(funnel_summary.get("ranking_rows"))
    db_current_links = _int_or_none(db_links.get("target_time_ge_now_minus_3h"))

    if failed:
        if failed[0]["check"] == "scheduler_service_state_valid" and parsed.get("scheduler_busy_writer_seen"):
            status = "BLOCKED_SCHEDULER_WRITER_GATE_FAILURE"
            reason = (
                "The last scheduler run failed because a write-capable weather catalog step hit BUSY_WRITER "
                "while R5/UI held SQLite."
            )
            next_step = "Phase 3BB-R46 - Cloud Scheduler Weather Writer-Gate Failure Repair"
            command = (
                "kalshi-bot phase3bb-r40-cloud-scheduler-runtime-monitor "
                "--output-dir reports/phase3bb_r40 --reports-dir reports\n"
                "kalshi-bot phase3bb-r45-weather-freshness-to-ranking-impact "
                "--output-dir reports/phase3bb_r45 --reports-dir reports"
            )
            first_blocker = "SCHEDULER_BUSY_WRITER_EXIT_75"
        else:
            status = "BLOCKED_WEATHER_FRESHNESS_IMPACT_REVIEW"
            reason = f"First failing check: {failed[0]['check']}."
            next_step = "Phase 3BB-R45 - Re-run Weather Freshness To Ranking Impact Review"
            command = (
                "kalshi-bot phase3bb-r45-weather-freshness-to-ranking-impact "
                "--output-dir reports/phase3bb_r45 --reports-dir reports"
            )
            first_blocker = failed[0]["check"].upper()
    elif rows_safe_to_link > 0 or rows_safe_to_relink > 0:
        status = "WEATHER_LINK_GATE_READY"
        reason = "R12 found safe weather link/relink rows; ranking impact should wait for a writer-gated link apply."
        next_step = "Phase 3BB-R46 - Weather Safe Link Apply And Ranking Impact Recheck"
        command = (
            "kalshi-bot db-writer-monitor --json\n"
            "kalshi-bot phase3az-r12-weather-missing-link-apply "
            "--output-dir reports/phase3az_r12_weather --limit 2000 "
            "--fresh-window-hours 24 --match-tolerance-hours 3 "
            "--max-records 25 --apply --backup-first\n"
            "kalshi-bot phase3bb-r45-weather-freshness-to-ranking-impact "
            "--output-dir reports/phase3bb_r45 --reports-dir reports"
        )
        first_blocker = "SAFE_LINK_WRITE_GATE_READY"
    elif current_weather_rows == 0 or parsed.get("weather_funnel_status") == "NO_CURRENT_WEATHER_ROWS":
        status = "WEATHER_REFRESH_DID_NOT_CREATE_RANKABLE_CURRENT_LINKS"
        reason = (
            "The scheduler hook refreshed the weather catalog and R12 preview, but the fast-lane still "
            "has zero current weather rows to rank."
        )
        next_step = "Phase 3BB-R47 - Weather Current Window Series Discovery And Linkability Repair"
        command = (
            "kalshi-bot phase3bb-r47-weather-current-window-series-discovery-linkability-repair "
            "--output-dir reports/phase3bb_r47 --reports-dir reports"
        )
        first_blocker = "NO_CURRENT_WEATHER_LINKS_AFTER_CATALOG_REFRESH"
    elif ranking_rows == 0:
        status = "WEATHER_CURRENT_ROWS_NEED_RANKING"
        reason = "Current weather rows exist, but no current weather rankings were produced."
        next_step = "Phase 3BB-R46 - Weather Ranking Path Repair"
        command = (
            "kalshi-bot phase3bb-r2-weather-fast-lane "
            "--output-dir reports/phase3bb_r2 --reports-dir reports"
        )
        first_blocker = "RANKING_MISSING"
    else:
        status = "WEATHER_RANKING_IMPACT_PRESENT"
        reason = "Weather current rows and rankings are present; refresh the unified paper gate next."
        next_step = "Phase 3BB-R8 - Unified Paper Gate Across Categories"
        command = (
            "kalshi-bot phase3bb-r8-unified-paper-gate "
            "--output-dir reports/phase3bb_r8 --reports-dir reports"
        )
        first_blocker = "PAPER_GATE_REFRESH_NEEDED"

    return {
        "status": status,
        "review_passed": not failed,
        "failed_check_count": len(failed),
        "first_failed_check": failed[0]["check"] if failed else None,
        "primary_reason": reason,
        "first_weather_blocker": first_blocker,
        "rows_safe_to_link": rows_safe_to_link,
        "rows_safe_to_relink": rows_safe_to_relink,
        "current_linkable_weather_tickers": current_linkable,
        "fast_lane_status": parsed.get("weather_funnel_status"),
        "current_weather_rows": current_weather_rows,
        "ranking_rows": ranking_rows,
        "db_current_weather_links": db_current_links,
        "db_future_weather_links": _int_or_none(db_links.get("target_time_ge_now")),
        "will_create_paper_trades": False,
        "will_submit_live_or_demo_orders": False,
        "operator_next_command": command,
        "next_codex_step": next_step,
    }


def _weather_blocker_counts(parsed: dict[str, Any]) -> list[dict[str, Any]]:
    counts = parsed.get("weather_activation_blocker_counts") or {}
    return [
        {"blocker": blocker, "rows": rows}
        for blocker, rows in sorted(counts.items(), key=lambda item: (-int(item[1]), str(item[0])))
    ]


def _weather_freshness_rows(parsed: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    preview_summary = parsed.get("weather_activation_preview_summary") or {}
    rows.append(
        {
            "source": "R12_PREVIEW_SUMMARY",
            "metric": "active_weather_markets_reviewed",
            "value": preview_summary.get("active_weather_markets_reviewed"),
            "detail": "Rows reviewed by the weather activation preview.",
        }
    )
    for key in [
        "stale_target_time_links",
        "current_linkable_weather_tickers",
        "rows_safe_to_link",
        "rows_safe_to_relink",
        "missing_weather_links",
        "expired_target_rows",
        "first_blocker",
    ]:
        rows.append(
            {
                "source": "R12_PREVIEW_SUMMARY",
                "metric": key,
                "value": preview_summary.get(key),
                "detail": "Weather activation preview summary.",
            }
        )
    funnel_summary = parsed.get("weather_funnel_summary") or {}
    for key in ["current_weather_rows", "ranking_rows", "paper_ready_rows", "first_blocker"]:
        rows.append(
            {
                "source": "R2_FAST_LANE_SUMMARY",
                "metric": key,
                "value": funnel_summary.get(key),
                "detail": "Weather fast-lane ranking impact summary.",
            }
        )
    db_snapshot = parsed.get("weather_db_snapshot") or {}
    db_links = db_snapshot.get("weather_market_links") or {}
    db_features = db_snapshot.get("weather_features") or {}
    for key in [
        "rows_sampled",
        "target_time_ge_now_minus_3h",
        "target_time_ge_now",
        "min_target_time",
        "max_target_time",
        "dominant_location_key",
    ]:
        rows.append(
            {
                "source": "REMOTE_DB_WEATHER_MARKET_LINKS",
                "metric": key,
                "value": db_links.get(key),
                "detail": "Read-only weather_market_links sample on cloud DB.",
            }
        )
    for key in [
        "rows_sampled",
        "target_time_ge_now_minus_3h",
        "generated_at_ge_now_minus_24h",
        "max_generated_at",
        "dominant_location_key",
    ]:
        rows.append(
            {
                "source": "REMOTE_DB_WEATHER_FEATURES",
                "metric": key,
                "value": db_features.get(key),
                "detail": "Read-only weather_features sample on cloud DB.",
            }
        )
    return rows


def _render_executive_summary(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# Phase 3BB-R45 Weather Freshness To Ranking Impact Review")
    decision = payload["impact_decision"]
    parsed = payload["parsed_impact_state"]
    preview = parsed.get("weather_activation_preview_summary") or {}
    lines.extend(
        [
            "",
            "## Result",
            "",
            f"- Status: `{decision['status']}`",
            f"- Review passed: `{decision['review_passed']}`",
            f"- Reason: {decision['primary_reason']}",
            f"- True first weather blocker: `{decision['first_weather_blocker']}`",
            f"- Scheduler timer: `{parsed.get('scheduler_timer_active_state')}`",
            f"- Scheduler service: `{parsed.get('scheduler_service_active_state')}`",
            f"- Scheduler last failure reason: `{parsed.get('scheduler_last_failure_reason')}`",
            f"- R44 verification passed: `{parsed.get('r44_verification_passed')}`",
            f"- R44 sequence: `{parsed.get('r44_weather_catalog_sequence')}`",
            f"- R12 active weather markets reviewed: `{preview.get('active_weather_markets_reviewed')}`",
            f"- R12 current linkable tickers: `{decision['current_linkable_weather_tickers']}`",
            f"- R12 rows_safe_to_link: `{decision['rows_safe_to_link']}`",
            f"- R12 rows_safe_to_relink: `{decision['rows_safe_to_relink']}`",
            f"- R2 fast-lane status: `{decision['fast_lane_status']}`",
            f"- R2 current weather rows: `{decision['current_weather_rows']}`",
            f"- R2 ranking rows: `{decision['ranking_rows']}`",
            f"- DB current weather links: `{decision['db_current_weather_links']}`",
            f"- DB future weather links: `{decision['db_future_weather_links']}`",
            "",
            "## Safety",
            "",
            "- Paper trade creation: `False`",
            "- Live/demo order submission/cancel/replace: `False`",
            "- Scheduler service/timer changes by this phase: `0`",
            "- Refresh jobs run by this phase: `0`",
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
    lines = _metadata_lines(payload, "# Phase 3BB-R45 Weather Freshness To Ranking Impact Detail")
    decision = payload["impact_decision"]
    parsed = payload["parsed_impact_state"]
    lines.extend(
        [
            "",
            "## Decision",
            "",
            f"- Status: `{decision['status']}`",
            f"- Primary reason: {decision['primary_reason']}",
            f"- First weather blocker: `{decision['first_weather_blocker']}`",
            "",
            "## Impact Checks",
            "",
            "| Check | Passed | Detail |",
            "|---|---:|---|",
        ]
    )
    for row in payload["impact_checks"]:
        lines.append(f"| `{row['check']}` | `{row['passed']}` | {row['detail']} |")
    lines.extend(["", "## Weather Freshness Metrics", "", "| Source | Metric | Value | Detail |", "|---|---|---:|---|"])
    for row in payload["weather_freshness_rows"]:
        lines.append(
            f"| `{row['source']}` | `{row['metric']}` | `{row.get('value', '')}` | {row.get('detail', '')} |"
        )
    lines.extend(["", "## R12 Blocker Counts", "", "| Blocker | Rows |", "|---|---:|"])
    for row in payload["weather_blocker_counts"]:
        lines.append(f"| `{row['blocker']}` | `{row['rows']}` |")
    lines.extend(["", "## Weather Reports", "", "| Path | Status | Mtime | Size |", "|---|---|---:|---:|"])
    for row in parsed.get("weather_report_freshness") or []:
        lines.append(
            f"| `{row['path']}` | `{row['status']}` | `{row['mtime_epoch']}` | `{row['size_bytes']}` |"
        )
    return "\n".join(lines) + "\n"


def _render_next_actions(payload: dict[str, Any]) -> str:
    decision = payload["impact_decision"]
    lines = _metadata_lines(payload, "# Phase 3BB-R45 Next Actions")
    lines.extend(
        [
            "",
            "## Next Operator Action",
            "",
            f"- Status: `{decision['status']}`",
            f"- Reason: {decision['primary_reason']}",
            f"- First weather blocker: `{decision['first_weather_blocker']}`",
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
            "- Do not manually run weather refresh jobs while the scheduler is active unless R45/R40 says to.",
        ]
    )
    return "\n".join(lines) + "\n"


def _render_operator_command(payload: dict[str, Any]) -> str:
    return "\n".join(
        [
            "#!/usr/bin/env bash",
            "set -euo pipefail",
            "",
            "# Phase 3BB-R45 next safe operator command.",
            payload["impact_decision"]["operator_next_command"],
            "",
        ]
    )


def _write_probe_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fieldnames = ["name", "ok", "exit_code", "duration_seconds", "timed_out", "stdout_excerpt", "stderr_excerpt"]
    _write_rows_csv(path, [{name: row.get(name) for name in fieldnames} for row in rows], fieldnames=fieldnames)


def _write_rows_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str] | None = None) -> None:
    if fieldnames is None:
        keys: list[str] = []
        for row in rows:
            for key in row:
                if key not in keys:
                    keys.append(key)
        fieldnames = keys or ["empty"]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        if rows:
            writer.writerows(rows)


def _mark_executable(path: Path) -> None:
    try:
        path.chmod(path.stat().st_mode | 0o111)
    except OSError:
        pass


def _check(name: str, passed: bool, detail: str) -> dict[str, Any]:
    return {"check": name, "passed": bool(passed), "detail": detail}


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
