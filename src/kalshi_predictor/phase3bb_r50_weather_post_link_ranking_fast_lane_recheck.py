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

PHASE3BB_R50_VERSION = "phase3bb_r50_weather_post_link_ranking_fast_lane_recheck_v1"
DEFAULT_OUTPUT_DIR = Path("reports/phase3bb_r50")
DEFAULT_REPORTS_DIR = Path("reports")
DEFAULT_PER_PROBE_TIMEOUT_SECONDS = 60
DEFAULT_FAST_LANE_TIMEOUT_SECONDS = 240

WEATHER_RANKING_RECHECK_REPORT_PATHS = (
    "reports/phase3bb_r49/weather_missing_link_apply_after_feature_refresh.json",
    "reports/phase3bb_r2/weather_funnel.json",
    "reports/phase3bb_r2/weather_candidates.csv",
    "reports/phase3ba_r2/weather_ranking_activation.json",
    "reports/phase3ba_r2/weather_opportunity_rows.csv",
    "reports/weather_opportunities.md",
)


@dataclass(frozen=True)
class Phase3BBR50WeatherPostLinkRankingFastLaneRecheckArtifacts:
    output_dir: Path
    executive_summary_path: Path
    markdown_path: Path
    json_path: Path
    probe_csv_path: Path
    checks_csv_path: Path
    fast_lane_summary_csv_path: Path
    report_freshness_csv_path: Path
    operator_command_path: Path
    next_actions_path: Path
    manifest_path: Path


def write_phase3bb_r50_weather_post_link_ranking_fast_lane_recheck_report(
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
    run_fast_lane: bool = True,
    fast_lane_timeout_seconds: int = DEFAULT_FAST_LANE_TIMEOUT_SECONDS,
    per_probe_timeout_seconds: int = DEFAULT_PER_PROBE_TIMEOUT_SECONDS,
    probe_runner: ProbeRunner | None = None,
) -> Phase3BBR50WeatherPostLinkRankingFastLaneRecheckArtifacts:
    payload = build_phase3bb_r50_weather_post_link_ranking_fast_lane_recheck(
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
        run_fast_lane=run_fast_lane,
        fast_lane_timeout_seconds=fast_lane_timeout_seconds,
        per_probe_timeout_seconds=per_probe_timeout_seconds,
        probe_runner=probe_runner,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    executive_summary_path = output_dir / "EXECUTIVE_SUMMARY.md"
    markdown_path = output_dir / "weather_post_link_ranking_fast_lane_recheck.md"
    json_path = output_dir / "weather_post_link_ranking_fast_lane_recheck.json"
    probe_csv_path = output_dir / "remote_probe_results.csv"
    checks_csv_path = output_dir / "fast_lane_checks.csv"
    fast_lane_summary_csv_path = output_dir / "weather_fast_lane_summary.csv"
    report_freshness_csv_path = output_dir / "weather_ranking_recheck_report_freshness.csv"
    operator_command_path = output_dir / "operator_next_command.sh"
    next_actions_path = output_dir / "NEXT_ACTIONS.md"
    manifest_path = output_dir / "MANIFEST.sha256"

    executive_summary_path.write_text(_render_executive_summary(payload), encoding="utf-8")
    markdown_path.write_text(_render_markdown(payload), encoding="utf-8")
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")
    _write_probe_csv(probe_csv_path, payload["remote_probe_results"])
    _write_rows_csv(checks_csv_path, payload["fast_lane_checks"])
    _write_rows_csv(fast_lane_summary_csv_path, [payload["weather_fast_lane_summary"]])
    _write_rows_csv(report_freshness_csv_path, payload["weather_ranking_recheck_report_freshness"])
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
            fast_lane_summary_csv_path,
            report_freshness_csv_path,
            operator_command_path,
            next_actions_path,
        ],
    )
    return Phase3BBR50WeatherPostLinkRankingFastLaneRecheckArtifacts(
        output_dir=output_dir,
        executive_summary_path=executive_summary_path,
        markdown_path=markdown_path,
        json_path=json_path,
        probe_csv_path=probe_csv_path,
        checks_csv_path=checks_csv_path,
        fast_lane_summary_csv_path=fast_lane_summary_csv_path,
        report_freshness_csv_path=report_freshness_csv_path,
        operator_command_path=operator_command_path,
        next_actions_path=next_actions_path,
        manifest_path=manifest_path,
    )


