from __future__ import annotations

import csv
import json
import shlex
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.phase3bb_acceleration import (
    _metadata,
    _metadata_lines,
    _safety_flags,
    _write_manifest,
)
from kalshi_predictor.phase3bb_r12_cloud_bootstrap import (
    CloudBootstrapTarget,
    ProbeRunner,
    RemoteProbe,
    RemoteProbeResult,
    _resolve_target,
    _result_payload,
    _run_ssh_probe,
)
from kalshi_predictor.phase3bb_r44_weather_catalog_hook_runtime_verification import (
    _mark_executable,
    _target_payload,
)
from kalshi_predictor.utils.time import utc_now

PHASE3BB_R52_VERSION = "phase3bb_r52_weather_ev_fair_value_diagnostic_v1"
DEFAULT_OUTPUT_DIR = Path("reports/phase3bb_r52")
DEFAULT_REPORTS_DIR = Path("reports")
DEFAULT_PER_PROBE_TIMEOUT_SECONDS = 60

ROW_FIELDS = [
    "ticker",
    "market_title",
    "target_time",
    "target_window_state",
    "forecast_probability",
    "yes_fair_value",
    "no_fair_value",
    "market_mid_probability",
    "forecast_minus_mid",
    "best_yes_bid",
    "best_yes_ask",
    "best_no_bid",
    "best_no_ask",
    "yes_edge",
    "no_edge",
    "best_side",
    "best_price",
    "estimated_edge",
    "edge_to_positive",
    "edge_to_min_threshold",
    "opportunity_score",
    "score_to_threshold",
    "spread",
    "liquidity",
    "liquidity_score",
    "spread_score",
    "time_to_close_minutes",
    "first_ev_blocker",
    "explanation",
]


@dataclass(frozen=True)
class Phase3BBR52WeatherEvFairValueDiagnosticArtifacts:
    output_dir: Path
    executive_summary_path: Path
    markdown_path: Path
    json_path: Path
    rows_csv_path: Path
    summary_csv_path: Path
    probe_csv_path: Path
    operator_command_path: Path
    next_actions_path: Path
    manifest_path: Path


def write_phase3bb_r52_weather_ev_fair_value_diagnostic_report(
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
    limit: int = 100,
    per_probe_timeout_seconds: int = DEFAULT_PER_PROBE_TIMEOUT_SECONDS,
    probe_runner: ProbeRunner | None = None,
) -> Phase3BBR52WeatherEvFairValueDiagnosticArtifacts:
    payload = build_phase3bb_r52_weather_ev_fair_value_diagnostic(
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
        limit=limit,
        per_probe_timeout_seconds=per_probe_timeout_seconds,
        probe_runner=probe_runner,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    executive_summary_path = output_dir / "EXECUTIVE_SUMMARY.md"
    markdown_path = output_dir / "weather_ev_fair_value_diagnostic.md"
    json_path = output_dir / "weather_ev_fair_value_diagnostic.json"
    rows_csv_path = output_dir / "weather_ev_rows.csv"
    summary_csv_path = output_dir / "weather_ev_summary.csv"
    probe_csv_path = output_dir / "remote_probe_results.csv"
    operator_command_path = output_dir / "operator_next_command.sh"
    next_actions_path = output_dir / "NEXT_ACTIONS.md"
    manifest_path = output_dir / "MANIFEST.sha256"

    executive_summary_path.write_text(_render_executive_summary(payload), encoding="utf-8")
    markdown_path.write_text(_render_markdown(payload), encoding="utf-8")
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")
    _write_rows_csv(rows_csv_path, payload["weather_ev_rows"], ROW_FIELDS)
    _write_rows_csv(summary_csv_path, [payload["summary"]])
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
            summary_csv_path,
            probe_csv_path,
            operator_command_path,
            next_actions_path,
        ],
    )
    return Phase3BBR52WeatherEvFairValueDiagnosticArtifacts(
        output_dir=output_dir,
        executive_summary_path=executive_summary_path,
        markdown_path=markdown_path,
        json_path=json_path,
        rows_csv_path=rows_csv_path,
        summary_csv_path=summary_csv_path,
        probe_csv_path=probe_csv_path,
        operator_command_path=operator_command_path,
        next_actions_path=next_actions_path,
        manifest_path=manifest_path,
    )


