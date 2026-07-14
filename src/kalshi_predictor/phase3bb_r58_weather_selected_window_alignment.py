from __future__ import annotations

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
    ProbeRunner,
    RemoteProbe,
    RemoteProbeResult,
    _json_from_probe,
    _resolve_target,
    _result_payload,
    _run_ssh_probe,
)
from kalshi_predictor.phase3bb_r44_weather_catalog_hook_runtime_verification import (
    _mark_executable,
    _target_payload,
)
from kalshi_predictor.phase3bb_r54_weather_missing_link_apply_deferral import (
    _int_or_zero,
    _write_probe_csv,
    _write_rows_csv,
)
from kalshi_predictor.utils.time import utc_now

PHASE3BB_R58_VERSION = "phase3bb_r58_weather_selected_window_alignment_v1"
DEFAULT_OUTPUT_DIR = Path("reports/phase3bb_r58")
DEFAULT_REPORTS_DIR = Path("reports")
DEFAULT_PER_PROBE_TIMEOUT_SECONDS = 60

ROW_FIELDS = [
    "ticker",
    "market_target_time",
    "market_close_time",
    "market_expected_expiration_time",
    "link_target_time",
    "link_detected_at",
    "snapshot_at",
    "source_forecast_time",
    "source_forecast_generated_at",
    "feature_target_time",
    "feature_generated_at",
    "feature_distance_hours",
    "forecast_at",
    "forecast_feature_target_time",
    "ranking_at",
    "ranking_edge",
    "first_alignment_blocker",
]


@dataclass(frozen=True)
class Phase3BBR58WeatherSelectedWindowAlignmentArtifacts:
    output_dir: Path
    executive_summary_path: Path
    markdown_path: Path
    json_path: Path
    rows_csv_path: Path
    patch_status_csv_path: Path
    probe_csv_path: Path
    operator_command_path: Path
    next_actions_path: Path
    manifest_path: Path


def write_phase3bb_r58_weather_selected_window_alignment_report(
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
    match_tolerance_hours: int = 3,
    per_probe_timeout_seconds: int = DEFAULT_PER_PROBE_TIMEOUT_SECONDS,
    probe_runner: ProbeRunner | None = None,
) -> Phase3BBR58WeatherSelectedWindowAlignmentArtifacts:
    payload = build_phase3bb_r58_weather_selected_window_alignment(
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
        match_tolerance_hours=match_tolerance_hours,
        per_probe_timeout_seconds=per_probe_timeout_seconds,
        probe_runner=probe_runner,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    executive_summary_path = output_dir / "EXECUTIVE_SUMMARY.md"
    markdown_path = output_dir / "weather_selected_window_alignment.md"
    json_path = output_dir / "weather_selected_window_alignment.json"
    rows_csv_path = output_dir / "selected_window_alignment_rows.csv"
    patch_status_csv_path = output_dir / "patch_status.csv"
    probe_csv_path = output_dir / "remote_probe_results.csv"
    operator_command_path = output_dir / "operator_next_command.sh"
    next_actions_path = output_dir / "NEXT_ACTIONS.md"
    manifest_path = output_dir / "MANIFEST.sha256"

    executive_summary_path.write_text(_render_executive_summary(payload), encoding="utf-8")
    markdown_path.write_text(_render_markdown(payload), encoding="utf-8")
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")
    _write_rows_csv(rows_csv_path, payload["alignment_rows"])
    _write_rows_csv(patch_status_csv_path, payload["patch_status_rows"])
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
            patch_status_csv_path,
            probe_csv_path,
            operator_command_path,
            next_actions_path,
        ],
    )
    return Phase3BBR58WeatherSelectedWindowAlignmentArtifacts(
        output_dir=output_dir,
        executive_summary_path=executive_summary_path,
        markdown_path=markdown_path,
        json_path=json_path,
        rows_csv_path=rows_csv_path,
        patch_status_csv_path=patch_status_csv_path,
        probe_csv_path=probe_csv_path,
        operator_command_path=operator_command_path,
        next_actions_path=next_actions_path,
        manifest_path=manifest_path,
    )