def build_phase3bb_r50_weather_post_link_ranking_fast_lane_recheck(
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
    run_fast_lane: bool = True,
    fast_lane_timeout_seconds: int = DEFAULT_FAST_LANE_TIMEOUT_SECONDS,
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
        "command": "kalshi-bot phase3bb-r50-weather-post-link-ranking-fast-lane-recheck",
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
    initial_results = [runner(probe, target) for probe in _initial_probes(target, timeout_seconds=per_probe_timeout_seconds)]
    initial = _parse_initial_probe_outputs(initial_results)
    local_r49_payload = _read_json(
        reports_dir / "phase3bb_r49" / "weather_missing_link_apply_after_feature_refresh.json"
    )
    if not initial.get("r49_json_available") and local_r49_payload:
        _merge_local_r49_fallback(initial, local_r49_payload)
    should_run = _should_run_fast_lane(initial, run_fast_lane=run_fast_lane)
    fast_lane_results: list[RemoteProbeResult] = []
    if should_run["run"]:
        fast_lane_results.append(
            runner(
                _fast_lane_probe(target, timeout_seconds=fast_lane_timeout_seconds),
                target,
            )
        )
    final_results = [
        runner(
            probe,
            target,
        )
        for probe in _final_probes(
            target,
            current_window_lookback_hours=current_window_lookback_hours,
            fresh_window_hours=fresh_window_hours,
            match_tolerance_hours=match_tolerance_hours,
            timeout_seconds=per_probe_timeout_seconds,
        )
    ]
    results = initial_results + fast_lane_results + final_results
    parsed = _parse_final_probe_outputs(results, initial=initial, should_run=should_run)
    checks = _fast_lane_checks(parsed)
    decision = _decision(checks, parsed)
    safety = {
        **_safety_flags(),
        "paper_only": True,
        "weather_fast_lane_recheck": True,
        "ssh_read_only_commands_executed": len(initial_results) + len(final_results),
        "ssh_write_capable_commands_executed": len(fast_lane_results),
        "remote_db_write_capable_fast_lane_executed": bool(fast_lane_results),
        "runs_weather_fast_lane": bool(fast_lane_results),
        "runs_weather_forecast_directly": False,
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
        "phase": "3BB-R50-WEATHER-POST-LINK-RANKING-FAST-LANE-RECHECK",
        "phase_version": PHASE3BB_R50_VERSION,
        "mode": "PAPER_ONLY_WEATHER_FAST_LANE_RECHECK",
        "reports_dir": str(reports_dir),
        "r11_context_available": bool(r11_context),
        "cloud_target": _target_payload(target),
        "remote_probe_results": [_result_payload(result) for result in results],
        "parsed_fast_lane_state": parsed,
        "fast_lane_checks": checks,
        "weather_fast_lane_summary": parsed.get("weather_fast_lane_summary") or {},
        "weather_ranking_recheck_report_freshness": parsed.get("weather_ranking_recheck_report_freshness") or [],
        "fast_lane_decision": decision,
        "next_operator_command": decision["operator_next_command"],
        "safety_flags": safety,
        "live_or_demo_execution": False,
        "order_submission_cancel_replace": False,
        "paper_trade_creation": False,
        "thresholds_lowered": False,
    }


def _initial_probes(target: CloudBootstrapTarget, *, timeout_seconds: int) -> list[RemoteProbe]:
    app = shlex.quote(target.app_path)
    env = shlex.quote(target.env_path)
    writer_cmd = f"cd {app} && set -a && . {env} && set +a && .venv/bin/kalshi-bot db-writer-monitor --json"
    return [
        RemoteProbe("remote_time_utc", "date -u +%Y-%m-%dT%H:%M:%SZ", timeout_seconds),
        RemoteProbe("db_writer_monitor_pre", writer_cmd, timeout_seconds),
        RemoteProbe("r49_json", f"cd {app} && cat reports/phase3bb_r49/weather_missing_link_apply_after_feature_refresh.json 2>/dev/null || true", timeout_seconds),
        RemoteProbe(
            "command_registry",
            (
                f"cd {app} && for cmd in "
                "phase3bb-r50-weather-post-link-ranking-fast-lane-recheck "
                "phase3bb-r49-weather-missing-link-apply-after-feature-refresh "
                "phase3bb-r2-weather-fast-lane "
                "phase3bb-r45-weather-freshness-to-ranking-impact "
                "phase3bb-r8-unified-paper-gate; do "
                ".venv/bin/kalshi-bot \"$cmd\" --help >/dev/null || exit 30; "
                "done; echo COMMAND_REGISTRY_OK"
            ),
            timeout_seconds,
        ),
    ]


def _fast_lane_probe(target: CloudBootstrapTarget, *, timeout_seconds: int) -> RemoteProbe:
    app = shlex.quote(target.app_path)
    env = shlex.quote(target.env_path)
    command = (
        f"cd {app} && set -a && . {env} && set +a && "
        f"timeout {int(timeout_seconds)} .venv/bin/kalshi-bot phase3bb-r2-weather-fast-lane "
        "--output-dir reports/phase3bb_r2 --reports-dir reports"
    )
    return RemoteProbe("weather_fast_lane_run", command, timeout_seconds + 10)


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
    report_list = " ".join(shlex.quote(path) for path in WEATHER_RANKING_RECHECK_REPORT_PATHS)
    writer_cmd = f"cd {app} && set -a && . {env} && set +a && .venv/bin/kalshi-bot db-writer-monitor --json"
    return [
        RemoteProbe("db_writer_monitor_post", writer_cmd, timeout_seconds),
        RemoteProbe("weather_funnel_json", f"cd {app} && cat reports/phase3bb_r2/weather_funnel.json 2>/dev/null || true", timeout_seconds),
        RemoteProbe("weather_ranking_activation_json", f"cd {app} && cat reports/phase3ba_r2/weather_ranking_activation.json 2>/dev/null || true", timeout_seconds),
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
            "weather_ranking_recheck_report_stats",
            (
                f"cd {app} && for p in {report_list}; do "
                "if [ -e \"$p\" ]; then stat -c '%n|%Y|%s' \"$p\"; "
                "else echo \"$p|MISSING|0\"; fi; done"
            ),
            timeout_seconds,
        ),
    ]