def build_phase3bb_r52_weather_ev_fair_value_diagnostic(
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
    limit: int = 100,
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
        "command": "kalshi-bot phase3bb-r52-weather-ev-fair-value-diagnostic",
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
    results = [
        runner(probe, target)
        for probe in _probes(
            target,
            current_window_lookback_hours=current_window_lookback_hours,
            limit=limit,
            timeout_seconds=per_probe_timeout_seconds,
        )
    ]
    parsed = _parse_probe_outputs(results, fallback_settings=resolved)
    rows = parsed["weather_ev_rows"]
    summary = _summary(rows, parsed=parsed)
    decision = _decision(summary)
    safety = {
        **_safety_flags(),
        "paper_only": True,
        "diagnostic_only": True,
        "weather_ev_fair_value_diagnostic": True,
        "ssh_read_only_commands_executed": len(results),
        "ssh_write_capable_commands_executed": 0,
        "runs_weather_forecast": False,
        "runs_weather_fast_lane": False,
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
        "phase": "3BB-R52-WEATHER-EV-FAIR-VALUE-DIAGNOSTIC",
        "phase_version": PHASE3BB_R52_VERSION,
        "mode": "PAPER_ONLY_WEATHER_EV_FAIR_VALUE_DIAGNOSTIC",
        "reports_dir": str(reports_dir),
        "cloud_target": _target_payload(target),
        "remote_probe_results": [_result_payload(result) for result in results],
        "parsed_weather_ev_state": parsed,
        "summary": summary,
        "decision": decision,
        "weather_ev_rows": rows,
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
    current_window_lookback_hours: int,
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
            "remote_thresholds",
            _remote_thresholds_command(target),
            timeout_seconds,
        ),
        RemoteProbe(
            "command_registry",
            (
                f"cd {app} && for cmd in "
                "phase3bb-r8-unified-paper-gate "
                "phase3bb-r51-weather-ranking-path-repair "
                "phase3bb-r52-weather-ev-fair-value-diagnostic; do "
                ".venv/bin/kalshi-bot \"$cmd\" --help >/dev/null || exit 30; "
                "done; echo COMMAND_REGISTRY_OK"
            ),
            timeout_seconds,
        ),
        RemoteProbe(
            "weather_ev_state",
            _weather_ev_state_command(
                target.db_path,
                current_window_lookback_hours=current_window_lookback_hours,
                limit=limit,
            ),
            timeout_seconds,
        ),
    ]


def _remote_thresholds_command(target: CloudBootstrapTarget) -> str:
    app = shlex.quote(target.app_path)
    env = shlex.quote(target.env_path)
    script = """
import json
from kalshi_predictor.config import get_settings

settings = get_settings()
print(json.dumps({
    "opportunity_min_edge": str(settings.opportunity_min_edge),
    "opportunity_min_score": str(settings.opportunity_min_score),
    "opportunity_max_spread": str(settings.opportunity_max_spread),
    "opportunity_min_liquidity": str(settings.opportunity_min_liquidity),
    "opportunity_min_time_to_close_minutes": str(settings.opportunity_min_time_to_close_minutes),
}, sort_keys=True))
"""
    return f"cd {app} && set -a && . {env} && set +a && .venv/bin/python -c {shlex.quote(script)}"


