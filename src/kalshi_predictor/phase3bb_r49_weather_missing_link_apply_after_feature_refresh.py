from __future__ import annotations

import csv
import json
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
from kalshi_predictor.phase3bb_r44_weather_catalog_hook_runtime_verification import (
    _first_line,
    _mark_executable,
    _parse_report_stats,
    _stdout,
    _target_payload,
)
from kalshi_predictor.phase3bb_r47_weather_current_window_series_discovery import (
    _weather_current_window_snapshot_command,
)
from kalshi_predictor.utils.time import utc_now

PHASE3BB_R49_VERSION = "phase3bb_r49_weather_missing_link_apply_after_feature_refresh_v1"
DEFAULT_OUTPUT_DIR = Path("reports/phase3bb_r49")
DEFAULT_REPORTS_DIR = Path("reports")
DEFAULT_PER_PROBE_TIMEOUT_SECONDS = 60

WEATHER_POST_LINK_REPORT_PATHS = (
    "reports/phase3bb_r48/weather_feature_refresh_runtime_verification.json",
    "reports/phase3az_r12_weather/weather_missing_link_apply.json",
    "reports/phase3az_r12_weather/weather_activation_preview.json",
    "reports/phase3az_r12_weather/safe_to_link.csv",
    "reports/phase3az_r12_weather/safe_to_relink.csv",
    "reports/phase3bb_r2/weather_funnel.json",
)


@dataclass(frozen=True)
class Phase3BBR49WeatherMissingLinkApplyAfterFeatureRefreshArtifacts:
    output_dir: Path
    executive_summary_path: Path
    markdown_path: Path
    json_path: Path
    probe_csv_path: Path
    checks_csv_path: Path
    apply_summary_csv_path: Path
    report_freshness_csv_path: Path
    operator_command_path: Path
    next_actions_path: Path
    manifest_path: Path


def write_phase3bb_r49_weather_missing_link_apply_after_feature_refresh_report(
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
    per_probe_timeout_seconds: int = DEFAULT_PER_PROBE_TIMEOUT_SECONDS,
    probe_runner: ProbeRunner | None = None,
) -> Phase3BBR49WeatherMissingLinkApplyAfterFeatureRefreshArtifacts:
    payload = build_phase3bb_r49_weather_missing_link_apply_after_feature_refresh(
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
        per_probe_timeout_seconds=per_probe_timeout_seconds,
        probe_runner=probe_runner,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    executive_summary_path = output_dir / "EXECUTIVE_SUMMARY.md"
    markdown_path = output_dir / "weather_missing_link_apply_after_feature_refresh.md"
    json_path = output_dir / "weather_missing_link_apply_after_feature_refresh.json"
    probe_csv_path = output_dir / "remote_probe_results.csv"
    checks_csv_path = output_dir / "post_link_checks.csv"
    apply_summary_csv_path = output_dir / "weather_missing_link_apply_summary.csv"
    report_freshness_csv_path = output_dir / "weather_post_link_report_freshness.csv"
    operator_command_path = output_dir / "operator_next_command.sh"
    next_actions_path = output_dir / "NEXT_ACTIONS.md"
    manifest_path = output_dir / "MANIFEST.sha256"

    executive_summary_path.write_text(_render_executive_summary(payload), encoding="utf-8")
    markdown_path.write_text(_render_markdown(payload), encoding="utf-8")
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")
    _write_probe_csv(probe_csv_path, payload["remote_probe_results"])
    _write_rows_csv(checks_csv_path, payload["post_link_checks"])
    _write_rows_csv(apply_summary_csv_path, [payload["weather_missing_link_apply_summary"]])
    _write_rows_csv(report_freshness_csv_path, payload["weather_post_link_report_freshness"])
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
            apply_summary_csv_path,
            report_freshness_csv_path,
            operator_command_path,
            next_actions_path,
        ],
    )
    return Phase3BBR49WeatherMissingLinkApplyAfterFeatureRefreshArtifacts(
        output_dir=output_dir,
        executive_summary_path=executive_summary_path,
        markdown_path=markdown_path,
        json_path=json_path,
        probe_csv_path=probe_csv_path,
        checks_csv_path=checks_csv_path,
        apply_summary_csv_path=apply_summary_csv_path,
        report_freshness_csv_path=report_freshness_csv_path,
        operator_command_path=operator_command_path,
        next_actions_path=next_actions_path,
        manifest_path=manifest_path,
    )


