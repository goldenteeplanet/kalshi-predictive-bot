from __future__ import annotations

import csv
import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings
from kalshi_predictor.economic.actuals import TRADING_ECONOMICS_ENV_NAMES
from kalshi_predictor.economic.consensus_watch import (
    write_phase3bd_r5_consensus_feed_watch_report,
)
from kalshi_predictor.economic.opportunity_quality_gate import (
    write_phase3bd_r7_economic_opportunity_quality_gate_report,
)
from kalshi_predictor.utils.time import utc_now

PHASE3BD_R8_VERSION = "phase3bd_r8_economic_evidence_activation"
DEFAULT_TEMPLATE_LIMIT = 200

R5Writer = Callable[..., Any]
R7Writer = Callable[..., Any]


@dataclass(frozen=True)
class Phase3BDR8Artifacts:
    json_path: Path
    markdown_path: Path
    template_csv_path: Path
    template_json_path: Path
    payload: dict[str, Any]


def write_phase3bd_r8_economic_evidence_activation_report(
    *,
    session: Session,
    output_dir: Path = Path("reports/phase3bd_r8"),
    r5_output_dir: Path = Path("reports/phase3bd_r5"),
    r7_output_dir: Path = Path("reports/phase3bd_r7"),
    settings: Settings | None = None,
    input_file: Path | None = None,
    trading_economics_api_key: str | None = None,
    country: str = "united states",
    days_back: int = 90,
    days_ahead: int = 14,
    min_importance: int = 2,
    force_refresh: bool = True,
    max_series: int = 24,
    markets_per_series: int = 50,
    snapshot_series_limit: int = 8,
    forecast_limit: int = 500,
    opportunity_limit: int = 75,
    r7_limit: int = 500,
    freshness_minutes: int = 360,
    min_expected_value: Decimal = Decimal("0"),
    min_edge: Decimal = Decimal("0.01"),
    min_score: Decimal = Decimal("60"),
    min_liquidity_score: Decimal = Decimal("1"),
    max_spread: Decimal = Decimal("0.03"),
    require_actual_consensus: bool = True,
    max_preflight: int = 10,
    risk_preflight: bool = False,
    template_limit: int = DEFAULT_TEMPLATE_LIMIT,
    r5_writer: R5Writer | None = None,
    r7_writer: R7Writer | None = None,
) -> Phase3BDR8Artifacts:
    output_dir.mkdir(parents=True, exist_ok=True)
    generated_at = utc_now()
    source_state = _source_state(
        input_file=input_file,
        trading_economics_api_key=trading_economics_api_key,
    )
    r7 = r7_writer or write_phase3bd_r7_economic_opportunity_quality_gate_report
    r5 = r5_writer or write_phase3bd_r5_consensus_feed_watch_report

    initial_r7 = r7(
        session=session,
        output_dir=r7_output_dir,
        settings=settings,
        limit=r7_limit,
        freshness_minutes=freshness_minutes,
        min_expected_value=min_expected_value,
        min_edge=min_edge,
        min_score=min_score,
        min_liquidity_score=min_liquidity_score,
        max_spread=max_spread,
        require_actual_consensus=require_actual_consensus,
        max_preflight=max_preflight,
        risk_preflight=False,
    )
    r5_payload: dict[str, Any] | None = None
    r5_artifacts: Any | None = None
    if source_state["source_configured"]:
        r5_artifacts = r5(
            session=session,
            output_dir=r5_output_dir,
            input_file=input_file,
            trading_economics_api_key=trading_economics_api_key,
            country=country,
            days_back=days_back,
            days_ahead=days_ahead,
            min_importance=min_importance,
            force_refresh=force_refresh,
            max_series=max_series,
            markets_per_series=markets_per_series,
            snapshot_series_limit=snapshot_series_limit,
            forecast_limit=forecast_limit,
            opportunity_limit=opportunity_limit,
        )
        r5_payload = getattr(r5_artifacts, "payload", None) or {}

    final_r7 = (
        r7(
            session=session,
            output_dir=r7_output_dir,
            settings=settings,
            limit=r7_limit,
            freshness_minutes=freshness_minutes,
            min_expected_value=min_expected_value,
            min_edge=min_edge,
            min_score=min_score,
            min_liquidity_score=min_liquidity_score,
            max_spread=max_spread,
            require_actual_consensus=require_actual_consensus,
            max_preflight=max_preflight,
            risk_preflight=risk_preflight and _r7_preflight_ready(r5_payload, initial_r7),
        )
        if source_state["source_configured"]
        else initial_r7
    )
    final_r7_payload = getattr(final_r7, "payload", None) or {}
    template_rows = _verified_export_template_rows(
        final_r7_payload,
        limit=template_limit,
    )
    template_csv_path = output_dir / "phase3bd_r8_verified_economic_export_template.csv"
    template_json_path = output_dir / "phase3bd_r8_verified_economic_export_template.json"
    _write_template_csv(template_csv_path, template_rows)
    _write_template_json(template_json_path, template_rows)

    payload = {
        "phase": "3BD-R8",
        "phase_version": PHASE3BD_R8_VERSION,
        "generated_at": generated_at.isoformat(),
        "mode": "PAPER_READ_ONLY_ECONOMIC_EVIDENCE_ACTIVATION",
        "paper_only_safety": "preserved",
        "live_demo_execution": "blocked",
        "order_submission_cancel_replace": "blocked",
        "source_state": source_state,
        "summary": _summary(
            source_state=source_state,
            initial_r7_payload=getattr(initial_r7, "payload", None) or {},
            final_r7_payload=final_r7_payload,
            r5_payload=r5_payload,
            template_rows=template_rows,
            risk_preflight=risk_preflight,
        ),
        "r5": _artifact_summary(r5_artifacts),
        "r7_before": _r7_report_summary(getattr(initial_r7, "payload", None) or {}),
        "r7_after": _r7_report_summary(final_r7_payload),
        "verified_export_template": {
            "csv_path": str(template_csv_path),
            "json_path": str(template_json_path),
            "rows": len(template_rows),
            "purpose": (
                "Fill source_url, forecast_value/consensus, actual_value after "
                "release, and previous_value when available. R4/R5 will ignore "
                "rows that are still incomplete."
            ),
        },
        "config": {
            "country": country,
            "days_back": days_back,
            "days_ahead": days_ahead,
            "min_importance": min_importance,
            "force_refresh": force_refresh,
            "max_series": max_series,
            "markets_per_series": markets_per_series,
            "snapshot_series_limit": snapshot_series_limit,
            "forecast_limit": forecast_limit,
            "opportunity_limit": opportunity_limit,
            "r7_limit": r7_limit,
            "freshness_minutes": freshness_minutes,
            "min_expected_value": str(min_expected_value),
            "min_edge": str(min_edge),
            "min_score": str(min_score),
            "min_liquidity_score": str(min_liquidity_score),
            "max_spread": str(max_spread),
            "require_actual_consensus": require_actual_consensus,
            "max_preflight": max_preflight,
            "risk_preflight_requested": risk_preflight,
            "risk_preflight_default": False,
        },
    }
    payload["recommended_next_action"] = _recommended_next_action(payload["summary"])
    json_path = output_dir / "phase3bd_r8_economic_evidence_activation.json"
    markdown_path = output_dir / "phase3bd_r8_economic_evidence_activation.md"
    json_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    markdown_path.write_text(_markdown(payload), encoding="utf-8")
    return Phase3BDR8Artifacts(
        json_path=json_path,
        markdown_path=markdown_path,
        template_csv_path=template_csv_path,
        template_json_path=template_json_path,
        payload=payload,
    )


