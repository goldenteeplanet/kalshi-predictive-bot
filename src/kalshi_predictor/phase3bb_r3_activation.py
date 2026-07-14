from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kalshi_predictor.paper.settlement_reconciliation import PAPER_ONLY_SAFETY
from kalshi_predictor.utils.time import utc_now

PHASE3BB_R3_SOURCE_ACTIVATION_VERSION = "phase3bb_r3_source_evidence_activation_v1"
DEFAULT_OUTPUT_DIR = Path("reports/phase3bb_r3_source_activation")
DEFAULT_REPORTS_DIR = Path("reports")

USDA_ADAPTER = "commodity_advertised_price_source"
FLIGHTAWARE_ADAPTER = "transportation_flight_cancellation_source"
CUSHMAN_ADAPTER = "infrastructure_data_center_capacity_source"

SOURCE_LABELS = {
    USDA_ADAPTER: "USDA",
    FLIGHTAWARE_ADAPTER: "FlightAware",
    CUSHMAN_ADAPTER: "Cushman",
}

SOURCE_FAMILIES = {
    USDA_ADAPTER: "commodity",
    FLIGHTAWARE_ADAPTER: "transportation",
    CUSHMAN_ADAPTER: "infrastructure",
}

NEXT_COMMAND_CANDIDATES = (
    "kalshi-bot phase3bb-r3-source-evidence-activation "
    "--output-dir reports/phase3bb_r3_source_activation --reports-dir reports",
    "kalshi-bot phase3bb-r2-general-source-evidence "
    "--output-dir reports/phase3bb_r2_sources",
    "kalshi-bot phase3bb-r2-general-source-availability "
    "--output-dir reports/phase3bb_r2_sources",
    "kalshi-bot phase3ax-gap-analysis --output-dir reports/phase3ax --reports-dir reports",
)


@dataclass(frozen=True)
class Phase3BBR3SourceActivationArtifacts:
    output_dir: Path
    executive_summary_path: Path
    next_actions_path: Path
    next_codex_task_path: Path
    activation_json_path: Path
    activation_markdown_path: Path
    activation_decisions_path: Path
    command_audit_path: Path
    manifest_path: Path