def build_phase3bb_r58_weather_selected_window_alignment(
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
        "command": "kalshi-bot phase3bb-r58-weather-selected-window-forecast-feature-alignment-repair",
        "argv": command_args or [],
    }
    r57_payload = _read_json(reports_dir / "phase3bb_r57" / "selected_window_weather_pipeline.json")
    selected = _selected_window_from_r57(r57_payload)
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
    probes = [
        RemoteProbe("remote_time_utc", "date -u +%Y-%m-%dT%H:%M:%SZ", per_probe_timeout_seconds),
        RemoteProbe(
            "command_registry",
            (
                f"cd {shlex.quote(target.app_path)} && for cmd in "
                "phase3bb-r53-weather-current-window-cadence-preview-narrowing-repair "
                "phase3bb-r57-weather-selected-window-pipeline-speed-repair "
                "phase3bb-r8-unified-paper-gate forecast; do "
                ".venv/bin/kalshi-bot \"$cmd\" --help >/dev/null || exit 30; "
                "done; echo COMMAND_REGISTRY_OK"
            ),
            per_probe_timeout_seconds,
        ),
        RemoteProbe(
            "selected_window_alignment",
            _alignment_probe_command(
                target.db_path,
                selected_target_time=selected["selected_target_time"],
                selected_tickers=selected["selected_tickers"],
                match_tolerance_hours=match_tolerance_hours,
            ),
            per_probe_timeout_seconds,
        ),
    ]
    results = [runner(probe, target) for probe in probes]
    alignment_payload = _json_from_named_probe(results, "selected_window_alignment")
    alignment_rows = alignment_payload.get("rows") if isinstance(alignment_payload.get("rows"), list) else []
    patch_status = _patch_status()
    summary = _summary(
        selected=selected,
        alignment_payload=alignment_payload,
        alignment_rows=alignment_rows,
        patch_status=patch_status,
        results=results,
    )
    decision = _decision(summary)
    safety = {
        **_safety_flags(),
        "paper_only": True,
        "diagnostic_only": True,
        "weather_selected_window_alignment_repair": True,
        "ssh_read_only_commands_executed": len(results),
        "ssh_write_capable_commands_executed": 0,
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
        "phase": "3BB-R58-WEATHER-SELECTED-WINDOW-FORECAST-FEATURE-ALIGNMENT-REPAIR",
        "phase_version": PHASE3BB_R58_VERSION,
        "mode": "PAPER_ONLY_WEATHER_SELECTED_WINDOW_ALIGNMENT_DIAGNOSTIC",
        "reports_dir": str(reports_dir),
        "cloud_target": _target_payload(target),
        "parameters": {
            "match_tolerance_hours": match_tolerance_hours,
            "per_probe_timeout_seconds": per_probe_timeout_seconds,
        },
        "r57_selected_window": selected,
        "r57_forecast_loop": _r57_forecast_loop_status(r57_payload),
        "alignment_probe": alignment_payload,
        "alignment_rows": alignment_rows,
        "patch_status": patch_status,
        "patch_status_rows": [{"check": key, "ok": value} for key, value in patch_status.items()],
        "summary": summary,
        "decision": decision,
        "next_operator_command": decision["operator_next_command"],
        "remote_probe_results": [_result_payload(result) for result in results],
        "safety_flags": safety,
        "live_or_demo_execution": False,
        "order_submission_cancel_replace": False,
        "paper_trade_creation": False,
        "thresholds_lowered": False,
    }


def _selected_window_from_r57(payload: dict[str, Any]) -> dict[str, Any]:
    r53 = payload.get("r53_final_payload") or payload.get("r53_after_apply_payload") or payload.get("r53_initial_payload") or {}
    summary = r53.get("summary") if isinstance(r53.get("summary"), dict) else {}
    rows = payload.get("selected_window_tickers") if isinstance(payload.get("selected_window_tickers"), list) else []
    tickers = []
    for row in rows:
        ticker = str(row.get("ticker") or "").strip()
        if ticker and ticker not in tickers:
            tickers.append(ticker)
    return {
        "selected_target_time": summary.get("selected_target_time"),
        "selected_minutes_until_target": summary.get("selected_minutes_until_target"),
        "selected_window_rows": _int_or_zero(summary.get("selected_window_market_rows")),
        "selected_forecast_rows": _int_or_zero(summary.get("selected_window_forecast_rows")),
        "selected_ranking_rows": _int_or_zero(summary.get("selected_window_ranking_rows")),
        "selected_tickers": tickers,
    }