def _source_state(
    *,
    input_file: Path | None,
    trading_economics_api_key: str | None,
) -> dict[str, Any]:
    configured_env_names = [name for name in TRADING_ECONOMICS_ENV_NAMES if os.getenv(name)]
    api_configured = bool(trading_economics_api_key or configured_env_names)
    file_requested = input_file is not None
    file_exists = bool(input_file and input_file.exists())
    file_configured = file_requested and file_exists
    if api_configured and file_configured:
        source_mode = "TRADING_ECONOMICS_API_AND_VERIFIED_INPUT_FILE"
    elif api_configured:
        source_mode = "TRADING_ECONOMICS_API"
    elif file_configured:
        source_mode = "VERIFIED_INPUT_FILE"
    elif file_requested:
        source_mode = "VERIFIED_INPUT_FILE_MISSING"
    else:
        source_mode = "NONE"
    return {
        "source_configured": api_configured or file_configured,
        "source_mode": source_mode,
        "trading_economics_api_configured": api_configured,
        "verified_input_file_requested": file_requested,
        "verified_input_file_configured": file_configured,
        "verified_input_file_exists": file_exists,
        "verified_input_file": str(input_file) if input_file is not None else None,
        "configured_env_names": configured_env_names,
        "credential_value_reported": False,
    }