def _weather_ev_state_command(
    db_path: str,
    *,
    current_window_lookback_hours: int,
    limit: int,
) -> str:
    script = f"""
import json
import sqlite3
from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

db_path = {db_path!r}
current_window_lookback_hours = {int(current_window_lookback_hours)!r}
limit = {int(limit)!r}
now = datetime.now(timezone.utc)
current_since = now - timedelta(hours=max(current_window_lookback_hours, 0))

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

def latest_by_ticker(rows):
    latest = {{}}
    for row in rows:
        ticker = str(row.get("ticker") or "")
        if ticker and ticker not in latest:
            latest[ticker] = row
    return latest

def load_raw(raw_text):
    if not raw_text:
        return {{}}
    try:
        return json.loads(raw_text)
    except Exception:
        return {{}}

def window_state(target_time):
    target_dt = parse_dt(target_time)
    if target_dt is None:
        return "TARGET_TIME_UNKNOWN"
    if target_dt >= now:
        return "LIVE_OR_FUTURE"
    if target_dt >= current_since:
        return "RECENTLY_EXPIRED"
    return "EXPIRED"

def edge_to_positive(edge):
    if edge is None:
        return None
    return Decimal("0") - edge if edge < 0 else Decimal("0")

payload = {{
    "ok": False,
    "error": None,
    "db_path": db_path,
    "generated_at": iso(now),
    "current_since": iso(current_since),
    "selected_target_time": None,
    "rows": [],
    "summary": {{}},
}}
try:
    conn = sqlite3.connect(f"file:{{db_path}}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    link_rows = [dict(row) for row in conn.execute(
        '''
        select id, ticker, location_key, target_time, target_operator, target_value, detected_at
        from weather_market_links
        where target_time is not null
        order by target_time desc, detected_at desc, id desc
        limit 5000
        '''
    ).fetchall()]
    eligible_links = []
    seen_eligible = set()
    for link in link_rows:
        ticker = str(link.get("ticker") or "")
        target_time = parse_dt(link.get("target_time"))
        if not ticker or ticker in seen_eligible:
            continue
        if target_time is None or target_time < current_since:
            continue
        link["_parsed_target_time"] = target_time
        seen_eligible.add(ticker)
        eligible_links.append(link)
    future_target_times = sorted({{iso(link["_parsed_target_time"]) for link in eligible_links if link["_parsed_target_time"] >= now}})
    selected_target_time = future_target_times[0] if future_target_times else None
    selected_link_rows = [
        link for link in eligible_links
        if selected_target_time is not None and iso(link["_parsed_target_time"]) == selected_target_time
    ]
    if not selected_link_rows:
        selected_link_rows = eligible_links
    payload["selected_target_time"] = selected_target_time
    links = []
    seen = set()
    for link in selected_link_rows:
        ticker = str(link.get("ticker") or "")
        if not ticker or ticker in seen:
            continue
        seen.add(ticker)
        links.append(link)
        if len(links) >= limit:
            break
    tickers = [str(link["ticker"]) for link in links]
    markets = {{}}
    forecasts = {{}}
    snapshots = {{}}
    rankings = {{}}
    if tickers:
        placeholders = ",".join("?" for _ in tickers)
        markets = {{str(row["ticker"]): dict(row) for row in conn.execute(
            f'''
            select ticker, title, status, series_ticker, close_time, expected_expiration_time, expiration_time
            from markets where ticker in ({{placeholders}})
            ''',
            tickers,
        ).fetchall()}}
        forecasts = latest_by_ticker([dict(row) for row in conn.execute(
            f'''
            select id, ticker, forecasted_at, model_name, yes_probability, market_mid_probability,
                   best_yes_bid, best_yes_ask
            from forecasts
            where ticker in ({{placeholders}}) and model_name = 'weather_v2'
            order by ticker, forecasted_at desc, id desc
            ''',
            tickers,
        ).fetchall()])
        snapshots = latest_by_ticker([dict(row) for row in conn.execute(
            f'''
            select id, ticker, captured_at, status, best_yes_bid, best_yes_ask, best_no_bid,
                   best_no_ask, spread, last_price_dollars, volume_fp, open_interest_fp,
                   raw_market_json, raw_orderbook_json
            from market_snapshots
            where ticker in ({{placeholders}})
            order by ticker, captured_at desc, id desc
            ''',
            tickers,
        ).fetchall()])
        rankings = latest_by_ticker([dict(row) for row in conn.execute(
            f'''
            select id, ticker, ranked_at, forecast_model, forecast_probability, best_side,
                   best_price, estimated_edge, opportunity_score, liquidity_score, spread_score,
                   model_confidence_score, time_score, spread, liquidity, midpoint,
                   time_to_close_minutes, reason, raw_json
            from market_rankings
            where ticker in ({{placeholders}}) and forecast_model = 'weather_v2'
            order by ticker, ranked_at desc, id desc
            ''',
            tickers,
        ).fetchall()])

    rows = []
    blockers = Counter()
    for link in links:
        ticker = str(link.get("ticker"))
        market = markets.get(ticker) or {{}}
        forecast = forecasts.get(ticker) or {{}}
        snapshot = snapshots.get(ticker) or {{}}
        ranking = rankings.get(ticker) or {{}}
        raw_ranking = load_raw(ranking.get("raw_json"))
        forecast_probability = dec(forecast.get("yes_probability")) or dec(ranking.get("forecast_probability"))
        yes_fair = forecast_probability
        no_fair = Decimal("1") - forecast_probability if forecast_probability is not None else None
        yes_ask = dec(snapshot.get("best_yes_ask")) or dec(forecast.get("best_yes_ask")) or dec(raw_ranking.get("best_yes_ask"))
        no_ask = dec(snapshot.get("best_no_ask")) or dec(raw_ranking.get("best_no_ask"))
        yes_bid = dec(snapshot.get("best_yes_bid"))
        no_bid = dec(snapshot.get("best_no_bid"))
        midpoint = dec(forecast.get("market_mid_probability")) or dec(ranking.get("midpoint"))
        if midpoint is None and yes_bid is not None and yes_ask is not None:
            midpoint = (yes_bid + yes_ask) / Decimal("2")
        yes_edge = yes_fair - yes_ask if yes_fair is not None and yes_ask is not None else None
        no_edge = no_fair - no_ask if no_fair is not None and no_ask is not None else None
        candidates = []
        if yes_edge is not None and yes_ask is not None:
            candidates.append(("BUY_YES", yes_ask, yes_edge))
        if no_edge is not None and no_ask is not None:
            candidates.append(("BUY_NO", no_ask, no_edge))
        if candidates:
            best_side, best_price, computed_edge = max(candidates, key=lambda item: item[2])
        else:
            best_side = ranking.get("best_side")
            best_price = dec(ranking.get("best_price"))
            computed_edge = dec(ranking.get("estimated_edge"))
        ranking_edge = dec(ranking.get("estimated_edge"))
        edge = ranking_edge if ranking_edge is not None else computed_edge
        score = dec(ranking.get("opportunity_score"))
        spread = dec(ranking.get("spread")) or dec(snapshot.get("spread"))
        liquidity = dec(ranking.get("liquidity"))
        liquidity_score = dec(ranking.get("liquidity_score"))
        spread_score = dec(ranking.get("spread_score"))
        if not ranking:
            blocker = "RANKING_MISSING"
            explanation = "No weather_v2 ranking exists for this linked weather row."
        elif forecast_probability is None:
            blocker = "FORECAST_PROBABILITY_MISSING"
            explanation = "Ranking exists but the forecast probability could not be read."
        elif not candidates and best_price is None:
            blocker = "BOOK_MISSING"
            explanation = "Forecast exists but no executable ask side was visible."
        elif edge is not None and edge <= Decimal("0"):
            blocker = "FAIR_VALUE_BELOW_EXECUTABLE_PRICE"
            explanation = "The best executable ask is above the model fair probability for both YES/NO sides."
        else:
            blocker = "EV_POSITIVE_OR_FILTERED_LATER"
            explanation = "Raw EV is positive or unresolved; downstream score/liquidity/spread gates should decide."
        blockers[blocker] += 1
        rows.append({{
            "ticker": ticker,
            "market_title": market.get("title"),
            "target_time": iso(parse_dt(link.get("target_time"))),
            "target_window_state": window_state(link.get("target_time")),
            "forecast_probability": dstr(forecast_probability),
            "yes_fair_value": dstr(yes_fair),
            "no_fair_value": dstr(no_fair),
            "market_mid_probability": dstr(midpoint),
            "forecast_minus_mid": dstr(forecast_probability - midpoint) if forecast_probability is not None and midpoint is not None else None,
            "best_yes_bid": dstr(yes_bid),
            "best_yes_ask": dstr(yes_ask),
            "best_no_bid": dstr(no_bid),
            "best_no_ask": dstr(no_ask),
            "yes_edge": dstr(yes_edge),
            "no_edge": dstr(no_edge),
            "best_side": best_side,
            "best_price": dstr(best_price),
            "estimated_edge": dstr(edge),
            "edge_to_positive": dstr(edge_to_positive(edge)),
            "edge_to_min_threshold": None,
            "opportunity_score": dstr(score),
            "score_to_threshold": None,
            "spread": dstr(spread),
            "liquidity": dstr(liquidity),
            "liquidity_score": dstr(liquidity_score),
            "spread_score": dstr(spread_score),
            "time_to_close_minutes": ranking.get("time_to_close_minutes"),
            "first_ev_blocker": blocker,
            "explanation": explanation,
            "forecasted_at": forecast.get("forecasted_at"),
            "ranked_at": ranking.get("ranked_at"),
            "snapshot_at": snapshot.get("captured_at"),
        }})
    payload["rows"] = rows
    payload["summary"] = {{
        "selected_target_time": selected_target_time,
        "linked_weather_rows": len(links),
        "ranked_weather_rows": sum(1 for row in rows if row["first_ev_blocker"] != "RANKING_MISSING"),
        "positive_ev_rows": sum(1 for row in rows if dec(row.get("estimated_edge")) is not None and dec(row.get("estimated_edge")) > Decimal("0")),
        "non_positive_ev_rows": sum(1 for row in rows if dec(row.get("estimated_edge")) is not None and dec(row.get("estimated_edge")) <= Decimal("0")),
        "live_or_future_rows": sum(1 for row in rows if row["target_window_state"] == "LIVE_OR_FUTURE"),
        "recently_expired_rows": sum(1 for row in rows if row["target_window_state"] == "RECENTLY_EXPIRED"),
        "expired_rows": sum(1 for row in rows if row["target_window_state"] == "EXPIRED"),
        "first_ev_blocker_counts": dict(blockers),
    }}
    payload["ok"] = True
except Exception as exc:
    payload["error"] = str(exc)
print(json.dumps(payload, sort_keys=True))
"""
    return "python3 -c " + shlex.quote(script)


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _parse_probe_outputs(
    results: list[RemoteProbeResult],
    *,
    fallback_settings: Settings,
) -> dict[str, Any]:
    by_name = {result.name: result for result in results}
    thresholds = _json_from_stdout(by_name.get("remote_thresholds"))
    if not thresholds:
        thresholds = {
            "opportunity_min_edge": str(fallback_settings.opportunity_min_edge),
            "opportunity_min_score": str(fallback_settings.opportunity_min_score),
            "opportunity_max_spread": str(fallback_settings.opportunity_max_spread),
            "opportunity_min_liquidity": str(fallback_settings.opportunity_min_liquidity),
            "opportunity_min_time_to_close_minutes": str(
                fallback_settings.opportunity_min_time_to_close_minutes
            ),
        }
    state = _json_from_stdout(by_name.get("weather_ev_state"))
    rows = state.get("rows") if isinstance(state, dict) else []
    rows = rows if isinstance(rows, list) else []
    _apply_threshold_gaps(rows, thresholds)
    return {
        "remote_time_utc": (by_name.get("remote_time_utc").stdout.strip() if by_name.get("remote_time_utc") else ""),
        "writer": _json_from_stdout(by_name.get("db_writer_monitor")),
        "command_registry_ok": bool(
            by_name.get("command_registry")
            and by_name["command_registry"].ok
            and "COMMAND_REGISTRY_OK" in by_name["command_registry"].stdout
        ),
        "thresholds": thresholds,
        "weather_ev_state_ok": bool(state.get("ok")) if isinstance(state, dict) else False,
        "weather_ev_state_error": state.get("error") if isinstance(state, dict) else "missing_weather_ev_state",
        "weather_ev_rows": rows,
        "weather_ev_state_summary": state.get("summary", {}) if isinstance(state, dict) else {},
    }