def build_phase3bb_r49_weather_missing_link_apply_after_feature_refresh(
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
        "command": "kalshi-bot phase3bb-r49-weather-missing-link-apply-after-feature-refresh",
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
        current_window_lookback_hours=current_window_lookback_hours,
        fresh_window_hours=fresh_window_hours,
        match_tolerance_hours=match_tolerance_hours,
        timeout_seconds=per_probe_timeout_seconds,
    )
    runner = probe_runner or _run_ssh_probe
    results = [runner(probe, target) for probe in probes]
    parsed = _parse_probe_outputs(results)
    local_r48_payload = _read_json(
        reports_dir / "phase3bb_r48" / "weather_feature_refresh_runtime_verification.json"
    )
    if not parsed.get("r48_json_available") and local_r48_payload:
        _merge_local_r48_fallback(parsed, local_r48_payload)
    checks = _post_link_checks(parsed)
    decision = _decision(checks, parsed)
    safety = {
        **_safety_flags(),
        "paper_only": True,
        "post_link_verification_only": True,
        "ssh_read_only_commands_executed": len(probes),
        "ssh_mutating_commands_executed": 0,
        "remote_db_writes_performed_by_this_phase": 0,
        "local_db_writes_performed": 0,
        "runs_weather_missing_link_apply": False,
        "runs_weather_activation_preview": False,
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
        "phase": "3BB-R49-WEATHER-MISSING-LINK-APPLY-AFTER-FEATURE-REFRESH",
        "phase_version": PHASE3BB_R49_VERSION,
        "mode": "PAPER_READ_ONLY_POST_LINK_APPLY_VERIFICATION",
        "reports_dir": str(reports_dir),
        "r11_context_available": bool(r11_context),
        "cloud_target": _target_payload(target),
        "remote_probe_results": [_result_payload(result) for result in results],
        "parsed_post_link_state": parsed,
        "post_link_checks": checks,
        "weather_missing_link_apply_summary": parsed.get("missing_link_apply_summary") or {},
        "weather_post_link_report_freshness": parsed.get("weather_post_link_report_freshness") or [],
        "post_link_decision": decision,
        "next_operator_command": decision["operator_next_command"],
        "safety_flags": safety,
        "live_or_demo_execution": False,
        "order_submission_cancel_replace": False,
        "paper_trade_creation": False,
        "thresholds_lowered": False,
    }


def _build_remote_probes(
    target: CloudBootstrapTarget,
    *,
    current_window_lookback_hours: int,
    fresh_window_hours: int,
    match_tolerance_hours: int,
    timeout_seconds: int,
) -> list[RemoteProbe]:
    app = shlex.quote(target.app_path)
    env = shlex.quote(target.env_path)
    report_list = " ".join(shlex.quote(path) for path in WEATHER_POST_LINK_REPORT_PATHS)
    writer_cmd = f"cd {app} && set -a && . {env} && set +a && .venv/bin/kalshi-bot db-writer-monitor --json"
    return [
        RemoteProbe("remote_time_utc", "date -u +%Y-%m-%dT%H:%M:%SZ", timeout_seconds),
        RemoteProbe("db_writer_monitor_raw", writer_cmd, timeout_seconds),
        RemoteProbe("r48_json", f"cd {app} && cat reports/phase3bb_r48/weather_feature_refresh_runtime_verification.json 2>/dev/null || true", timeout_seconds),
        RemoteProbe("weather_missing_link_apply_json", f"cd {app} && cat reports/phase3az_r12_weather/weather_missing_link_apply.json 2>/dev/null || true", timeout_seconds),
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
            "weather_post_link_report_stats",
            (
                f"cd {app} && for p in {report_list}; do "
                "if [ -e \"$p\" ]; then stat -c '%n|%Y|%s' \"$p\"; "
                "else echo \"$p|MISSING|0\"; fi; done"
            ),
            timeout_seconds,
        ),
        RemoteProbe(
            "command_registry",
            (
                f"cd {app} && for cmd in "
                "phase3bb-r49-weather-missing-link-apply-after-feature-refresh "
                "phase3bb-r48-weather-feature-refresh-runtime-verification "
                "phase3az-r12-weather-activation-preview "
                "phase3az-r12-weather-missing-link-apply "
                "phase3bb-r2-weather-fast-lane "
                "phase3bb-r45-weather-freshness-to-ranking-impact; do "
                ".venv/bin/kalshi-bot \"$cmd\" --help >/dev/null || exit 30; "
                "done; echo COMMAND_REGISTRY_OK"
            ),
            timeout_seconds,
        ),
    ]