def _summary(
    *,
    source_state: dict[str, Any],
    initial_r7_payload: dict[str, Any],
    final_r7_payload: dict[str, Any],
    r5_payload: dict[str, Any] | None,
    template_rows: list[dict[str, Any]],
    risk_preflight: bool,
) -> dict[str, Any]:
    initial_r7_summary = initial_r7_payload.get("summary", {})
    final_r7_summary = final_r7_payload.get("summary", {})
    r5_summary = (r5_payload or {}).get("summary", {})
    summary = {
        "status": _status(
            source_state=source_state,
            r5_summary=r5_summary,
            final_r7_summary=final_r7_summary,
        ),
        "source_configured": bool(source_state["source_configured"]),
        "source_mode": source_state["source_mode"],
        "r5_ran": r5_payload is not None,
        "r5_status": r5_summary.get("status"),
        "consensus_value_observations": r5_summary.get("consensus_value_observations", 0),
        "actual_and_consensus_observations": r5_summary.get(
            "actual_and_consensus_observations",
            0,
        ),
        "features_inserted": r5_summary.get("features_inserted", 0),
        "forecasts_inserted": r5_summary.get("forecasts_inserted", 0),
        "rankings_inserted": r5_summary.get("rankings_inserted", 0),
        "initial_r7_status": initial_r7_summary.get("status"),
        "initial_r7_primary_gap": initial_r7_summary.get("primary_gap"),
        "initial_calendar_only_rows": _evidence_count(initial_r7_payload, "CALENDAR_ONLY"),
        "final_r7_status": final_r7_summary.get("status"),
        "final_r7_primary_gap": final_r7_summary.get("primary_gap"),
        "economic_rankings_scanned": final_r7_summary.get("economic_rankings_scanned", 0),
        "source_evidence_ready_rows": final_r7_summary.get(
            "source_evidence_ready_rows",
            0,
        ),
        "positive_ev_rows": final_r7_summary.get("positive_ev_rows", 0),
        "clean_execution_rows": final_r7_summary.get("clean_execution_rows", 0),
        "risk_ready_rows": final_r7_summary.get("risk_ready_rows", 0),
        "preflight_ready_rows": final_r7_summary.get("preflight_ready_rows", 0),
        "phase3m_phase3n_preflight_recorded": final_r7_summary.get(
            "phase3m_phase3n_preflight_recorded",
            0,
        ),
        "risk_preflight_requested": risk_preflight,
        "template_rows_written": len(template_rows),
        "paper_only_safety": "preserved",
        "live_demo_execution": "blocked",
        "order_submission_cancel_replace": "blocked",
    }
    return summary