def _parse_initial_probe_outputs(results: list[RemoteProbeResult]) -> dict[str, Any]:
    by_name = {result.name: result for result in results}
    writer = _json_from_probe(by_name.get("db_writer_monitor_pre"))
    r49_payload = _json_from_probe(by_name.get("r49_json"))
    if not isinstance(writer, dict):
        writer = {}
    if not isinstance(r49_payload, dict):
        r49_payload = {}
    r49_decision = r49_payload.get("post_link_decision") if isinstance(r49_payload.get("post_link_decision"), dict) else {}
    return {
        "remote_time_utc": _first_line(_stdout(by_name.get("remote_time_utc"))),
        "writer_pre_status": writer.get("status") or "UNKNOWN",
        "writer_pre_safe_to_start_write": bool(writer.get("safe_to_start_write")) if writer else False,
        "writer_pre_current_pid": writer.get("current_writer_pid"),
        "r49_json_available": bool(r49_payload),
        "r49_status": r49_decision.get("status"),
        "r49_verification_passed": bool(r49_decision.get("verification_passed")),
        "r49_rows_written": _int_or_none(r49_decision.get("rows_written")),
        "r49_rows_safe_to_link": _int_or_none(r49_decision.get("rows_safe_to_link")),
        "r49_rows_safe_to_relink": _int_or_none(r49_decision.get("rows_safe_to_relink")),
        "command_registry_ok": "COMMAND_REGISTRY_OK" in _stdout(by_name.get("command_registry")),
        "failed_initial_probe_names": [result.name for result in results if not result.ok],
    }