def build_phase3bb_r3_source_evidence_activation(
    *,
    reports_dir: Path = DEFAULT_REPORTS_DIR,
    registered_commands: set[str] | None = None,
) -> dict[str, Any]:
    """Build a report-only activation audit for general-source evidence gates."""

    generated_at = utc_now()
    evidence_report = _read_json(
        reports_dir / "phase3bb_r2_sources" / "phase3bb_r2_general_source_evidence.json"
    )
    availability_report = _read_json(
        reports_dir
        / "phase3bb_r2_sources"
        / "phase3bb_r2_general_source_availability.json"
    )
    readiness_report = _read_json(
        reports_dir / "phase3bb_r2_sources" / "source_readiness_matrix.json"
    )
    usda_date_report = _read_json(
        reports_dir / "phase3bb_r2_sources" / "usda_fvwretail_date_resolution.json"
    )
    flightaware_date_report = _read_json(
        reports_dir
        / "phase3bb_r2_sources"
        / "flightaware_cancellation_date_resolution.json"
    )
    phase3an_status = _read_json(reports_dir / "phase3an" / "general_sources_status.json")
    phase3ax_status = _read_json(reports_dir / "phase3ax" / "source_evidence_gap_status.json")

    evidence_rows = _list_value(evidence_report.get("evidence_rows"))
    availability_rows = _list_value(availability_report.get("availability_rows"))
    readiness_rows = _list_value(readiness_report.get("data"))

    decisions = [
        _source_activation_decision(
            adapter_key=adapter_key,
            evidence_rows=[
                row for row in evidence_rows if row.get("source_adapter_key") == adapter_key
            ],
            availability_row=_first_row(availability_rows, "source_adapter_key", adapter_key),
            readiness_row=_readiness_row(readiness_rows, adapter_key),
            usda_date_report=usda_date_report,
            flightaware_date_report=flightaware_date_report,
        )
        for adapter_key in (USDA_ADAPTER, FLIGHTAWARE_ADAPTER, CUSHMAN_ADAPTER)
    ]
    command_audit = _command_registry_audit(set(registered_commands or ()))
    next_actions = _registered_next_commands(command_audit)
    summary = _activation_summary(
        decisions=decisions,
        evidence_report=evidence_report,
        availability_report=availability_report,
        phase3an_status=phase3an_status,
        phase3ax_status=phase3ax_status,
    )
    next_codex_task = _next_codex_task(summary=summary, decisions=decisions)

    return {
        "generated_at": generated_at.isoformat(),
        "phase": "3BB-R3",
        "phase_version": PHASE3BB_R3_SOURCE_ACTIVATION_VERSION,
        "mode": "PAPER_ONLY_SOURCE_EVIDENCE_ACTIVATION_AUDIT",
        "reports_dir": str(reports_dir),
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "live_or_demo_execution": False,
        "order_submission": False,
        "paper_trade_creation": False,
        "thresholds_lowered": False,
        "fabricated_evidence": False,
        "source_reports_used": {
            "phase3bb_r2_general_source_evidence": str(
                reports_dir
                / "phase3bb_r2_sources"
                / "phase3bb_r2_general_source_evidence.json"
            ),
            "phase3bb_r2_general_source_availability": str(
                reports_dir
                / "phase3bb_r2_sources"
                / "phase3bb_r2_general_source_availability.json"
            ),
            "source_readiness_matrix": str(
                reports_dir / "phase3bb_r2_sources" / "source_readiness_matrix.json"
            ),
            "usda_fvwretail_date_resolution": str(
                reports_dir
                / "phase3bb_r2_sources"
                / "usda_fvwretail_date_resolution.json"
            ),
            "phase3an_general_sources_status": str(
                reports_dir / "phase3an" / "general_sources_status.json"
            ),
            "phase3ax_source_evidence_gap_status": str(
                reports_dir / "phase3ax" / "source_evidence_gap_status.json"
            ),
        },
        "summary": summary,
        "source_activation_decisions": decisions,
        "command_registry_audit": command_audit,
        "next_actions": next_actions,
        "next_codex_task": next_codex_task,
        "operator_do_not_run": [
            "Do not submit, cancel, replace, or amend live/demo exchange orders.",
            "Do not create paper trades from source diagnostics.",
            "Do not promote USDA rows while the exact July 3 source remains unresolved.",
            "Do not promote Cushman rows while values or licensing review are unresolved.",
            "Do not promote FlightAware rows until link-safe and forecast-safe review gates pass.",
            "Do not recommend commands that are not registered in kalshi-bot --help.",
        ],
    }