def _status(
    *,
    source_state: dict[str, Any],
    r5_summary: dict[str, Any],
    final_r7_summary: dict[str, Any],
) -> str:
    if source_state["source_mode"] == "VERIFIED_INPUT_FILE_MISSING":
        return "BLOCKED_BY_MISSING_VERIFIED_INPUT_FILE"
    if not source_state["source_configured"]:
        return "BLOCKED_BY_MISSING_VERIFIED_CONSENSUS_SOURCE"
    if final_r7_summary.get("preflight_ready_rows", 0) > 0:
        return "R7_PREFLIGHT_READY"
    if final_r7_summary.get("source_evidence_ready_rows", 0) > 0:
        return "ACTUAL_CONSENSUS_LOADED_BUT_R7_BLOCKED"
    if r5_summary.get("actual_and_consensus_observations", 0) > 0:
        return "ACTUAL_CONSENSUS_REFRESHED_R7_NOT_READY"
    if r5_summary.get("consensus_value_observations", 0) > 0:
        return "CONSENSUS_ONLY_WAITING_FOR_ACTUALS"
    return "SOURCE_CONFIGURED_NO_USABLE_ACTUAL_CONSENSUS"


def _verified_export_template_rows(
    r7_payload: dict[str, Any],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in r7_payload.get("rows", [])[: max(limit, 0)]:
        evidence = row.get("economic_evidence") or {}
        if evidence.get("actual_and_consensus"):
            continue
        event_key = evidence.get("event_key") or ""
        rows.append(
            {
                "event_key": event_key,
                "event_time": evidence.get("event_time") or "",
                "category": event_key or "",
                "title": evidence.get("event_title") or row.get("title") or "",
                "source_url": evidence.get("source_url") or "",
                "actual_value": evidence.get("actual_value") or "",
                "forecast_value": evidence.get("forecast_value") or "",
                "previous_value": evidence.get("previous_value") or "",
                "source": "verified_manual_export",
                "provider": "",
                "verification_status": "NEEDS_VERIFIED_SOURCE",
                "ticker": row.get("ticker") or "",
                "event_ticker": row.get("event_ticker") or "",
                "series_ticker": row.get("series_ticker") or "",
                "market_title": row.get("title") or "",
                "market_status": row.get("market_status") or "",
                "ranked_at": row.get("ranked_at") or "",
                "current_evidence_state": row.get("economic_evidence_state") or "",
                "blockers": ";".join(row.get("blockers") or []),
                "review_note": (
                    "Fill verified source_url and forecast_value/consensus. "
                    "Fill actual_value after release; keep blank if unreleased."
                ),
            }
        )
    return rows


def _write_template_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    headers = _template_headers()
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _write_template_json(path: Path, rows: list[dict[str, Any]]) -> None:
    payload = {
        "instructions": (
            "Fill source_url and forecast_value/consensus from a verified source. "
            "Fill actual_value after the release and previous_value when available. "
            "Then pass this file to phase3bd-r8-economic-evidence-activation "
            "with --input-file."
        ),
        "events": rows,
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _template_headers() -> list[str]:
    return [
        "event_key",
        "event_time",
        "category",
        "title",
        "source_url",
        "actual_value",
        "forecast_value",
        "previous_value",
        "source",
        "provider",
        "verification_status",
        "ticker",
        "event_ticker",
        "series_ticker",
        "market_title",
        "market_status",
        "ranked_at",
        "current_evidence_state",
        "blockers",
        "review_note",
    ]


def _artifact_summary(artifacts: Any | None) -> dict[str, Any] | None:
    if artifacts is None:
        return None
    payload = getattr(artifacts, "payload", {}) or {}
    return {
        "json_path": str(getattr(artifacts, "json_path", "")),
        "markdown_path": str(getattr(artifacts, "markdown_path", "")),
        "summary": payload.get("summary", {}),
        "recommended_next_action": payload.get("recommended_next_action"),
    }


def _r7_report_summary(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "generated_at": payload.get("generated_at"),
        "summary": payload.get("summary", {}),
        "blocker_counts": payload.get("blocker_counts", {}),
        "evidence_state_counts": payload.get("evidence_state_counts", {}),
        "recommended_next_action": payload.get("recommended_next_action"),
    }


def _evidence_count(payload: dict[str, Any], state: str) -> int:
    counts = payload.get("evidence_state_counts") or {}
    return int(counts.get(state) or 0)


def _r7_preflight_ready(
    r5_payload: dict[str, Any] | None,
    initial_r7: Any,
) -> bool:
    del r5_payload
    summary = (getattr(initial_r7, "payload", {}) or {}).get("summary", {})
    return bool(summary.get("preflight_ready_rows", 0) > 0)


def _recommended_next_action(summary: dict[str, Any]) -> str:
    status = summary["status"]
    if status == "BLOCKED_BY_MISSING_VERIFIED_INPUT_FILE":
        return "Fix the --input-file path, then rerun Phase 3BD-R8."
    if status == "BLOCKED_BY_MISSING_VERIFIED_CONSENSUS_SOURCE":
        return (
            "Configure TRADING_ECONOMICS_API_KEY or fill the R8 verified export "
            "template, then rerun Phase 3BD-R8 with --input-file."
        )
    if summary["preflight_ready_rows"] > 0:
        return (
            "R7 found clean economic rows. Review the R7 report, then rerun R7 "
            "with --risk-preflight for paper-only Phase 3M/3N evidence."
        )
    if summary["source_evidence_ready_rows"] > 0:
        return (
            "Actual-plus-consensus evidence is present, but R7 still blocks trading; "
            "inspect EV, execution, freshness, and risk blockers."
        )
    if summary["consensus_value_observations"] > 0:
        return "Consensus rows are loaded; keep watching release windows for actual values."
    return (
        "Source was configured but no usable actual-plus-consensus rows were loaded; "
        "verify source mapping, date range, and event keys."
    )


def _markdown(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    source = payload["source_state"]
    template = payload["verified_export_template"]
    lines = [
        "# Phase 3BD-R8 Economic Evidence Activation",
        "",
        f"Generated: {payload['generated_at']}",
        "",
        "Mode: PAPER / READ ONLY. Live/demo execution and order writes remain blocked.",
        "",
        "## Summary",
        "",
        f"- Status: {summary['status']}",
        f"- Source mode: {summary['source_mode']}",
        f"- R5 ran: {summary['r5_ran']}",
        f"- R5 status: {summary['r5_status'] or 'n/a'}",
        f"- Consensus observations: {summary['consensus_value_observations']}",
        f"- Actual + consensus observations: {summary['actual_and_consensus_observations']}",
        f"- R7 final status: {summary['final_r7_status']}",
        f"- R7 primary gap: {summary['final_r7_primary_gap']}",
        f"- Economic rankings scanned: {summary['economic_rankings_scanned']}",
        f"- Source evidence ready rows: {summary['source_evidence_ready_rows']}",
        f"- Positive EV rows: {summary['positive_ev_rows']}",
        f"- Clean execution rows: {summary['clean_execution_rows']}",
        f"- Risk ready rows: {summary['risk_ready_rows']}",
        f"- Preflight-ready rows: {summary['preflight_ready_rows']}",
        f"- Template rows written: {summary['template_rows_written']}",
        "",
        "## Source State",
        "",
        f"- Trading Economics API configured: {source['trading_economics_api_configured']}",
        f"- Verified input file: {source['verified_input_file'] or 'n/a'}",
        f"- Verified input file exists: {source['verified_input_file_exists']}",
        f"- Credential value reported: {source['credential_value_reported']}",
        "",
        "## Verified Export Template",
        "",
        f"- CSV: {template['csv_path']}",
        f"- JSON: {template['json_path']}",
        "",
        "## Recommended Next Action",
        "",
        payload["recommended_next_action"],
    ]
    return "\n".join(lines) + "\n"