def _merge_local_r49_fallback(parsed: dict[str, Any], payload: dict[str, Any]) -> None:
    decision = payload.get("post_link_decision") if isinstance(payload.get("post_link_decision"), dict) else {}
    if not decision:
        return
    parsed["r49_json_available"] = True
    parsed["r49_source"] = "LOCAL_REPORT_FALLBACK"
    parsed["r49_status"] = decision.get("status")
    parsed["r49_verification_passed"] = bool(decision.get("verification_passed"))
    parsed["r49_rows_written"] = _int_or_none(decision.get("rows_written"))
    parsed["r49_rows_safe_to_link"] = _int_or_none(decision.get("rows_safe_to_link"))
    parsed["r49_rows_safe_to_relink"] = _int_or_none(decision.get("rows_safe_to_relink"))


def _should_run_fast_lane(parsed: dict[str, Any], *, run_fast_lane: bool) -> dict[str, Any]:
    if not run_fast_lane:
        return {"run": False, "reason": "Disabled by --no-run-fast-lane."}
    if not parsed.get("writer_pre_safe_to_start_write"):
        return {"run": False, "reason": "Writer gate is not clear."}
    if not parsed.get("command_registry_ok"):
        return {"run": False, "reason": "Required cloud command registry check failed."}
    if not parsed.get("r49_verification_passed"):
        return {"run": False, "reason": "R49 post-link verification has not passed."}
    if int(parsed.get("r49_rows_safe_to_link") or 0) != 0:
        return {"run": False, "reason": "R49/R12 link gate is still open."}
    if int(parsed.get("r49_rows_safe_to_relink") or 0) != 0:
        return {"run": False, "reason": "R49/R12 relink gate is still open."}
    return {"run": True, "reason": "Writer clear and R49 link gate closed."}


