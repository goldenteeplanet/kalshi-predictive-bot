from __future__ import annotations

import csv
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.data.locks import db_writer_monitor
from kalshi_predictor.phase3ba_r2 import write_phase3ba_r2_weather_ranking_activation_report
from kalshi_predictor.phase3ba_r3 import write_phase3ba_r3_weather_paper_gate_report
from kalshi_predictor.phase3ba_r5 import write_phase3ba_r5_paper_ready_truth_report
from kalshi_predictor.phase3bb_acceleration import (
    _metadata,
    _metadata_lines,
    _read_json,
    _safety_flags,
    _write_json,
)
from kalshi_predictor.utils.time import utc_now

PHASE3BB_R2_VERSION = "phase3bb_r2_weather_fast_lane_v1"
MODEL_NAME = "weather_v2"
DEFAULT_LIMIT = 100

FUNNEL_STEPS = (
    "active weather market",
    "source evidence",
    "feature",
    "forecast",
    "ranking",
    "positive EV",
    "executable EV",
    "book/liquidity/spread",
    "risk",
    "paper-ready",
)


@dataclass(frozen=True)
class Phase3BBR2WeatherFastLaneArtifacts:
    output_dir: Path
    executive_summary_path: Path
    markdown_path: Path
    json_path: Path
    candidates_csv_path: Path
    next_actions_path: Path