def _r57_forecast_loop_status(payload: dict[str, Any]) -> dict[str, Any]:
    rows = payload.get("remote_probe_results") if isinstance(payload.get("remote_probe_results"), list) else []
    probe = next((row for row in rows if row.get("name") == "weather_per_ticker_forecast"), {})
    stdout = str(probe.get("stdout_excerpt") or "")
    forecasted = _extract_count(stdout, "PHASE3BB_R57_FORECASTED_TICKERS")
    selected = _extract_count(stdout, "PHASE3BB_R57_SELECTED_TICKERS")
    return {
        "probe_seen": bool(probe),
        "forecasted_tickers": forecasted,
        "selected_tickers_reported": selected,
        "stdout_excerpt": stdout,
        "zero_forecast_regression_seen": forecasted == 0,
    }


def _extract_count(text: str, key: str) -> int | None:
    match = re.search(rf"{re.escape(key)}=(\d+)", text)
    if not match:
        return None
    return int(match.group(1))


def _patch_status() -> dict[str, bool]:
    source = Path(__file__).with_name("phase3bb_r57_weather_selected_window_pipeline.py")
    text = source.read_text(encoding="utf-8") if source.exists() else ""
    return {
        "r57_passes_selected_tickers_to_pipeline": "selected_tickers=_selected_ticker_values(active_r53)" in text,
        "r57_forecast_shell_uses_selected_tickers": "selected_tickers = " in text and "PHASE3BB_R57_SELECTED_TICKERS" in text,
        "r57_no_longer_rediscovers_window_for_forecast": "where (series_ticker = 'KXTEMPNYCH'" not in text,
    }