def _parse_final_probe_outputs(
    results: list[RemoteProbeResult],
    *,
    initial: dict[str, Any],
    should_run: dict[str, Any],
) -> dict[str, Any]:
    by_name = {result.name: result for result in results}
    writer_post = _json_from_probe(by_name.get("db_writer_monitor_post"))
    funnel_payload = _json_from_probe(by_name.get("weather_funnel_json"))
    ranking_payload = _json_from_probe(by_name.get("weather_ranking_activation_json"))
    snapshot = _json_from_probe(by_name.get("weather_current_window_snapshot"))
    if not isinstance(writer_post, dict):
        writer_post = {}
    if not isinstance(funnel_payload, dict):
        funnel_payload = {}
    if not isinstance(ranking_payload, dict):
        ranking_payload = {}
    if not isinstance(snapshot, dict):
        snapshot = {}
    funnel_summary = funnel_payload.get("summary") if isinstance(funnel_payload.get("summary"), dict) else {}
    ranking_summary = ranking_payload.get("summary") if isinstance(ranking_payload.get("summary"), dict) else {}
    snapshot_summary = snapshot.get("summary") if isinstance(snapshot.get("summary"), dict) else {}
    fast_lane_result = by_name.get("weather_fast_lane_run")
    return {
        **initial,
        "fast_lane_should_run": bool(should_run.get("run")),
        "fast_lane_skip_reason": should_run.get("reason"),
        "fast_lane_run_attempted": fast_lane_result is not None,
        "fast_lane_run_ok": bool(fast_lane_result and fast_lane_result.ok),
        "fast_lane_run_exit_code": fast_lane_result.exit_code if fast_lane_result else None,
        "fast_lane_run_stdout_tail": _tail(_stdout(fast_lane_result)),
        "fast_lane_run_stderr_tail": _tail(fast_lane_result.stderr if fast_lane_result else ""),
        "writer_post_status": writer_post.get("status") or "UNKNOWN",
        "writer_post_safe_to_start_write": bool(writer_post.get("safe_to_start_write")) if writer_post else False,
        "writer_post_current_pid": writer_post.get("current_writer_pid"),
        "weather_funnel_json_ok": bool(funnel_payload),
        "weather_funnel_status": funnel_payload.get("status"),
        "weather_funnel_summary": funnel_summary,
        "weather_fast_lane_summary": {
            "status": funnel_payload.get("status"),
            "current_weather_rows": _int_or_none(funnel_summary.get("current_weather_rows")),
            "verified_link_rows": _int_or_none(funnel_summary.get("verified_link_rows")),
            "forecast_rows": _int_or_none(funnel_summary.get("forecast_rows")),
            "ranking_rows": _int_or_none(funnel_summary.get("ranking_rows")),
            "positive_ev_rows": _int_or_none(funnel_summary.get("positive_ev_rows")),
            "paper_ready_rows": _int_or_none(funnel_summary.get("paper_ready_rows")),
            "first_hard_blocker": funnel_summary.get("first_hard_blocker"),
        },
        "weather_ranking_activation_json_ok": bool(ranking_payload),
        "weather_ranking_activation_status": ranking_payload.get("status"),
        "weather_ranking_activation_summary": ranking_summary,
        "weather_current_window_snapshot_ok": bool(snapshot.get("ok")),
        "weather_current_window_summary": snapshot_summary,
        "weather_ranking_recheck_report_freshness": _parse_report_stats(_stdout(by_name.get("weather_ranking_recheck_report_stats"))),
        "failed_final_probe_names": [
            result.name for result in results if not result.ok and result.name != "weather_fast_lane_run"
        ],
    }


def _fast_lane_checks(parsed: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        _check("initial_remote_probes_completed", not parsed.get("failed_initial_probe_names"), f"failed={','.join(parsed.get('failed_initial_probe_names') or []) or 'none'}."),
        _check("command_registry_ok", bool(parsed.get("command_registry_ok")), "R50/R49/R2/R45/R8 commands are registered on the cloud host."),
        _check("r49_post_link_verified", bool(parsed.get("r49_verification_passed")), f"r49_status={parsed.get('r49_status')}."),
        _check("r49_link_gate_closed", int(parsed.get("r49_rows_safe_to_link") or 0) == 0 and int(parsed.get("r49_rows_safe_to_relink") or 0) == 0, f"safe_to_link={parsed.get('r49_rows_safe_to_link')} safe_to_relink={parsed.get('r49_rows_safe_to_relink')}."),
        _check("writer_pre_state_captured", parsed.get("writer_pre_status") != "UNKNOWN", f"writer_pre_status={parsed.get('writer_pre_status')}."),
        _check("fast_lane_run_policy_recorded", parsed.get("fast_lane_skip_reason") is not None, str(parsed.get("fast_lane_skip_reason"))),
        _check(
            "fast_lane_run_succeeded_or_cleanly_skipped",
            (not parsed.get("fast_lane_should_run") and not parsed.get("fast_lane_run_attempted"))
            or bool(parsed.get("fast_lane_run_ok")),
            f"attempted={parsed.get('fast_lane_run_attempted')} ok={parsed.get('fast_lane_run_ok')} exit={parsed.get('fast_lane_run_exit_code')}.",
        ),
        _check("final_remote_probes_completed", not parsed.get("failed_final_probe_names"), f"failed={','.join(parsed.get('failed_final_probe_names') or []) or 'none'}."),
        _check("weather_funnel_available", bool(parsed.get("weather_funnel_json_ok")), f"status={parsed.get('weather_funnel_status')}."),
    ]