def _parse_probe_outputs(results: list[RemoteProbeResult]) -> dict[str, Any]:
    by_name = {result.name: result for result in results}
    writer = _json_from_probe(by_name.get("db_writer_monitor_raw"))
    r48_payload = _json_from_probe(by_name.get("r48_json"))
    apply_payload = _json_from_probe(by_name.get("weather_missing_link_apply_json"))
    preview_payload = _json_from_probe(by_name.get("weather_activation_preview_json"))
    funnel_payload = _json_from_probe(by_name.get("weather_funnel_json"))
    snapshot = _json_from_probe(by_name.get("weather_current_window_snapshot"))
    if not isinstance(writer, dict):
        writer = {}
    if not isinstance(r48_payload, dict):
        r48_payload = {}
    if not isinstance(apply_payload, dict):
        apply_payload = {}
    if not isinstance(preview_payload, dict):
        preview_payload = {}
    if not isinstance(funnel_payload, dict):
        funnel_payload = {}
    if not isinstance(snapshot, dict):
        snapshot = {}
    apply_summary = apply_payload.get("summary") if isinstance(apply_payload.get("summary"), dict) else {}
    preview_summary = preview_payload.get("summary") if isinstance(preview_payload.get("summary"), dict) else {}
    funnel_summary = funnel_payload.get("summary") if isinstance(funnel_payload.get("summary"), dict) else {}
    snapshot_summary = snapshot.get("summary") if isinstance(snapshot.get("summary"), dict) else {}
    r48_decision = r48_payload.get("runtime_decision") if isinstance(r48_payload.get("runtime_decision"), dict) else {}
    return {
        "remote_time_utc": _first_line(_stdout(by_name.get("remote_time_utc"))),
        "writer_status": writer.get("status") or "UNKNOWN",
        "writer_safe_to_start_write": bool(writer.get("safe_to_start_write")) if writer else False,
        "writer_current_pid": writer.get("current_writer_pid"),
        "r48_json_available": bool(r48_payload),
        "r48_status": r48_decision.get("status"),
        "r48_rows_safe_to_link": r48_decision.get("rows_safe_to_link"),
        "r48_rows_safe_to_relink": r48_decision.get("rows_safe_to_relink"),
        "missing_link_apply_json_available": bool(apply_payload),
        "missing_link_apply_status": apply_payload.get("status"),
        "missing_link_apply_generated_at": apply_payload.get("generated_at"),
        "missing_link_apply_backup": apply_payload.get("backup") or apply_payload.get("backup_path"),
        "missing_link_apply_summary": {
            "status": apply_payload.get("status"),
            "generated_at": apply_payload.get("generated_at"),
            "backup": apply_payload.get("backup") or apply_payload.get("backup_path"),
            "preview_rows_safe_to_link": _int_or_none(apply_summary.get("preview_rows_safe_to_link")),
            "candidates_reviewed": _int_or_none(apply_summary.get("candidates_reviewed")),
            "would_write_link_rows": _int_or_none(apply_summary.get("would_write_link_rows")),
            "link_rows_written": _int_or_none(apply_summary.get("link_rows_written")),
            "skipped_rows": _int_or_none(apply_summary.get("skipped_rows")),
        },
        "weather_activation_preview_json_ok": bool(preview_payload),
        "weather_activation_preview_summary": preview_summary,
        "weather_activation_rows_safe_to_link": _int_or_none(preview_summary.get("rows_safe_to_link")),
        "weather_activation_rows_safe_to_relink": _int_or_none(preview_summary.get("rows_safe_to_relink")),
        "weather_activation_current_linkable_weather_tickers": _int_or_none(preview_summary.get("current_linkable_weather_tickers")),
        "weather_activation_missing_weather_links": _int_or_none(preview_summary.get("missing_weather_links")),
        "weather_activation_first_blocker": preview_summary.get("first_blocker"),
        "weather_funnel_json_ok": bool(funnel_payload),
        "weather_funnel_status": funnel_payload.get("status"),
        "weather_funnel_summary": funnel_summary,
        "weather_current_rows": _int_or_none(funnel_summary.get("current_weather_rows")),
        "weather_ranking_rows": _int_or_none(funnel_summary.get("ranking_rows")),
        "weather_current_window_snapshot_ok": bool(snapshot.get("ok")),
        "weather_current_window_error": snapshot.get("error"),
        "weather_current_window_summary": snapshot_summary,
        "weather_post_link_report_freshness": _parse_report_stats(_stdout(by_name.get("weather_post_link_report_stats"))),
        "command_registry_ok": "COMMAND_REGISTRY_OK" in _stdout(by_name.get("command_registry")),
        "failed_probe_names": [result.name for result in results if not result.ok],
    }