def _alignment_probe_command(
    db_path: str,
    *,
    selected_target_time: str | None,
    selected_tickers: list[str],
    match_tolerance_hours: int,
) -> str:
    script = r"""
import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

db_path = __DB_PATH__
selected_target_time = __SELECTED_TARGET_TIME__
selected_tickers = json.loads(__SELECTED_TICKERS_JSON__)
match_tolerance_hours = __MATCH_TOLERANCE_HOURS__
now = datetime.now(timezone.utc)

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
    parsed = parse_dt(value)
    return parsed.isoformat() if parsed else None

def target_from_market(row):
    for key in ("close_time", "expected_expiration_time", "expiration_time", "settlement_ts"):
        parsed = parse_dt(row.get(key))
        if parsed is not None:
            return parsed
    return None

def nearest(rows, target, key):
    target_dt = parse_dt(target)
    if target_dt is None:
        return None, None
    best = None
    best_distance = None
    for row in rows:
        candidate = parse_dt(row.get(key))
        if candidate is None:
            continue
        distance = abs((candidate - target_dt).total_seconds()) / 3600
        if best_distance is None or distance < best_distance:
            best = row
            best_distance = distance
    return best, best_distance

def dec(value):
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None

payload = {
    "ok": False,
    "error": None,
    "generated_at": now.isoformat(),
    "selected_target_time": selected_target_time,
    "selected_ticker_count": len(selected_tickers),
    "rows": [],
    "summary": {},
}
try:
    conn = sqlite3.connect("file:" + db_path + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    features = [dict(row) for row in conn.execute(
        '''
        select id, location_key, generated_at, target_time, temperature_f,
               weather_confidence_score
        from weather_features
        where location_key = 'new_york'
        order by generated_at desc, id desc
        limit 50000
        '''
    ).fetchall()]
    source = [dict(row) for row in conn.execute(
        '''
        select id, location_key, forecast_generated_at, forecast_time, temperature_f
        from weather_forecasts
        where location_key = 'new_york'
        order by forecast_generated_at desc, id desc
        limit 50000
        '''
    ).fetchall()]
    rows = []
    for ticker in selected_tickers:
        market_row = conn.execute(
            '''
            select ticker, status, close_time, expected_expiration_time,
                   expiration_time, settlement_ts, last_seen_at
            from markets where ticker = ?
            ''',
            (ticker,),
        ).fetchone()
        market = dict(market_row) if market_row else {}
        link_row = conn.execute(
            '''
            select ticker, location_key, target_time, detected_at, target_value,
                   confidence
            from weather_market_links
            where ticker = ?
            order by detected_at desc, id desc
            limit 1
            ''',
            (ticker,),
        ).fetchone()
        link = dict(link_row) if link_row else {}
        snapshot_row = conn.execute(
            '''
            select ticker, captured_at, best_yes_bid, best_yes_ask
            from market_snapshots
            where ticker = ?
            order by captured_at desc, id desc
            limit 1
            ''',
            (ticker,),
        ).fetchone()
        snapshot = dict(snapshot_row) if snapshot_row else {}
        forecast_row = conn.execute(
            '''
            select ticker, forecasted_at, yes_probability, feature_json
            from forecasts
            where ticker = ? and model_name = 'weather_v2'
            order by forecasted_at desc, id desc
            limit 1
            ''',
            (ticker,),
        ).fetchone()
        forecast = dict(forecast_row) if forecast_row else {}
        ranking_row = conn.execute(
            '''
            select ticker, ranked_at, estimated_edge, opportunity_score
            from market_rankings
            where ticker = ? and forecast_model = 'weather_v2'
            order by ranked_at desc, id desc
            limit 1
            ''',
            (ticker,),
        ).fetchone()
        ranking = dict(ranking_row) if ranking_row else {}
        market_target = target_from_market(market)
        link_target = link.get("target_time")
        compare_target = link_target or (market_target.isoformat() if market_target else selected_target_time)
        feature, feature_distance = nearest(features, compare_target, "target_time")
        source_forecast, source_distance = nearest(source, compare_target, "forecast_time")
        forecast_feature_target = None
        if forecast.get("feature_json"):
            try:
                forecast_feature_target = json.loads(forecast["feature_json"]).get("target_time")
            except Exception:
                forecast_feature_target = None
        feature_aligned = feature is not None and feature_distance is not None and feature_distance <= match_tolerance_hours
        source_aligned = source_forecast is not None and source_distance is not None and source_distance <= match_tolerance_hours
        market_selected_aligned = True
        selected_dt = parse_dt(selected_target_time)
        if market_target is not None and selected_dt is not None:
            market_selected_aligned = abs((market_target - selected_dt).total_seconds()) / 3600 <= match_tolerance_hours
        link_market_aligned = True
        link_dt = parse_dt(link_target)
        if link_dt is not None and market_target is not None:
            link_market_aligned = abs((link_dt - market_target).total_seconds()) / 3600 <= match_tolerance_hours
        if not market:
            blocker = "MARKET_MISSING"
        elif not market_selected_aligned:
            blocker = "MARKET_TARGET_NOT_SELECTED_WINDOW"
        elif not link:
            blocker = "LINK_MISSING"
        elif not link_market_aligned:
            blocker = "LINK_TARGET_MISMATCH"
        elif not source_aligned:
            blocker = "SOURCE_FORECAST_TARGET_MISSING"
        elif not feature_aligned:
            blocker = "FEATURE_TARGET_MISSING"
        elif not snapshot:
            blocker = "SNAPSHOT_MISSING"
        elif not forecast:
            blocker = "FORECAST_MISSING"
        elif not ranking:
            blocker = "RANKING_MISSING"
        elif dec(ranking.get("estimated_edge")) and dec(ranking.get("estimated_edge")) > 0:
            blocker = "POSITIVE_EV_ALIGNED"
        else:
            blocker = "EV_NOT_POSITIVE_ALIGNED"
        rows.append({
            "ticker": ticker,
            "market_target_time": iso(market_target),
            "market_close_time": iso(market.get("close_time")),
            "market_expected_expiration_time": iso(market.get("expected_expiration_time")),
            "link_target_time": iso(link.get("target_time")),
            "link_detected_at": iso(link.get("detected_at")),
            "snapshot_at": iso(snapshot.get("captured_at")),
            "source_forecast_time": iso(source_forecast.get("forecast_time") if source_forecast else None),
            "source_forecast_generated_at": iso(source_forecast.get("forecast_generated_at") if source_forecast else None),
            "feature_target_time": iso(feature.get("target_time") if feature else None),
            "feature_generated_at": iso(feature.get("generated_at") if feature else None),
            "feature_distance_hours": feature_distance,
            "forecast_at": iso(forecast.get("forecasted_at")),
            "forecast_feature_target_time": iso(forecast_feature_target),
            "ranking_at": iso(ranking.get("ranked_at")),
            "ranking_edge": ranking.get("estimated_edge"),
            "first_alignment_blocker": blocker,
        })
    counts = {}
    for row in rows:
        key = row.get("first_alignment_blocker") or "UNKNOWN"
        counts[key] = counts.get(key, 0) + 1
    payload["ok"] = True
    payload["rows"] = rows
    payload["summary"] = {
        "row_count": len(rows),
        "feature_aligned_rows": sum(1 for row in rows if row.get("feature_target_time") and row.get("feature_distance_hours") is not None and row["feature_distance_hours"] <= match_tolerance_hours),
        "forecast_rows": sum(1 for row in rows if row.get("forecast_at")),
        "ranking_rows": sum(1 for row in rows if row.get("ranking_at")),
        "positive_ev_aligned_rows": counts.get("POSITIVE_EV_ALIGNED", 0),
        "blocker_counts": counts,
    }
except Exception as exc:
    payload["error"] = str(exc)
print(json.dumps(payload, sort_keys=True))
"""
    script = script.replace("__DB_PATH__", repr(db_path))
    script = script.replace("__SELECTED_TARGET_TIME__", repr(selected_target_time))
    script = script.replace("__SELECTED_TICKERS_JSON__", repr(json.dumps(selected_tickers)))
    script = script.replace("__MATCH_TOLERANCE_HOURS__", repr(int(match_tolerance_hours)))
    return "python3 - <<'PY'\n" + script.strip() + "\nPY"