def _apply_threshold_gaps(rows: list[dict[str, Any]], thresholds: dict[str, Any]) -> None:
    min_edge = _decimal(thresholds.get("opportunity_min_edge"))
    min_score = _decimal(thresholds.get("opportunity_min_score"))
    for row in rows:
        edge = _decimal(row.get("estimated_edge"))
        score = _decimal(row.get("opportunity_score"))
        row["edge_to_min_threshold"] = _format_decimal(min_edge - edge) if min_edge is not None and edge is not None else None
        row["score_to_threshold"] = _format_decimal(min_score - score) if min_score is not None and score is not None else None


def _summary(rows: list[dict[str, Any]], *, parsed: dict[str, Any]) -> dict[str, Any]:
    blockers = Counter(str(row.get("first_ev_blocker") or "UNKNOWN") for row in rows)
    ranked_rows = [row for row in rows if row.get("first_ev_blocker") != "RANKING_MISSING"]
    positive = [_decimal(row.get("estimated_edge")) for row in ranked_rows]
    positive_values = [value for value in positive if value is not None and value > 0]
    best_row = _best_row(rows)
    return {
        "status": "",
        "linked_weather_rows": len(rows),
        "ranked_weather_rows": len(ranked_rows),
        "positive_ev_rows": len(positive_values),
        "non_positive_ev_rows": sum(1 for value in positive if value is not None and value <= 0),
        "live_or_future_rows": sum(1 for row in rows if row.get("target_window_state") == "LIVE_OR_FUTURE"),
        "recently_expired_rows": sum(1 for row in rows if row.get("target_window_state") == "RECENTLY_EXPIRED"),
        "first_ev_blocker": blockers.most_common(1)[0][0] if blockers else "NO_WEATHER_ROWS",
        "first_ev_blocker_counts": dict(blockers),
        "best_ticker": best_row.get("ticker") if best_row else None,
        "best_estimated_edge": best_row.get("estimated_edge") if best_row else None,
        "best_edge_to_positive": best_row.get("edge_to_positive") if best_row else None,
        "best_side": best_row.get("best_side") if best_row else None,
        "best_price": best_row.get("best_price") if best_row else None,
        "command_registry_ok": parsed.get("command_registry_ok"),
        "writer_status": (parsed.get("writer") or {}).get("status"),
    }