def _merge_local_r48_fallback(parsed: dict[str, Any], payload: dict[str, Any]) -> None:
    r48_decision = payload.get("runtime_decision") if isinstance(payload.get("runtime_decision"), dict) else {}
    if not r48_decision:
        return
    parsed["r48_json_available"] = True
    parsed["r48_source"] = "LOCAL_REPORT_FALLBACK"
    parsed["r48_status"] = r48_decision.get("status")
    parsed["r48_rows_safe_to_link"] = r48_decision.get("rows_safe_to_link")
    parsed["r48_rows_safe_to_relink"] = r48_decision.get("rows_safe_to_relink")


def _post_link_checks(parsed: dict[str, Any]) -> list[dict[str, Any]]:
    apply_summary = parsed.get("missing_link_apply_summary") or {}
    preview_rows_safe_to_link = int(parsed.get("weather_activation_rows_safe_to_link") or 0)
    preview_rows_safe_to_relink = int(parsed.get("weather_activation_rows_safe_to_relink") or 0)
    return [
        _check("remote_probes_completed", not parsed.get("failed_probe_names"), f"failed={','.join(parsed.get('failed_probe_names') or []) or 'none'}."),
        _check("command_registry_ok", bool(parsed.get("command_registry_ok")), "R49/R48/R12/R2/R45 commands are registered on the cloud host."),
        _check("writer_state_captured", parsed.get("writer_status") != "UNKNOWN", f"writer_status={parsed.get('writer_status')} pid={parsed.get('writer_current_pid')}."),
        _check("r48_runtime_report_available", bool(parsed.get("r48_json_available")), "R48 weather feature runtime verification JSON exists."),
        _check("r12_apply_report_available", bool(parsed.get("missing_link_apply_json_available")), "R12 missing-link apply JSON exists."),
        _check(
            "r12_apply_status_known",
            parsed.get("missing_link_apply_status") in {"APPLIED", "NO_SAFE_ROWS"},
            f"status={parsed.get('missing_link_apply_status')}.",
        ),
        _check(
            "backup_present_when_rows_written",
            int(apply_summary.get("link_rows_written") or 0) == 0 or bool(apply_summary.get("backup")),
            f"rows_written={apply_summary.get('link_rows_written')} backup={apply_summary.get('backup')}.",
        ),
        _check("r12_preview_available_after_apply", bool(parsed.get("weather_activation_preview_json_ok")), "R12 activation preview JSON exists after apply."),
        _check(
            "post_apply_link_gate_closed",
            preview_rows_safe_to_link == 0 and preview_rows_safe_to_relink == 0,
            f"rows_safe_to_link={preview_rows_safe_to_link} rows_safe_to_relink={preview_rows_safe_to_relink}.",
        ),
        _check("current_window_snapshot_readable", bool(parsed.get("weather_current_window_snapshot_ok")), f"error={parsed.get('weather_current_window_error')}."),
    ]