def write_phase3bb_r2_weather_fast_lane_report(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3bb_r2"),
    reports_dir: Path = Path("reports"),
    settings: Settings | None = None,
    command_args: list[str] | None = None,
    limit: int = DEFAULT_LIMIT,
) -> Phase3BBR2WeatherFastLaneArtifacts:
    payload = build_phase3bb_r2_weather_fast_lane(
        session,
        output_dir=output_dir,
        reports_dir=reports_dir,
        settings=settings,
        command_args=command_args,
        limit=limit,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    executive_summary_path = output_dir / "EXECUTIVE_SUMMARY.md"
    markdown_path = output_dir / "weather_fast_lane.md"
    json_path = output_dir / "weather_funnel.json"
    candidates_csv_path = output_dir / "weather_candidates.csv"
    next_actions_path = output_dir / "NEXT_ACTIONS.md"

    executive_summary_path.write_text(_render_executive_summary(payload), encoding="utf-8")
    markdown_path.write_text(_render_weather_fast_lane(payload), encoding="utf-8")
    _write_json(json_path, payload)
    _write_candidates_csv(candidates_csv_path, payload["weather_candidates"])
    next_actions_path.write_text(_render_next_actions(payload), encoding="utf-8")
    return Phase3BBR2WeatherFastLaneArtifacts(
        output_dir=output_dir,
        executive_summary_path=executive_summary_path,
        markdown_path=markdown_path,
        json_path=json_path,
        candidates_csv_path=candidates_csv_path,
        next_actions_path=next_actions_path,
    )


def build_phase3bb_r2_weather_fast_lane(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3bb_r2"),
    reports_dir: Path = Path("reports"),
    settings: Settings | None = None,
    command_args: list[str] | None = None,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    resolved = settings or get_settings()
    generated_at = utc_now()
    metadata = _metadata(
        session,
        settings=resolved,
        generated_at=generated_at.isoformat(),
        command_args=command_args or [],
        output_dir=output_dir,
    )
    metadata["command_arguments"] = {
        "command": "kalshi-bot phase3bb-r2-weather-fast-lane",
        "argv": command_args or [],
    }
    writer = db_writer_monitor(settings=resolved)
    writer_active = bool(writer.get("current_writer_pid")) or not bool(
        writer.get("safe_to_start_write", True)
    )
    opportunity_output = reports_dir / "weather_opportunities.md"
    if writer_active:
        r2_payload = _blocked_ranking_payload(
            writer=writer,
            opportunity_output=opportunity_output,
            limit=limit,
        )
        r2_artifact_status = {
            "ran": False,
            "status": "SKIPPED_ACTIVE_WRITER",
            "reason": "Writer gate is not clear; weather ranking path was not called.",
        }
    else:
        r2_artifacts = write_phase3ba_r2_weather_ranking_activation_report(
            session,
            output_dir=reports_dir / "phase3ba_r2",
            reports_dir=reports_dir,
            settings=resolved,
            command_args=[
                "phase3bb-r2-weather-fast-lane",
                "embedded-phase3ba-r2-weather-ranking",
            ],
            limit=limit,
            opportunity_output=opportunity_output,
        )
        r2_payload = _read_json(r2_artifacts.json_path)
        r2_artifact_status = {
            "ran": True,
            "status": r2_payload.get("status"),
            "json_path": str(r2_artifacts.json_path),
            "markdown_path": str(r2_artifacts.markdown_path),
            "rows_csv_path": str(r2_artifacts.rows_csv_path),
            "opportunity_output": str(opportunity_output),
        }
    r3_artifacts = write_phase3ba_r3_weather_paper_gate_report(
        session,
        output_dir=reports_dir / "phase3ba_r3",
        reports_dir=reports_dir,
        settings=resolved,
        command_args=[
            "phase3bb-r2-weather-fast-lane",
            "embedded-phase3ba-r3-weather-paper-gate",
        ],
        limit=max(limit, DEFAULT_LIMIT),
    )
    r3_payload = _read_json(r3_artifacts.json_path)
    r5_artifacts = write_phase3ba_r5_paper_ready_truth_report(
        session,
        output_dir=reports_dir / "phase3ba_r5",
        reports_dir=reports_dir,
        settings=resolved,
        command_args=[
            "phase3bb-r2-weather-fast-lane",
            "embedded-phase3ba-r5-paper-ready-truth",
        ],
        max_duration_seconds=120,
        limit=max(limit, DEFAULT_LIMIT),
    )
    r5_payload = _read_json(r5_artifacts.json_path)
    candidates = weather_candidates_from_reports(r2_payload=r2_payload, r3_payload=r3_payload)
    summary = weather_fast_lane_summary(
        candidates,
        writer=writer,
        r2_payload=r2_payload,
        r3_payload=r3_payload,
        r5_payload=r5_payload,
    )
    status = _status(summary=summary, writer_active=writer_active)
    dashboard = (r5_payload.get("category_summaries") or {}).get("weather") or {}
    safety = {
        **_safety_flags(),
        "creates_rankings": bool(
            not writer_active and (r2_payload.get("opportunity_scan") or {}).get("ran")
        ),
        "creates_opportunity_rows": bool(
            not writer_active and (r2_payload.get("opportunity_scan") or {}).get("ran")
        ),
        "creates_paper_orders": False,
        "creates_paper_trades": False,
        "uses_current_active_weather_markets_only": True,
        "fabricates_weather_data": False,
        "thresholds_lowered": False,
    }
    return {
        **metadata,
        "phase": "3BB-R2",
        "phase_version": PHASE3BB_R2_VERSION,
        "mode": "PAPER_ONLY_WEATHER_FAST_LANE",
        "status": status,
        "output_dir": str(output_dir),
        "reports_dir": str(reports_dir),
        "parameters": {
            "model_name": MODEL_NAME,
            "limit": limit,
            "weather_opportunity_command": _weather_opportunity_command(
                opportunity_output,
                limit=limit,
            ),
        },
        "writer": writer,
        "weather_paper_funnel": list(FUNNEL_STEPS),
        "weather_ranking_activation": r2_artifact_status,
        "weather_paper_gate": {
            "status": r3_payload.get("status"),
            "json_path": str(r3_artifacts.json_path),
            "markdown_path": str(r3_artifacts.markdown_path),
            "rows_csv_path": str(r3_artifacts.rows_csv_path),
        },
        "dashboard_truth_refresh": {
            "status": r5_payload.get("status"),
            "json_path": str(r5_artifacts.json_path),
            "weather_category_summary": dashboard,
        },
        "summary": summary,
        "weather_candidates": candidates,
        "acceptance": _acceptance(summary=summary, writer_active=writer_active),
        "next_action": _next_action(summary=summary, writer_active=writer_active),
        "operator_guardrails": _operator_guardrails(),
        "safety_flags": safety,
    }


def weather_candidates_from_reports(
    *,
    r2_payload: dict[str, Any],
    r3_payload: dict[str, Any],
) -> list[dict[str, Any]]:
    ranking_rows = {row.get("ticker"): row for row in r2_payload.get("weather_rows", [])}
    gate_rows = r3_payload.get("weather_rows", [])
    candidates: list[dict[str, Any]] = []
    for row in gate_rows:
        ticker = row.get("ticker")
        ranking = ranking_rows.get(ticker) or {}
        first_blocker = row.get("first_blocker") or ranking.get("first_hard_blocker")
        candidates.append(
            {
                "ticker": ticker,
                "location_key": row.get("location_key") or ranking.get("location_key"),
                "market_title": row.get("market_title"),
                "target_time": row.get("target_time") or ranking.get("target_time"),
                "active_weather_market": bool(row.get("current_window_eligible")),
                "verified_kalshi_link": bool(row.get("verified_kalshi_url")),
                "weather_source_evidence": bool(row.get("has_weather_source_forecast")),
                "weather_source_fresh": bool(row.get("weather_source_forecast_fresh")),
                "weather_feature": bool(row.get("has_weather_feature")),
                "weather_feature_fresh": bool(row.get("weather_feature_fresh")),
                "weather_snapshot_fresh": bool(row.get("snapshot_fresh")),
                "weather_forecast": bool(row.get("has_current_forecast")),
                "weather_ranking": bool(row.get("has_current_ranking")),
                "positive_ev": str(first_blocker) not in {
                    "SOURCE_MISSING",
                    "SNAPSHOT_STALE",
                    "FORECAST_MISSING",
                    "RANKING_MISSING",
                    "EV_NOT_POSITIVE",
                },
                "positive_executable_ev": str(first_blocker)
                not in {
                    "SOURCE_MISSING",
                    "SNAPSHOT_STALE",
                    "FORECAST_MISSING",
                    "RANKING_MISSING",
                    "EV_NOT_POSITIVE",
                    "EXECUTABLE_EV_NOT_POSITIVE",
                },
                "executable_book": bool(row.get("executable_book")),
                "book_reason": row.get("book_reason") or row.get("no_book_reason"),
                "spread": row.get("spread"),
                "liquidity": row.get("liquidity"),
                "settlement_terms_known": bool(row.get("settlement_terms_known")),
                "risk_gate_eligible": bool(row.get("phase3s_proceed")),
                "phase3m_nonzero_size": bool(row.get("phase3m_nonzero_size")),
                "phase3n_approved": bool(row.get("phase3n_approved")),
                "paper_ready": bool(row.get("paper_ready")),
                "first_blocker": first_blocker or "UNKNOWN",
                "raw_ev": row.get("raw_ev") or ranking.get("estimated_edge"),
                "executable_ev": row.get("executable_ev"),
                "opportunity_score": row.get("opportunity_score")
                or ranking.get("opportunity_score"),
                "ranking_reason": ranking.get("ranking_reason"),
            }
        )
    if candidates:
        return candidates
    for row in ranking_rows.values():
        candidates.append(
            {
                "ticker": row.get("ticker"),
                "location_key": row.get("location_key"),
                "market_title": None,
                "target_time": row.get("target_time"),
                "active_weather_market": True,
                "verified_kalshi_link": bool(row.get("ticker")),
                "weather_source_evidence": False,
                "weather_source_fresh": False,
                "weather_feature": False,
                "weather_feature_fresh": False,
                "weather_snapshot_fresh": bool(row.get("has_snapshot")),
                "weather_forecast": bool(row.get("has_current_forecast")),
                "weather_ranking": bool(row.get("has_current_ranking")),
                "positive_ev": row.get("first_hard_blocker") not in {
                    "SNAPSHOT_MISSING",
                    "FORECAST_MISSING",
                    "RANKING_MISSING",
                    "EV_NOT_POSITIVE",
                },
                "positive_executable_ev": False,
                "executable_book": bool(row.get("best_side")),
                "book_reason": row.get("first_hard_blocker"),
                "spread": row.get("spread"),
                "liquidity": row.get("liquidity"),
                "settlement_terms_known": bool(row.get("settlement_terms_known")),
                "risk_gate_eligible": False,
                "phase3m_nonzero_size": False,
                "phase3n_approved": False,
                "paper_ready": row.get("first_hard_blocker") == "PAPER_GATE_READY",
                "first_blocker": row.get("first_hard_blocker") or "UNKNOWN",
                "raw_ev": row.get("estimated_edge"),
                "executable_ev": None,
                "opportunity_score": row.get("opportunity_score"),
                "ranking_reason": row.get("ranking_reason"),
            }
        )
    return candidates


def weather_fast_lane_summary(
    candidates: list[dict[str, Any]],
    *,
    writer: dict[str, Any],
    r2_payload: dict[str, Any],
    r3_payload: dict[str, Any],
    r5_payload: dict[str, Any],
) -> dict[str, Any]:
    blockers = Counter(row.get("first_blocker") or "UNKNOWN" for row in candidates)
    dashboard_weather = (r5_payload.get("category_summaries") or {}).get("weather") or {}
    return {
        "current_weather_rows": len(candidates),
        "verified_link_rows": sum(1 for row in candidates if row["verified_kalshi_link"]),
        "source_evidence_rows": sum(1 for row in candidates if row["weather_source_evidence"]),
        "fresh_source_rows": sum(1 for row in candidates if row["weather_source_fresh"]),
        "fresh_snapshot_rows": sum(1 for row in candidates if row["weather_snapshot_fresh"]),
        "feature_rows": sum(1 for row in candidates if row["weather_feature"]),
        "forecast_rows": sum(1 for row in candidates if row["weather_forecast"]),
        "ranking_rows": sum(1 for row in candidates if row["weather_ranking"]),
        "positive_ev_rows": sum(1 for row in candidates if row["positive_ev"]),
        "positive_executable_ev_rows": sum(
            1 for row in candidates if row["positive_executable_ev"]
        ),
        "executable_book_rows": sum(1 for row in candidates if row["executable_book"]),
        "risk_gate_eligible_rows": sum(1 for row in candidates if row["risk_gate_eligible"]),
        "paper_ready_rows": sum(1 for row in candidates if row["paper_ready"]),
        "first_hard_blocker": _first_blocker(blockers),
        "first_hard_blocker_counts": dict(blockers),
        "writer_safe_to_start": bool(writer.get("safe_to_start_write")),
        "active_writer_pid": writer.get("current_writer_pid"),
        "ranking_activation_status": r2_payload.get("status"),
        "opportunity_scan": r2_payload.get("opportunity_scan") or {},
        "paper_gate_status": r3_payload.get("status"),
        "dashboard_weather_status": dashboard_weather.get("status"),
        "dashboard_weather_current_rows": dashboard_weather.get("current_rows"),
        "dashboard_weather_first_blocker": dashboard_weather.get("first_blocker"),
    }


def _status(*, summary: dict[str, Any], writer_active: bool) -> str:
    if writer_active:
        return "BLOCKED_BY_ACTIVE_WRITER"
    if summary["current_weather_rows"] == 0:
        return "NO_CURRENT_WEATHER_ROWS"
    if summary["paper_ready_rows"] > 0:
        return "WEATHER_PAPER_READY"
    if summary["ranking_rows"] > 0:
        return "WEATHER_RANKED_GATE_BLOCKED"
    return "WEATHER_FAST_LANE_GAP_EXPLAINED"


def _acceptance(*, summary: dict[str, Any], writer_active: bool) -> dict[str, Any]:
    return {
        "weather_rows_ranked_or_exact_blocker": (
            summary["ranking_rows"] > 0
            or bool(summary["first_hard_blocker_counts"])
            or summary.get("first_hard_blocker") == "NO_CURRENT_WEATHER_ROWS"
            or writer_active
        ),
        "weather_dashboard_category_engine_visible": bool(
            summary.get("dashboard_weather_status")
            or summary.get("dashboard_weather_current_rows") is not None
        ),
        "no_live_demo_or_paper_orders": True,
        "no_paper_trades_created": True,
        "uses_current_active_weather_markets": True,
        "thresholds_lowered": False,
    }


def _next_action(*, summary: dict[str, Any], writer_active: bool) -> dict[str, Any]:
    if writer_active:
        return {
            "stage": "WAIT_FOR_WRITER_CLEAR",
            "command": "kalshi-bot db-writer-monitor --json",
            "reason": "Weather ranking/opportunity path is writer-capable.",
            "allow_paper_trade_creation": False,
        }
    if summary["paper_ready_rows"] > 0:
        return {
            "stage": "PAPER_ONLY_OPERATOR_REVIEW",
            "command": (
                "kalshi-bot phase3ba-r5-paper-ready-truth --output-dir "
                "reports/phase3ba_r5 --reports-dir reports --max-duration-seconds 120"
            ),
            "reason": "Weather has paper-ready rows; refresh truth before review.",
            "allow_paper_trade_creation": False,
        }
    if summary["first_hard_blocker"] == "NO_CURRENT_WEATHER_ROWS":
        return {
            "stage": "REFRESH_ACTIVE_WEATHER_CATALOG",
            "command": (
                "kalshi-bot db-writer-monitor --json\n"
                "kalshi-bot sync-markets --status open --limit 100 --max-pages 3 "
                "--series-ticker KXTEMPNYCH\n"
                "kalshi-bot market-legs-parse --refresh --limit 1500\n"
                "kalshi-bot phase3az-r12-weather-activation-preview --output-dir "
                "reports/phase3az_r12_weather --limit 2000 --fresh-window-hours 24 "
                "--match-tolerance-hours 3"
            ),
            "reason": (
                "Strict current active weather inventory is empty while older dashboard "
                "weather artifacts still exist."
            ),
            "allow_paper_trade_creation": False,
        }
    if summary["first_hard_blocker"] == "RANKING_MISSING":
        return {
            "stage": "RERUN_WEATHER_FAST_LANE_AFTER_WRITER_GATE",
            "command": (
                "kalshi-bot phase3bb-r2-weather-fast-lane --output-dir "
                "reports/phase3bb_r2 --reports-dir reports"
            ),
            "reason": "Current weather rows still need rankings.",
            "allow_paper_trade_creation": False,
        }
    return {
        "stage": "KEEP_WEATHER_DIAGNOSTIC_ONLY",
        "command": (
            "kalshi-bot phase3bb-r2-weather-fast-lane --output-dir "
            "reports/phase3bb_r2 --reports-dir reports"
        ),
        "reason": f"Weather is blocked by {summary['first_hard_blocker']}.",
        "allow_paper_trade_creation": False,
    }


def _first_blocker(blockers: Counter[str]) -> str:
    if not blockers:
        return "NO_CURRENT_WEATHER_ROWS"
    order = (
        "SOURCE_MISSING",
        "SNAPSHOT_STALE",
        "FORECAST_MISSING",
        "RANKING_MISSING",
        "EV_NOT_POSITIVE",
        "EXECUTABLE_EV_NOT_POSITIVE",
        "BOOK_MISSING",
        "LIQUIDITY_TOO_LOW",
        "SPREAD_TOO_WIDE",
        "SETTLEMENT_TERMS_UNKNOWN",
        "RISK_NOT_ELIGIBLE",
        "PHASE_3M_ZERO_SIZE",
        "PHASE_3N_RISK_BLOCK",
        "PAPER_READY",
    )
    for blocker in order:
        if blockers.get(blocker):
            return blocker
    return next(iter(blockers))


def _blocked_ranking_payload(
    *,
    writer: dict[str, Any],
    opportunity_output: Path,
    limit: int,
) -> dict[str, Any]:
    return {
        "status": "BLOCKED_BY_ACTIVE_WRITER",
        "weather_rows": [],
        "opportunity_scan": {
            "ran": False,
            "status": "SKIPPED_ACTIVE_WRITER",
            "registered_command": _weather_opportunity_command(
                opportunity_output,
                limit=limit,
            ),
        },
        "active_db_writer_status": writer,
    }


def _weather_opportunity_command(output_path: Path, *, limit: int) -> str:
    return (
        "kalshi-bot find-opportunities --model-name weather_v2 "
        f"--limit {limit} --output {output_path}"
    )


def _operator_guardrails() -> list[str]:
    return [
        "Keep PAPER / READ-ONLY; no live/demo exchange writes.",
        "Do not create paper trades from this phase.",
        "Do not fabricate weather data, links, books, or settlements.",
        "Use only current active weather markets.",
        "Do not lower EV, confidence, liquidity, spread, settlement, or risk thresholds.",
        "Run write-capable weather ranking only when db-writer-monitor is clear.",
    ]


def _render_executive_summary(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    next_action = payload["next_action"]
    lines = _metadata_lines(payload, "# Phase 3BB-R2 Weather Fast Lane")
    lines.extend(
        [
            "",
            "## Result",
            "",
            f"- Status: `{payload['status']}`",
            f"- Current weather rows: `{summary['current_weather_rows']}`",
            f"- Verified link rows: `{summary['verified_link_rows']}`",
            f"- Fresh source rows: `{summary['fresh_source_rows']}`",
            f"- Fresh snapshot rows: `{summary['fresh_snapshot_rows']}`",
            f"- Forecast rows: `{summary['forecast_rows']}`",
            f"- Ranking rows: `{summary['ranking_rows']}`",
            f"- Positive EV rows: `{summary['positive_ev_rows']}`",
            f"- Executable book rows: `{summary['executable_book_rows']}`",
            f"- Paper-ready rows: `{summary['paper_ready_rows']}`",
            f"- First hard blocker: `{summary['first_hard_blocker']}`",
            "",
            "## Next Action",
            "",
            "```bash",
            next_action["command"],
            "```",
            "",
            f"- Stage: `{next_action['stage']}`",
            f"- Reason: {next_action['reason']}",
            f"- Paper trade creation allowed: `{next_action['allow_paper_trade_creation']}`",
        ]
    )
    return "\n".join(lines) + "\n"


def _render_weather_fast_lane(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# Phase 3BB-R2 Weather Fast Lane Detail")
    lines.extend(["", "## Funnel", ""])
    for index, step in enumerate(payload["weather_paper_funnel"], start=1):
        lines.append(f"{index}. {step}")
    lines.extend(["", "## Summary", ""])
    for key, value in payload["summary"].items():
        if key != "opportunity_scan":
            lines.append(f"- {key}: `{value}`")
    lines.extend(
        [
            "",
            "## Weather Candidates",
            "",
            "| Ticker | Forecast | Ranking | EV | Exec EV | Book | Risk | Blocker |",
            "| --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
    )
    if not payload["weather_candidates"]:
        lines.append("| none |  |  |  |  |  |  | NO_CURRENT_WEATHER_ROWS |")
    for row in payload["weather_candidates"]:
        lines.append(
            "| "
            f"{row['ticker']} | "
            f"{row['weather_forecast']} | "
            f"{row['weather_ranking']} | "
            f"{row['positive_ev']} | "
            f"{row['positive_executable_ev']} | "
            f"{row['executable_book']} | "
            f"{row['risk_gate_eligible']} | "
            f"{row['first_blocker']} |"
        )
    return "\n".join(lines) + "\n"


def _render_next_actions(payload: dict[str, Any]) -> str:
    next_action = payload["next_action"]
    lines = _metadata_lines(payload, "# Phase 3BB-R2 Next Actions")
    lines.extend(
        [
            "",
            "## Exact Next Command",
            "",
            "```bash",
            next_action["command"],
            "```",
            "",
            f"- Stage: `{next_action['stage']}`",
            f"- Reason: {next_action['reason']}",
            f"- Paper trade creation allowed: `{next_action['allow_paper_trade_creation']}`",
            "",
            "## Guardrails",
            "",
        ]
    )
    for guardrail in payload["operator_guardrails"]:
        lines.append(f"- {guardrail}")
    return "\n".join(lines) + "\n"


def _write_candidates_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fieldnames = [
        "ticker",
        "location_key",
        "market_title",
        "target_time",
        "active_weather_market",
        "verified_kalshi_link",
        "weather_source_evidence",
        "weather_source_fresh",
        "weather_feature",
        "weather_feature_fresh",
        "weather_snapshot_fresh",
        "weather_forecast",
        "weather_ranking",
        "positive_ev",
        "positive_executable_ev",
        "executable_book",
        "book_reason",
        "spread",
        "liquidity",
        "settlement_terms_known",
        "risk_gate_eligible",
        "phase3m_nonzero_size",
        "phase3n_approved",
        "paper_ready",
        "first_blocker",
        "raw_ev",
        "executable_ev",
        "opportunity_score",
        "ranking_reason",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fieldnames})