def _decision(summary: dict[str, Any]) -> dict[str, Any]:
    if not summary.get("command_registry_ok"):
        status = "WEATHER_EV_DIAGNOSTIC_COMMAND_REGISTRY_INCOMPLETE"
        blocker = "COMMAND_REGISTRY_MISSING"
        command = "kalshi-bot phase3bb-r12-cloud-bootstrap-verification --output-dir reports/phase3bb_r12 --reports-dir reports"
    elif summary["ranked_weather_rows"] <= 0:
        status = "WEATHER_EV_DIAGNOSTIC_RANKING_ROWS_MISSING"
        blocker = "RANKING_MISSING"
        command = (
            "kalshi-bot phase3bb-r51-weather-ranking-path-repair "
            "--output-dir reports/phase3bb_r51 --reports-dir reports"
        )
    elif summary["positive_ev_rows"] > 0:
        status = "WEATHER_POSITIVE_EV_FOUND_REFRESH_PAPER_GATE"
        blocker = "PAPER_GATE_REFRESH_NEEDED"
        command = "kalshi-bot phase3bb-r8-unified-paper-gate --output-dir reports/phase3bb_r8 --reports-dir reports"
    elif summary["live_or_future_rows"] <= 0:
        status = "WEATHER_EV_WINDOW_EXPIRED_AFTER_RANKING"
        blocker = "TARGET_WINDOW_EXPIRED"
        command = (
            "kalshi-bot phase3bb-r47-weather-current-window-series-discovery-linkability-repair "
            "--output-dir reports/phase3bb_r47 --reports-dir reports"
        )
    else:
        status = "WEATHER_EV_NOT_POSITIVE_EXPLAINED"
        blocker = summary.get("first_ev_blocker") or "EV_NOT_POSITIVE"
        command = "kalshi-bot phase3bb-r40-cloud-scheduler-runtime-monitor --output-dir reports/phase3bb_r40 --reports-dir reports"
    summary["status"] = status
    summary["first_hard_blocker"] = blocker
    return {
        "status": status,
        "first_hard_blocker": blocker,
        "operator_next_command": command,
        "paper_trades_allowed": False,
        "live_demo_orders_allowed": False,
    }