def _decision(checks: list[dict[str, Any]], parsed: dict[str, Any]) -> dict[str, Any]:
    failed = [row for row in checks if not row["passed"]]
    apply_summary = parsed.get("missing_link_apply_summary") or {}
    preview_rows_safe_to_link = int(parsed.get("weather_activation_rows_safe_to_link") or 0)
    preview_rows_safe_to_relink = int(parsed.get("weather_activation_rows_safe_to_relink") or 0)
    rows_written = int(apply_summary.get("link_rows_written") or 0)
    weather_current_rows = _int_or_none(parsed.get("weather_current_rows"))
    ranking_rows = _int_or_none(parsed.get("weather_ranking_rows"))

    writer_safe = bool(parsed.get("writer_safe_to_start_write"))

    if failed and failed[0]["check"] == "post_apply_link_gate_closed" and (
        preview_rows_safe_to_link > 0 or preview_rows_safe_to_relink > 0
    ):
        status = "WEATHER_LINK_GATE_STILL_OPEN"
        reason = "R12 still reports safe rows after the last apply; rerun only the guarded apply path."
        next_step = "Phase 3AZ-R12 - Weather Missing Link Apply"
        command = (
            "kalshi-bot db-writer-monitor --json\n"
            "kalshi-bot phase3az-r12-weather-missing-link-apply "
            "--output-dir reports/phase3az_r12_weather --limit 2000 "
            "--fresh-window-hours 24 --match-tolerance-hours 3 "
            "--max-records 25 --apply --backup-first\n"
            "kalshi-bot phase3az-r12-weather-activation-preview "
            "--output-dir reports/phase3az_r12_weather --limit 2000 "
            "--fresh-window-hours 24 --match-tolerance-hours 3"
        )
        first_blocker = "R12_LINK_GATE_STILL_OPEN"
    elif failed:
        status = "BLOCKED_WEATHER_MISSING_LINK_APPLY_VERIFICATION"
        reason = f"First failing check: {failed[0]['check']}."
        next_step = "Phase 3BB-R49 - Repair Missing Link Apply Verification"
        command = (
            "kalshi-bot phase3bb-r49-weather-missing-link-apply-after-feature-refresh "
            "--output-dir reports/phase3bb_r49 --reports-dir reports"
        )
        first_blocker = failed[0]["check"].upper()
    elif rows_written > 0 and not writer_safe:
        status = "WEATHER_MISSING_LINK_APPLY_VERIFIED_WAIT_FOR_WRITER"
        reason = "The R12 apply wrote rows and closed the link gate; wait for the active writer before weather fast-lane."
        next_step = "Phase 3BB-R49 - Wait For Writer Gate"
        command = "kalshi-bot db-writer-monitor --json"
        first_blocker = "ACTIVE_WRITER_BEFORE_FAST_LANE"
    elif rows_written > 0:
        status = "WEATHER_MISSING_LINK_APPLY_VERIFIED"
        reason = "The R12 writer-gated missing-link apply wrote rows with a backup, and the follow-up preview gate is closed."
        next_step = "Phase 3BB-R50 - Weather Post-Link Ranking Fast-Lane Recheck"
        command = (
            "kalshi-bot phase3bb-r50-weather-post-link-ranking-fast-lane-recheck "
            "--output-dir reports/phase3bb_r50 --reports-dir reports"
        )
        first_blocker = "RANKING_RECHECK_NEEDED"
    elif weather_current_rows and ranking_rows:
        status = "WEATHER_LINK_GATE_CLOSED_AND_RANKING_PRESENT"
        reason = "Weather links and rankings are present; refresh the unified paper gate."
        next_step = "Phase 3BB-R8 - Unified Paper Gate Across Categories"
        command = "kalshi-bot phase3bb-r8-unified-paper-gate --output-dir reports/phase3bb_r8 --reports-dir reports"
        first_blocker = "PAPER_GATE_REFRESH_NEEDED"
    else:
        status = "WEATHER_LINK_GATE_CLOSED_NEEDS_FAST_LANE"
        reason = "The R12 link gate is closed, but weather ranking impact still needs a fast-lane recheck."
        next_step = "Phase 3BB-R50 - Weather Post-Link Ranking Fast-Lane Recheck"
        command = (
            "kalshi-bot db-writer-monitor --json\n"
            "kalshi-bot phase3bb-r2-weather-fast-lane "
            "--output-dir reports/phase3bb_r2 --reports-dir reports"
        )
        first_blocker = "RANKING_RECHECK_NEEDED"

    return {
        "status": status,
        "verification_passed": not failed,
        "failed_check_count": len(failed),
        "first_failed_check": failed[0]["check"] if failed else None,
        "primary_reason": reason,
        "first_weather_blocker": first_blocker,
        "rows_written": rows_written,
        "backup": apply_summary.get("backup"),
        "rows_safe_to_link": preview_rows_safe_to_link,
        "rows_safe_to_relink": preview_rows_safe_to_relink,
        "weather_current_rows": weather_current_rows,
        "weather_ranking_rows": ranking_rows,
        "will_create_paper_trades": False,
        "will_submit_live_or_demo_orders": False,
        "operator_next_command": command,
        "next_codex_step": next_step,
    }