def write_phase3bb_r3_source_evidence_activation_report(
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    reports_dir: Path = DEFAULT_REPORTS_DIR,
    registered_commands: set[str] | None = None,
) -> Phase3BBR3SourceActivationArtifacts:
    payload = build_phase3bb_r3_source_evidence_activation(
        reports_dir=reports_dir,
        registered_commands=registered_commands,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    executive_summary_path = output_dir / "EXECUTIVE_SUMMARY.md"
    next_actions_path = output_dir / "NEXT_ACTIONS.md"
    next_codex_task_path = output_dir / "NEXT_CODEX_TASK.md"
    activation_json_path = output_dir / "source_evidence_activation.json"
    activation_markdown_path = output_dir / "source_evidence_activation.md"
    activation_decisions_path = output_dir / "source_activation_decisions.json"
    command_audit_path = output_dir / "source_command_audit.json"
    manifest_path = output_dir / "MANIFEST.sha256"

    _write_json(activation_json_path, payload)
    _write_json(activation_decisions_path, payload["source_activation_decisions"])
    _write_json(command_audit_path, payload["command_registry_audit"])
    executive_summary_path.write_text(_render_executive_summary(payload), encoding="utf-8")
    next_actions_path.write_text(_render_next_actions(payload), encoding="utf-8")
    next_codex_task_path.write_text(_render_next_codex_task(payload), encoding="utf-8")
    activation_markdown_path.write_text(_render_activation_markdown(payload), encoding="utf-8")
    _write_manifest(
        manifest_path,
        [
            executive_summary_path,
            next_actions_path,
            next_codex_task_path,
            activation_json_path,
            activation_markdown_path,
            activation_decisions_path,
            command_audit_path,
        ],
    )
    return Phase3BBR3SourceActivationArtifacts(
        output_dir=output_dir,
        executive_summary_path=executive_summary_path,
        next_actions_path=next_actions_path,
        next_codex_task_path=next_codex_task_path,
        activation_json_path=activation_json_path,
        activation_markdown_path=activation_markdown_path,
        activation_decisions_path=activation_decisions_path,
        command_audit_path=command_audit_path,
        manifest_path=manifest_path,
    )


def _source_activation_decision(
    *,
    adapter_key: str,
    evidence_rows: list[dict[str, Any]],
    availability_row: dict[str, Any],
    readiness_row: dict[str, Any],
    usda_date_report: dict[str, Any],
    flightaware_date_report: dict[str, Any],
) -> dict[str, Any]:
    evidence_ready = sum(
        1
        for row in evidence_rows
        if row.get("evidence_status") == "EXACT_EVIDENCE_READY_FOR_REVIEW"
    )
    link_safe = sum(1 for row in evidence_rows if row.get("safe_to_link") is True)
    forecast_safe = sum(1 for row in evidence_rows if row.get("safe_to_forecast") is True)
    missing_or_unavailable = sum(
        1
        for row in evidence_rows
        if row.get("evidence_status")
        in {
            "SOURCE_EVIDENCE_UNAVAILABLE",
            "MISSING_SOURCE_EVIDENCE_FILE",
            "SOURCE_EVIDENCE_FILE_INVALID",
        }
    )
    blocker_codes = _source_blocker_codes(
        adapter_key=adapter_key,
        evidence_ready=evidence_ready,
        missing_or_unavailable=missing_or_unavailable,
        link_safe=link_safe,
        forecast_safe=forecast_safe,
        readiness_row=readiness_row,
        usda_date_report=usda_date_report,
    )
    affected_rows = max(
        len(evidence_rows),
        _int_value(availability_row.get("affected_diagnostic_rows")),
    )
    source_value_available = bool(evidence_ready) or (
        availability_row.get("availability_status") == "SOURCE_VALUE_AVAILABLE_FOR_REVIEW"
    )
    activation_allowed = (
        affected_rows > 0
        and link_safe == affected_rows
        and forecast_safe == affected_rows
    )
    activation_allowed = activation_allowed and not blocker_codes
    return {
        "source_name": SOURCE_LABELS.get(adapter_key, adapter_key),
        "source_adapter_key": adapter_key,
        "source_family": SOURCE_FAMILIES.get(adapter_key, "general"),
        "affected_rows": affected_rows,
        "affected_tickers": _affected_tickers(evidence_rows, availability_row),
        "evidence_ready_rows": evidence_ready,
        "missing_or_unavailable_rows": missing_or_unavailable,
        "source_value_available_for_review": source_value_available,
        "readiness_state": readiness_row.get("readiness_state") or "NOT_REPORTED",
        "availability_status": availability_row.get("availability_status") or "NOT_REPORTED",
        "link_safe_rows": link_safe,
        "forecast_safe_rows": forecast_safe,
        "activation_status": "ACTIVATION_READY" if activation_allowed else "GATED",
        "link_safe_decision": "ALLOW" if activation_allowed else "BLOCK",
        "forecast_safe_decision": "ALLOW" if activation_allowed else "BLOCK",
        "activation_allowed": activation_allowed,
        "promoted_to_link_safe_rows": 0,
        "promoted_to_forecast_safe_rows": 0,
        "proposed_db_writes": 0,
        "feature_writes": False,
        "forecast_writes": False,
        "link_writes": False,
        "paper_trade_writes": False,
        "live_or_demo_execution": False,
        "blocker_codes": blocker_codes,
        "first_blocker": blocker_codes[0] if blocker_codes else "NONE",
        "block_reason": _block_reason(
            adapter_key=adapter_key,
            blocker_codes=blocker_codes,
            availability_row=availability_row,
            readiness_row=readiness_row,
            usda_date_report=usda_date_report,
            flightaware_date_report=flightaware_date_report,
        ),
        "next_action": _source_next_action(adapter_key, blocker_codes),
        "evidence_reference": {
            "source_file": availability_row.get("source_file")
            or _first_text(evidence_rows, "source_file"),
            "source_url": availability_row.get("source_url")
            or _first_nested_text(evidence_rows, "matched_evidence", "source_url"),
            "source_name": availability_row.get("source_name")
            or _first_nested_text(evidence_rows, "matched_evidence", "source_name"),
            "observed_value": availability_row.get("observed_value")
            or _first_observed_value(adapter_key, evidence_rows),
            "target_observation": availability_row.get("target_observation"),
            "target_publication": availability_row.get("target_publication"),
        },
    }


def _source_blocker_codes(
    *,
    adapter_key: str,
    evidence_ready: int,
    missing_or_unavailable: int,
    link_safe: int,
    forecast_safe: int,
    readiness_row: dict[str, Any],
    usda_date_report: dict[str, Any],
) -> list[str]:
    blockers: list[str] = []
    if adapter_key == USDA_ADAPTER:
        if usda_date_report and usda_date_report.get("exact_july_3_report_found") is not True:
            blockers.append("SOURCE_DATE_MISMATCH_BLOCKER")
        if missing_or_unavailable:
            blockers.append("SOURCE_VALUE_UNAVAILABLE")
    elif adapter_key == CUSHMAN_ADAPTER:
        if readiness_row.get("readiness_state") == "PROPRIETARY_SOURCE_REVIEW_REQUIRED":
            blockers.append("PROPRIETARY_REVIEW_REQUIRED")
        if missing_or_unavailable:
            blockers.append("SOURCE_VALUE_UNAVAILABLE")
    elif adapter_key == FLIGHTAWARE_ADAPTER:
        if evidence_ready:
            blockers.append("READY_FOR_REVIEW_NOT_LINK_SAFE")
        else:
            blockers.append("SOURCE_VALUE_UNAVAILABLE")
    if link_safe == 0:
        blockers.append("LINK_SAFE_FALSE")
    if forecast_safe == 0:
        blockers.append("FORECAST_SAFE_FALSE")
    return list(dict.fromkeys(blockers))


def _activation_summary(
    *,
    decisions: list[dict[str, Any]],
    evidence_report: dict[str, Any],
    availability_report: dict[str, Any],
    phase3an_status: dict[str, Any],
    phase3ax_status: dict[str, Any],
) -> dict[str, Any]:
    evidence_summary = (
        evidence_report.get("summary")
        if isinstance(evidence_report.get("summary"), dict)
        else {}
    )
    availability_summary = (
        availability_report.get("summary")
        if isinstance(availability_report.get("summary"), dict)
        else {}
    )
    evidence_ready = _first_int(
        evidence_summary,
        phase3an_status,
        phase3ax_status,
        keys=("exact_evidence_ready_rows", "source_evidence_ready_rows", "evidence_ready_rows"),
    )
    link_safe = _first_int(
        evidence_summary,
        availability_summary,
        phase3an_status,
        phase3ax_status,
        keys=("safe_to_link_rows", "link_safe_rows"),
    )
    forecast_safe = _first_int(
        evidence_summary,
        availability_summary,
        phase3an_status,
        phase3ax_status,
        keys=("safe_to_forecast_rows", "forecast_safe_rows"),
    )
    activation_candidates = sum(1 for row in decisions if row["activation_allowed"])
    all_blockers = [blocker for row in decisions for blocker in row["blocker_codes"]]
    first_hard_blocker = _first_hard_blocker(all_blockers)
    return {
        "activation_readiness": "READY" if activation_candidates else "NOT_READY",
        "activation_outcome": (
            "SOURCE_ACTIVATION_READY"
            if activation_candidates
            else "NO_ACTIVATION_UNSAFE_OR_UNAPPROVED"
        ),
        "current_status": "GATED" if not activation_candidates else "READY",
        "evidence_ready_rows": evidence_ready,
        "expired_or_diagnostic_only_rows": 0,
        "link_safe_rows": link_safe,
        "forecast_safe_rows": forecast_safe,
        "activation_candidate_sources": activation_candidates,
        "activation_candidate_rows": sum(
            row["affected_rows"] for row in decisions if row["activation_allowed"]
        ),
        "promoted_to_link_safe_rows": 0,
        "promoted_to_forecast_safe_rows": 0,
        "source_date_mismatch_blockers": any(
            "SOURCE_DATE_MISMATCH_BLOCKER" in row["blocker_codes"] for row in decisions
        ),
        "proprietary_review_blockers": any(
            "PROPRIETARY_REVIEW_REQUIRED" in row["blocker_codes"] for row in decisions
        ),
        "review_required_blockers": any(
            "READY_FOR_REVIEW_NOT_LINK_SAFE" in row["blocker_codes"] for row in decisions
        ),
        "first_hard_blocker": first_hard_blocker,
        "source_decision_counts": _decision_counts(decisions),
        "safe_to_create_links": activation_candidates > 0,
        "safe_to_create_forecasts": activation_candidates > 0,
        "safe_to_create_paper_trades": False,
        "proposed_db_writes": 0,
        "paper_trade_writes": False,
        "live_or_demo_execution": False,
        "thresholds_lowered": False,
        "fabricated_evidence": False,
    }


def _command_registry_audit(registered_commands: set[str]) -> dict[str, Any]:
    rows = []
    for command in NEXT_COMMAND_CANDIDATES:
        command_name = _command_name(command)
        registered = command_name in registered_commands
        rows.append(
            {
                "command": command_name,
                "full_command": command,
                "registered": registered,
                "included_in_next_actions": registered,
                "recommended_fix": "No action." if registered else "Register command or remove it.",
            }
        )
    missing = [row for row in rows if not row["registered"]]
    return {
        "registered_command_count": len(registered_commands),
        "candidate_commands": rows,
        "missing_commands": missing,
        "missing_command_names": [row["command"] for row in missing],
        "next_actions_reference_only_registered_commands": True,
    }


def _registered_next_commands(command_audit: dict[str, Any]) -> list[str]:
    return [
        str(row["full_command"])
        for row in command_audit["candidate_commands"]
        if row.get("registered") is True
    ]


def _next_codex_task(
    *,
    summary: dict[str, Any],
    decisions: list[dict[str, Any]],
) -> dict[str, Any]:
    if summary["activation_readiness"] == "READY":
        phase = "Phase 3BB-R4 Reviewed Source Promotion Dry Run"
        reason = "At least one general-source adapter is link-safe and forecast-safe."
        problem = "Run a report-only dry run before any reviewed source promotion."
    elif any(row["first_blocker"] == "READY_FOR_REVIEW_NOT_LINK_SAFE" for row in decisions):
        phase = "Phase 3BB-R4 FlightAware Review-to-Link Gate"
        reason = "FlightAware evidence is exact and review-ready but remains link/forecast unsafe."
        problem = (
            "Define report-only entity, time-window, freshness, no-leakage, and review "
            "approval checks before any link-safe or forecast-safe promotion."
        )
    else:
        phase = "Phase 3AH-R3 Sports Provenance Repair"
        reason = "General-source gates are reported but blocked by external or review evidence."
        problem = (
            "Move to the next implementation gap while source evidence waits for exact "
            "inputs."
        )
    return {
        "task_phase_name": phase,
        "reason": reason,
        "problem_statement": problem,
        "acceptance_criteria": [
            "Keep everything PAPER / READ-ONLY.",
            "Do not submit/cancel/replace/amend live or demo exchange orders.",
            "Do not create paper trades from source diagnostics.",
            "Do not promote any row unless exact source, link-safe, and forecast-safe gates pass.",
            "NEXT_ACTIONS must reference only registered kalshi-bot commands.",
        ],
        "full_codex_prompt": _render_codex_prompt(phase, reason, problem),
        "estimated_risk_level": "MEDIUM",
    }


def _render_executive_summary(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    return "\n".join(
        [
            "# Phase 3BB-R3 Executive Summary",
            "",
            f"- Generated at: `{payload['generated_at']}`",
            f"- Activation readiness: `{summary['activation_readiness']}`",
            f"- Activation outcome: `{summary['activation_outcome']}`",
            f"- Evidence-ready rows: `{summary['evidence_ready_rows']}`",
            f"- Link-safe rows: `{summary['link_safe_rows']}`",
            f"- Forecast-safe rows: `{summary['forecast_safe_rows']}`",
            f"- First hard blocker: `{summary['first_hard_blocker']}`",
            f"- Promoted rows: `{summary['promoted_to_link_safe_rows']}` link-safe, "
            f"`{summary['promoted_to_forecast_safe_rows']}` forecast-safe",
            f"- Next Codex task: `{payload['next_codex_task']['task_phase_name']}`",
            "",
            "No live/demo exchange writes, paper trades, threshold changes, or fabricated evidence "
            "were produced by this report.",
            "",
        ]
    )


def _render_next_actions(payload: dict[str, Any]) -> str:
    lines = [
        "# Phase 3BB-R3 Next Actions",
        "",
        f"- Activation readiness: `{payload['summary']['activation_readiness']}`",
        f"- First hard blocker: `{payload['summary']['first_hard_blocker']}`",
        f"- Next Codex task: `{payload['next_codex_task']['task_phase_name']}`",
        "",
        "## Registered Commands",
        "",
    ]
    if payload["next_actions"]:
        lines.extend(f"- `{command}`" for command in payload["next_actions"])
    else:
        lines.append("- No registered command recommendations are available.")
    lines.extend(
        [
            "",
            "## Non-Command Operator Work",
            "",
            "- Keep USDA rows blocked until exact July 3 USDA evidence is available.",
            "- Keep Cushman rows blocked until the exact value and licensing review are available.",
            "- Keep FlightAware rows blocked until review gates mark them link-safe and "
            "forecast-safe.",
            "",
            "Missing command details are recorded in `source_command_audit.json`.",
            "",
        ]
    )
    return "\n".join(lines)


def _render_next_codex_task(payload: dict[str, Any]) -> str:
    task = payload["next_codex_task"]
    lines = [
        "# Phase 3BB-R3 Next Codex Task",
        "",
        f"Task phase name: `{task['task_phase_name']}`",
        "",
        f"Reason: {task['reason']}",
        "",
        f"Problem statement: {task['problem_statement']}",
        "",
        "Acceptance criteria:",
    ]
    lines.extend(f"- {item}" for item in task["acceptance_criteria"])
    lines.extend(["", "Full Codex prompt:", "", "```text", task["full_codex_prompt"], "```", ""])
    return "\n".join(lines)


def _render_activation_markdown(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    lines = [
        "# Phase 3BB-R3 Source Evidence Activation",
        "",
        f"- Generated at: `{payload['generated_at']}`",
        f"- Mode: `{payload['mode']}`",
        f"- Paper-only safety: `{payload['paper_only_safety']}`",
        f"- Activation readiness: `{summary['activation_readiness']}`",
        f"- First hard blocker: `{summary['first_hard_blocker']}`",
        "",
        "## Summary",
        "",
    ]
    for key, value in summary.items():
        lines.append(f"- {key}: `{value}`")
    lines.extend(
        [
            "",
            "## Source Decisions",
            "",
            (
                "| Source | Status | Rows | Evidence-ready | Link-safe | Forecast-safe | "
                "First blocker | Decision |"
            ),
            "| --- | --- | ---: | ---: | ---: | ---: | --- | --- |",
        ]
    )
    for row in payload["source_activation_decisions"]:
        lines.append(
            "| {source} | {status} | {rows} | {evidence} | {link} | {forecast} | "
            "{blocker} | {decision} |".format(
                source=_md(row["source_name"]),
                status=_md(row["activation_status"]),
                rows=row["affected_rows"],
                evidence=row["evidence_ready_rows"],
                link=row["link_safe_rows"],
                forecast=row["forecast_safe_rows"],
                blocker=_md(row["first_blocker"]),
                decision=_md(row["block_reason"]),
            )
        )
    lines.extend(["", "## Next Commands", ""])
    lines.extend(f"- `{command}`" for command in payload["next_actions"])
    lines.append("")
    return "\n".join(lines)


def _render_codex_prompt(phase: str, reason: str, problem: str) -> str:
    return "\n".join(
        [
            phase,
            "",
            "You are Codex working inside the kalshi-predictive-bot repository.",
            "",
            f"Reason: {reason}",
            "",
            f"Problem: {problem}",
            "",
            "Requirements:",
            "1. Keep everything PAPER / READ-ONLY.",
            "2. Do not submit, cancel, replace, or amend live/demo exchange orders.",
            "3. Do not create paper trades from source diagnostics.",
            "4. Do not lower thresholds or fabricate evidence.",
            "5. Use exact source evidence only; no sibling/fuzzy source matching.",
            "6. NEXT_ACTIONS and operator docs must reference only registered kalshi-bot commands.",
            "",
            "Acceptance:",
            "- The selected source or provenance gap is fixed or reported with exact evidence.",
            "- Unsafe source rows remain diagnostic-only.",
            "- The safety guard remains intact.",
        ]
    )


def _block_reason(
    *,
    adapter_key: str,
    blocker_codes: list[str],
    availability_row: dict[str, Any],
    readiness_row: dict[str, Any],
    usda_date_report: dict[str, Any],
    flightaware_date_report: dict[str, Any],
) -> str:
    if "SOURCE_DATE_MISMATCH_BLOCKER" in blocker_codes:
        return str(
            usda_date_report.get("next_action")
            or "Exact USDA July 3 source evidence is not proven."
        )
    if "PROPRIETARY_REVIEW_REQUIRED" in blocker_codes:
        return str(
            readiness_row.get("current_blocker")
            or availability_row.get("block_reason")
            or "Proprietary source review is required."
        )
    if "READY_FOR_REVIEW_NOT_LINK_SAFE" in blocker_codes:
        return str(
            flightaware_date_report.get("next_action")
            or availability_row.get("block_reason")
            or "Exact evidence is present for review, but link/forecast gates remain blocked."
        )
    if blocker_codes:
        return str(availability_row.get("block_reason") or "; ".join(blocker_codes))
    return f"{SOURCE_LABELS.get(adapter_key, adapter_key)} is ready for report-only dry run."


def _source_next_action(adapter_key: str, blocker_codes: list[str]) -> str:
    if adapter_key == USDA_ADAPTER:
        return "Obtain exact official July 3 USDA FVWRETAIL evidence before promotion."
    if adapter_key == CUSHMAN_ADAPTER:
        return "Resolve exact 2026 capacity value and licensing review before promotion."
    if adapter_key == FLIGHTAWARE_ADAPTER:
        return "Build reviewed FlightAware link-safe and forecast-safe gates before promotion."
    if blocker_codes:
        return "Keep source diagnostic-only until all blockers clear."
    return "Run report-only dry run before any reviewed promotion."


def _first_hard_blocker(blockers: list[str]) -> str:
    priority = (
        "SOURCE_DATE_MISMATCH_BLOCKER",
        "PROPRIETARY_REVIEW_REQUIRED",
        "SOURCE_VALUE_UNAVAILABLE",
        "READY_FOR_REVIEW_NOT_LINK_SAFE",
        "LINK_SAFE_FALSE",
        "FORECAST_SAFE_FALSE",
    )
    for blocker in priority:
        if blocker in blockers:
            return blocker
    return blockers[0] if blockers else "NONE"


def _decision_counts(decisions: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in decisions:
        key = str(row.get("activation_status") or "UNKNOWN")
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def _affected_tickers(
    evidence_rows: list[dict[str, Any]],
    availability_row: dict[str, Any],
) -> list[str]:
    tickers = [str(value) for value in _list_value(availability_row.get("affected_tickers"))]
    if not tickers:
        tickers = [str(row.get("ticker")) for row in evidence_rows if row.get("ticker")]
    return sorted(set(tickers))


def _first_observed_value(adapter_key: str, evidence_rows: list[dict[str, Any]]) -> Any:
    fields = {
        USDA_ADAPTER: "price_usd_each",
        FLIGHTAWARE_ADAPTER: "cancellation_count",
        CUSHMAN_ADAPTER: "capacity_gw",
    }
    field = fields.get(adapter_key)
    if not field:
        return None
    for row in evidence_rows:
        evidence = row.get("matched_evidence")
        if isinstance(evidence, dict) and evidence.get(field) not in (None, ""):
            return evidence.get(field)
    return None


def _readiness_row(rows: list[dict[str, Any]], adapter_key: str) -> dict[str, Any]:
    source_name = SOURCE_LABELS.get(adapter_key, "").upper()
    for row in rows:
        if str(row.get("source_name") or "").upper() == source_name:
            return row
    return {}


def _first_row(rows: list[dict[str, Any]], key: str, value: str) -> dict[str, Any]:
    for row in rows:
        if row.get(key) == value:
            return row
    return {}


def _first_text(rows: list[dict[str, Any]], key: str) -> str | None:
    for row in rows:
        value = row.get(key)
        if value not in (None, ""):
            return str(value)
    return None


def _first_nested_text(rows: list[dict[str, Any]], parent: str, key: str) -> str | None:
    for row in rows:
        nested = row.get(parent)
        if isinstance(nested, dict) and nested.get(key) not in (None, ""):
            return str(nested[key])
    return None


def _first_int(*payloads: dict[str, Any], keys: tuple[str, ...]) -> int:
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        for key in keys:
            if key in payload:
                return _int_value(payload.get(key))
        summary = payload.get("summary")
        if isinstance(summary, dict):
            value = _first_int(summary, keys=keys)
            if value:
                return value
    return 0


def _int_value(value: Any) -> int:
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(str(value)))
        except (TypeError, ValueError):
            return 0


def _command_name(command: str) -> str:
    parts = command.split()
    if len(parts) >= 2 and parts[0] == "kalshi-bot":
        return parts[1]
    return parts[0] if parts else ""


def _list_value(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    return []


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")


def _write_manifest(path: Path, files: list[Path]) -> None:
    lines: list[str] = []
    for artifact in files:
        if artifact.exists():
            lines.append(f"{hashlib.sha256(artifact.read_bytes()).hexdigest()}  {artifact.name}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _md(value: Any) -> str:
    return str(value).replace("|", "/")