def _json_from_named_probe(results: list[RemoteProbeResult], name: str) -> dict[str, Any]:
    for result in results:
        if result.name == name:
            parsed = _json_from_probe(result)
            return parsed if isinstance(parsed, dict) else {}
    return {}


def _summary(
    *,
    selected: dict[str, Any],
    alignment_payload: dict[str, Any],
    alignment_rows: list[dict[str, Any]],
    patch_status: dict[str, bool],
    results: list[RemoteProbeResult],
) -> dict[str, Any]:
    remote_summary = alignment_payload.get("summary") if isinstance(alignment_payload.get("summary"), dict) else {}
    r57_loop = {}
    failed = [result.name for result in results if not result.ok]
    blockers: dict[str, int] = remote_summary.get("blocker_counts") or {}
    return {
        "selected_target_time": selected.get("selected_target_time"),
        "selected_ticker_count": len(selected.get("selected_tickers") or []),
        "selected_window_rows": selected.get("selected_window_rows"),
        "remote_probe_ok": bool(alignment_payload.get("ok")),
        "failed_probe_names": failed,
        "feature_aligned_rows": _int_or_zero(remote_summary.get("feature_aligned_rows")),
        "forecast_rows": _int_or_zero(remote_summary.get("forecast_rows")),
        "ranking_rows": _int_or_zero(remote_summary.get("ranking_rows")),
        "positive_ev_aligned_rows": _int_or_zero(remote_summary.get("positive_ev_aligned_rows")),
        "blocker_counts": blockers,
        "first_alignment_blocker": _dominant_blocker(blockers),
        "r57_patch_complete": all(patch_status.values()),
        "alignment_row_count": len(alignment_rows),
        "r57_loop": r57_loop,
    }


def _dominant_blocker(blockers: dict[str, int]) -> str:
    if not blockers:
        return "NO_ALIGNMENT_ROWS"
    for blocker in (
        "MARKET_MISSING",
        "MARKET_TARGET_NOT_SELECTED_WINDOW",
        "LINK_MISSING",
        "LINK_TARGET_MISMATCH",
        "SOURCE_FORECAST_TARGET_MISSING",
        "FEATURE_TARGET_MISSING",
        "SNAPSHOT_MISSING",
        "FORECAST_MISSING",
        "RANKING_MISSING",
        "EV_NOT_POSITIVE_ALIGNED",
        "POSITIVE_EV_ALIGNED",
    ):
        if blockers.get(blocker):
            return blocker
    return sorted(blockers.items(), key=lambda item: item[1], reverse=True)[0][0]