def _render_executive_summary(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# Phase 3BB-R49 Weather Missing Link Apply After Feature Refresh")
    decision = payload["post_link_decision"]
    parsed = payload["parsed_post_link_state"]
    lines.extend(
        [
            "",
            "## Result",
            "",
            f"- Status: `{decision['status']}`",
            f"- Verification passed: `{decision['verification_passed']}`",
            f"- Reason: {decision['primary_reason']}",
            f"- First weather blocker: `{decision['first_weather_blocker']}`",
            f"- Writer safe after apply: `{parsed.get('writer_safe_to_start_write')}`",
            f"- R12 apply status: `{parsed.get('missing_link_apply_status')}`",
            f"- Link rows written: `{decision['rows_written']}`",
            f"- Backup: `{decision['backup']}`",
            f"- Post-apply rows_safe_to_link: `{decision['rows_safe_to_link']}`",
            f"- Post-apply rows_safe_to_relink: `{decision['rows_safe_to_relink']}`",
            f"- Weather current rows: `{decision['weather_current_rows']}`",
            f"- Weather ranking rows: `{decision['weather_ranking_rows']}`",
            "",
            "## Safety",
            "",
            "- Paper trade creation: `False`",
            "- Live/demo order submission/cancel/replace: `False`",
            "- Missing-link apply run by this phase: `False`",
            "- Remote DB writes by this phase: `0`",
            "- Forecast/fast-lane run by this phase: `False`",
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
    decision = payload["post_link_decision"]
    lines = _render_executive_summary(payload).splitlines()
    lines.extend(["", "## Checks", ""])
    lines.extend(_table(payload["post_link_checks"], ["check", "passed", "detail"]))
    lines.extend(["", "## Apply Summary", ""])
    lines.extend(
        _table(
            [payload["weather_missing_link_apply_summary"]],
            [
                "status",
                "generated_at",
                "preview_rows_safe_to_link",
                "candidates_reviewed",
                "link_rows_written",
                "skipped_rows",
                "backup",
            ],
        )
    )
    lines.extend(["", "## Report Freshness", ""])
    lines.extend(
        _table(
            payload["weather_post_link_report_freshness"],
            ["path", "status", "mtime_epoch", "size_bytes"],
        )
    )
    lines.extend(
        [
            "",
            "## Guardrails",
            "",
            "- Do not rerun missing-link apply unless R12 preview reports rows_safe_to_link greater than 0.",
            "- Do not create paper trades unless a downstream paper-ready gate opens.",
            "- Do not run live/demo exchange order commands.",
            "- Keep the next step writer-gated because weather fast-lane can write ranking artifacts.",
            "",
            f"Decision: `{decision['status']}`",
            "",
        ]
    )
    return "\n".join(lines)


def _render_next_actions(payload: dict[str, Any]) -> str:
    decision = payload["post_link_decision"]
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
            "- Do not create paper trades from this phase.",
            "- Do not submit/cancel/replace live or demo orders.",
            "- Do not rerun R12 apply while rows_safe_to_link=0.",
            "- Run weather fast-lane only after db-writer-monitor is clear.",
            "",
        ]
    )


def _render_operator_command(payload: dict[str, Any]) -> str:
    command = payload["post_link_decision"]["operator_next_command"]
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
    return str(value).replace("|", "\\|").replace("\n", " ")


def _int_or_none(value: Any) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None