def _best_row(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    ranked = [row for row in rows if _decimal(row.get("estimated_edge")) is not None]
    if not ranked:
        return rows[0] if rows else None
    return max(ranked, key=lambda row: _decimal(row.get("estimated_edge")) or 0)


def _decimal(value: Any):
    from decimal import Decimal, InvalidOperation

    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _format_decimal(value: Any) -> str | None:
    decimal_value = _decimal(value)
    if decimal_value is None:
        return None
    return format(decimal_value.quantize(Decimal("0.0001")), "f")


def _json_from_stdout(result: RemoteProbeResult | None) -> dict[str, Any]:
    if result is None or not result.stdout:
        return {}
    text = result.stdout.strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        for line in reversed(lines):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    return {}


def _render_executive_summary(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    decision = payload["decision"]
    lines = _metadata_lines(payload, "# Phase 3BB-R52 Weather EV / Fair-Value Diagnostic")
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
        f"- Linked weather rows inspected: `{summary['linked_weather_rows']}`",
        f"- Ranked weather rows: `{summary['ranked_weather_rows']}`",
        f"- Positive-EV rows: `{summary['positive_ev_rows']}`",
        f"- Non-positive-EV rows: `{summary['non_positive_ev_rows']}`",
        f"- Live/future rows: `{summary['live_or_future_rows']}`",
        f"- Best ticker: `{summary.get('best_ticker')}`",
        f"- Best estimated edge: `{summary.get('best_estimated_edge')}`",
        f"- Best edge gap to positive: `{summary.get('best_edge_to_positive')}`",
        "",
        "## Explanation",
        "",
        "R52 compares the weather model's fair value (`YES=p`, `NO=1-p`) against the executable ask prices. "
        "A row stays blocked when the best executable side costs more than its model fair value.",
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
    rows = payload["weather_ev_rows"]
    decision = payload["decision"]
    lines = [
        "# Weather EV / Fair-Value Diagnostic",
        "",
        f"Status: `{decision['status']}`",
        f"First hard blocker: `{decision['first_hard_blocker']}`",
        "",
        "| Ticker | Window | p(YES) | YES ask | NO ask | Best side | Price | Edge | Gap to +EV | Blocker |",
        "|---|---:|---:|---:|---:|---|---:|---:|---:|---|",
    ]
    for row in rows[:25]:
        lines.append(
            "| "
            + " | ".join(
                [
                    str(row.get("ticker") or ""),
                    str(row.get("target_window_state") or ""),
                    str(row.get("forecast_probability") or ""),
                    str(row.get("best_yes_ask") or ""),
                    str(row.get("best_no_ask") or ""),
                    str(row.get("best_side") or ""),
                    str(row.get("best_price") or ""),
                    str(row.get("estimated_edge") or ""),
                    str(row.get("edge_to_positive") or ""),
                    str(row.get("first_ev_blocker") or ""),
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
            "",
            "```bash",
            decision["operator_next_command"],
            "```",
            "",
            "Guardrails:",
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