def _decision(checks: list[dict[str, Any]], parsed: dict[str, Any]) -> dict[str, Any]:
    failed = [row for row in checks if not row["passed"]]
    summary = parsed.get("weather_fast_lane_summary") or {}
    current_rows = _int_or_none(summary.get("current_weather_rows")) or 0
    ranking_rows = _int_or_none(summary.get("ranking_rows")) or 0
    positive_ev_rows = _int_or_none(summary.get("positive_ev_rows")) or 0
    paper_ready_rows = _int_or_none(summary.get("paper_ready_rows")) or 0
    if not parsed.get("writer_pre_safe_to_start_write"):
        status = "WAIT_FOR_WRITER_CLEAR"
        reason = "Writer gate was busy; weather fast-lane was not run."
        next_step = "Phase 3BB-R50 - Retry Weather Fast-Lane After Writer Clears"
        command = "kalshi-bot db-writer-monitor --json"
        first_blocker = "ACTIVE_WRITER"
    elif failed:
        status = "BLOCKED_WEATHER_FAST_LANE_RECHECK"
        reason = f"First failing check: {failed[0]['check']}."
        next_step = "Phase 3BB-R50 - Repair Weather Fast-Lane Recheck"
        command = (
            "kalshi-bot phase3bb-r50-weather-post-link-ranking-fast-lane-recheck "
            "--output-dir reports/phase3bb_r50 --reports-dir reports"
        )
        first_blocker = failed[0]["check"].upper()
    elif paper_ready_rows > 0:
        status = "WEATHER_FAST_LANE_PAPER_READY_CANDIDATES"
        reason = "Weather fast-lane produced paper-ready rows; refresh unified gate before any operator review."
        next_step = "Phase 3BB-R8 - Unified Paper Gate Across Categories"
        command = "kalshi-bot phase3bb-r8-unified-paper-gate --output-dir reports/phase3bb_r8 --reports-dir reports"
        first_blocker = "PAPER_GATE_REFRESH_NEEDED"
    elif ranking_rows > 0:
        status = "WEATHER_FAST_LANE_RANKING_PRESENT"
        reason = "Weather fast-lane produced ranking rows; paper gate can now evaluate exact blockers."
        next_step = "Phase 3BB-R8 - Unified Paper Gate Across Categories"
        command = "kalshi-bot phase3bb-r8-unified-paper-gate --output-dir reports/phase3bb_r8 --reports-dir reports"
        first_blocker = summary.get("first_hard_blocker") or "PAPER_GATE_REFRESH_NEEDED"
    elif current_rows > 0:
        status = "WEATHER_FAST_LANE_RANKING_STILL_MISSING"
        reason = "Current weather rows are linked, but rankings still were not produced."
        next_step = "Phase 3BB-R51 - Weather Ranking Path Repair"
        command = (
            "kalshi-bot phase3bb-r51-weather-ranking-path-repair "
            "--output-dir reports/phase3bb_r51 --reports-dir reports"
        )
        first_blocker = "RANKING_MISSING"
    else:
        status = "WEATHER_FAST_LANE_NO_CURRENT_ROWS"
        reason = "The R12 link gate is closed, but fast-lane still sees no current weather rows."
        next_step = "Phase 3BB-R47 - Weather Current Window Series Discovery And Linkability Repair"
        command = (
            "kalshi-bot phase3bb-r47-weather-current-window-series-discovery-linkability-repair "
            "--output-dir reports/phase3bb_r47 --reports-dir reports"
        )
        first_blocker = "NO_CURRENT_WEATHER_ROWS"
    return {
        "status": status,
        "verification_passed": not failed and status != "WAIT_FOR_WRITER_CLEAR",
        "failed_check_count": len(failed),
        "first_failed_check": failed[0]["check"] if failed else None,
        "primary_reason": reason,
        "first_weather_blocker": first_blocker,
        "fast_lane_run_attempted": bool(parsed.get("fast_lane_run_attempted")),
        "fast_lane_run_ok": bool(parsed.get("fast_lane_run_ok")),
        "current_weather_rows": current_rows,
        "ranking_rows": ranking_rows,
        "positive_ev_rows": positive_ev_rows,
        "paper_ready_rows": paper_ready_rows,
        "writer_pre_safe_to_start_write": bool(parsed.get("writer_pre_safe_to_start_write")),
        "writer_post_safe_to_start_write": bool(parsed.get("writer_post_safe_to_start_write")),
        "will_create_paper_trades": False,
        "will_submit_live_or_demo_orders": False,
        "operator_next_command": command,
        "next_codex_step": next_step,
    }