def _decision(summary: dict[str, Any]) -> dict[str, Any]:
    if not summary["remote_probe_ok"]:
        status = "ALIGNMENT_PROBE_FAILED"
        blocker = "REMOTE_ALIGNMENT_PROBE_FAILED"
        reason = "R58 could not inspect the cloud weather alignment state."
        command = (
            "kalshi-bot phase3bb-r58-weather-selected-window-forecast-feature-alignment-repair "
            "--output-dir reports/phase3bb_r58 --reports-dir reports"
        )
        next_step = "Phase 3BB-R58 - Repair remote alignment probe"
    elif not summary["r57_patch_complete"]:
        status = "R57_FORECAST_LOOP_PATCH_INCOMPLETE"
        blocker = "R57_FORECAST_LOOP_STILL_REDISCOVERS_WINDOW"
        reason = "R57 still has the zero-ticker forecast-loop regression."
        command = "pytest tests/test_phase3bb_r57_weather_selected_window_pipeline.py -q"
        next_step = "Patch R57 forecast loop before rerun"
    elif summary["feature_aligned_rows"] > 0 and summary["forecast_rows"] == 0:
        status = "R57_PATCHED_RERUN_SELECTED_WINDOW_PIPELINE"
        blocker = "FORECAST_MISSING_AFTER_FEATURE_ALIGNMENT"
        reason = "Selected tickers have aligned feature/source rows, but no weather_v2 forecasts were written."
        command = (
            "kalshi-bot phase3bb-r57-weather-selected-window-pipeline-speed-repair "
            "--output-dir reports/phase3bb_r57 --reports-dir reports --max-wait-seconds 420 "
            "--poll-interval-seconds 15 --pipeline-timeout-seconds 150 "
            "--per-ticker-timeout-seconds 25 --forecast-limit 1"
        )
        next_step = "Phase 3BB-R57 - Rerun patched selected-window pipeline"
    elif summary["forecast_rows"] > 0 and summary["ranking_rows"] == 0:
        status = "FORECAST_ALIGNED_RANKING_MISSING"
        blocker = "RANKING_MISSING"
        reason = "Selected tickers have forecasts, but rankings are missing."
        command = (
            "kalshi-bot phase3bb-r2-weather-fast-lane "
            "--output-dir reports/phase3bb_r2 --reports-dir reports"
        )
        next_step = "Phase 3BB-R2 - Weather ranking fast-lane"
    elif summary["positive_ev_aligned_rows"] > 0:
        status = "WEATHER_POSITIVE_EV_ALIGNED"
        blocker = "PAPER_GATE_RECHECK"
        reason = "Selected-window rows are aligned and have positive EV; recheck paper gate."
        command = "kalshi-bot phase3bb-r8-unified-paper-gate --output-dir reports/phase3bb_r8 --reports-dir reports"
        next_step = "Paper-only gate recheck"
    else:
        status = "WEATHER_ALIGNED_EV_NOT_POSITIVE"
        blocker = summary["first_alignment_blocker"]
        reason = "Selected-window rows are aligned enough to diagnose; no paper-ready EV exists yet."
        command = (
            "kalshi-bot phase3bb-r52-weather-ev-fair-value-diagnostic "
            "--output-dir reports/phase3bb_r52 --reports-dir reports"
        )
        next_step = "Phase 3BB-R52 - Weather EV diagnostic"
    return {
        "status": status,
        "first_hard_blocker": blocker,
        "primary_reason": reason,
        "operator_next_command": command,
        "next_codex_step": next_step,
        "paper_trades_allowed": False,
        "live_demo_orders_allowed": False,
    }


def _render_executive_summary(payload: dict[str, Any]) -> str:
    decision = payload["decision"]
    summary = payload["summary"]
    lines = _metadata_lines(payload, "# Phase 3BB-R58 Weather Selected-Window Alignment")
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
            f"- Selected target: `{summary['selected_target_time']}`",
            f"- Selected tickers: `{summary['selected_ticker_count']}`",
            f"- Feature-aligned rows: `{summary['feature_aligned_rows']}`",
            f"- Forecast rows: `{summary['forecast_rows']}`",
            f"- Ranking rows: `{summary['ranking_rows']}`",
            f"- R57 patch complete: `{summary['r57_patch_complete']}`",
            "",
            "## Why",
            "",
            decision["primary_reason"],
            "",
            "## Next",
            "",
            "```bash",
            decision["operator_next_command"],
            "```",
        ]
    )
    return "\n".join(lines) + "\n"


def _render_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Weather Selected-Window Alignment",
        "",
        f"Status: `{payload['decision']['status']}`",
        f"First blocker: `{payload['decision']['first_hard_blocker']}`",
        "",
        "## Blockers",
        "",
        "```json",
        json.dumps(payload["summary"].get("blocker_counts") or {}, indent=2, sort_keys=True),
        "```",
        "",
        "## Patch Status",
        "",
        "| Check | OK |",
        "|---|---:|",
    ]
    for key, value in payload["patch_status"].items():
        lines.append(f"| {key} | {value} |")
    lines.extend(
        [
            "",
            "## Guardrails",
            "",
            "- Paper-only diagnostic.",
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
            "",
            "```bash",
            decision["operator_next_command"],
            "```",
            "",
            "Do not create paper trades or live/demo orders from this phase.",
        ]
    ) + "\n"


def _render_operator_command(payload: dict[str, Any]) -> str:
    return "#!/usr/bin/env bash\nset -euo pipefail\n" + payload["decision"]["operator_next_command"] + "\n"