def _render_executive_summary(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# Phase 3BB-R50 Weather Post-Link Ranking Fast-Lane Recheck")
    decision = payload["fast_lane_decision"]
    lines.extend(
        [
            "",
            "## Result",
            "",
            f"- Status: `{decision['status']}`",
            f"- Verification passed: `{decision['verification_passed']}`",
            f"- Reason: {decision['primary_reason']}",
            f"- First weather blocker: `{decision['first_weather_blocker']}`",
            f"- Fast-lane run attempted: `{decision['fast_lane_run_attempted']}`",
            f"- Fast-lane run ok: `{decision['fast_lane_run_ok']}`",
            f"- Current weather rows: `{decision['current_weather_rows']}`",
            f"- Ranking rows: `{decision['ranking_rows']}`",
            f"- Positive EV rows: `{decision['positive_ev_rows']}`",
            f"- Paper-ready rows: `{decision['paper_ready_rows']}`",
            f"- Writer safe before: `{decision['writer_pre_safe_to_start_write']}`",
            f"- Writer safe after: `{decision['writer_post_safe_to_start_write']}`",
            "",
            "## Safety",
            "",
            "- Paper trade creation: `False`",
            "- Live/demo order submission/cancel/replace: `False`",
            "- Missing-link apply run by this phase: `False`",
            "- Forecast run directly by this phase: `False`",
            "- Weather fast-lane may run only when writer gate is clear.",
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
    lines.extend(["", "## Checks", ""])
    lines.extend(_table(payload["fast_lane_checks"], ["check", "passed", "detail"]))
    lines.extend(["", "## Weather Fast-Lane Summary", ""])
    lines.extend(
        _table(
            [payload["weather_fast_lane_summary"]],
            [
                "status",
                "current_weather_rows",
                "verified_link_rows",
                "forecast_rows",
                "ranking_rows",
                "positive_ev_rows",
                "paper_ready_rows",
                "first_hard_blocker",
            ],
        )
    )
    lines.extend(["", "## Report Freshness", ""])
    lines.extend(
        _table(
            payload["weather_ranking_recheck_report_freshness"],
            ["path", "status", "mtime_epoch", "size_bytes"],
        )
    )
    lines.extend(["", "## Fast-Lane Output Tail", ""])
    parsed = payload["parsed_fast_lane_state"]
    lines.extend(["```text", str(parsed.get("fast_lane_run_stdout_tail") or ""), "```"])
    if parsed.get("fast_lane_run_stderr_tail"):
        lines.extend(["", "```text", str(parsed.get("fast_lane_run_stderr_tail") or ""), "```"])
    return "\n".join(lines)


def _render_next_actions(payload: dict[str, Any]) -> str:
    decision = payload["fast_lane_decision"]
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
            "- Do not create paper trades unless a downstream paper-ready gate opens.",
            "- Do not submit/cancel/replace live or demo orders.",
            "",
        ]
    )


def _render_operator_command(payload: dict[str, Any]) -> str:
    command = payload["fast_lane_decision"]["operator_next_command"]
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


def _tail(text: str, *, lines: int = 40) -> str:
    if not text:
        return ""
    return "\n".join(text.splitlines()[-lines:])


def _int_or_none(value: Any) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None
