from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kalshi_predictor.paper.settlement_reconciliation import PAPER_ONLY_SAFETY
from kalshi_predictor.utils.time import utc_now

PHASE3BB_R4_FLIGHTAWARE_VERSION = "phase3bb_r4_flightaware_review_link_gate_v1"
DEFAULT_OUTPUT_DIR = Path("reports/phase3bb_r4_flightaware")
DEFAULT_REPORTS_DIR = Path("reports")
FLIGHTAWARE_ADAPTER = "transportation_flight_cancellation_source"
TARGET_FAMILY = "KXUSFLYCAN-26JUL03"

NEXT_COMMAND_CANDIDATES = (
    "kalshi-bot phase3bb-r4-flightaware-review-link-gate "
    "--output-dir reports/phase3bb_r4_flightaware --reports-dir reports",
    "kalshi-bot phase3bb-r3-source-evidence-activation "
    "--output-dir reports/phase3bb_r3_source_activation --reports-dir reports",
    "kalshi-bot phase3bb-r2-general-source-evidence "
    "--output-dir reports/phase3bb_r2_sources",
    "kalshi-bot phase3ax-gap-analysis --output-dir reports/phase3ax --reports-dir reports",
)


@dataclass(frozen=True)
class Phase3BBR4FlightAwareArtifacts:
    output_dir: Path
    executive_summary_path: Path
    next_actions_path: Path
    next_codex_task_path: Path
    gate_json_path: Path
    gate_markdown_path: Path
    review_checks_path: Path
    command_audit_path: Path
    manifest_path: Path


def build_phase3bb_r4_flightaware_review_link_gate(
    *,
    reports_dir: Path = DEFAULT_REPORTS_DIR,
    registered_commands: set[str] | None = None,
) -> dict[str, Any]:
    """Build the report-only FlightAware review-to-link gate."""

    generated_at = utc_now()
    evidence_report = _read_json(
        reports_dir / "phase3bb_r2_sources" / "phase3bb_r2_general_source_evidence.json"
    )
    availability_report = _read_json(
        reports_dir
        / "phase3bb_r2_sources"
        / "phase3bb_r2_general_source_availability.json"
    )
    date_report = _read_json(
        reports_dir
        / "phase3bb_r2_sources"
        / "flightaware_cancellation_date_resolution.json"
    )
    activation_report = _read_json(
        reports_dir / "phase3bb_r3_source_activation" / "source_evidence_activation.json"
    )

    evidence_rows = [
        row
        for row in _list_value(evidence_report.get("evidence_rows"))
        if row.get("source_adapter_key") == FLIGHTAWARE_ADAPTER
    ]
    availability_row = _first_row(
        _list_value(availability_report.get("availability_rows")),
        "source_adapter_key",
        FLIGHTAWARE_ADAPTER,
    )
    activation_row = _source_activation_row(activation_report)
    checks = _review_checks(
        evidence_rows=evidence_rows,
        availability_row=availability_row,
        date_report=date_report,
        activation_row=activation_row,
    )
    command_audit = _command_registry_audit(set(registered_commands or ()))
    summary = _summary(
        evidence_rows=evidence_rows,
        availability_row=availability_row,
        date_report=date_report,
        activation_row=activation_row,
        checks=checks,
    )
    next_codex_task = _next_codex_task(summary)
    return {
        "generated_at": generated_at.isoformat(),
        "phase": "3BB-R4",
        "phase_version": PHASE3BB_R4_FLIGHTAWARE_VERSION,
        "mode": "PAPER_ONLY_FLIGHTAWARE_REVIEW_LINK_GATE",
        "reports_dir": str(reports_dir),
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "live_or_demo_execution": False,
        "paper_trade_creation": False,
        "order_submission": False,
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
            "flightaware_cancellation_date_resolution": str(
                reports_dir
                / "phase3bb_r2_sources"
                / "flightaware_cancellation_date_resolution.json"
            ),
            "phase3bb_r3_source_activation": str(
                reports_dir
                / "phase3bb_r3_source_activation"
                / "source_evidence_activation.json"
            ),
        },
        "summary": summary,
        "flightaware_review_checks": checks,
        "flightaware_evidence": _flightaware_evidence_reference(
            evidence_rows=evidence_rows,
            availability_row=availability_row,
            date_report=date_report,
        ),
        "command_registry_audit": command_audit,
        "next_actions": _registered_next_commands(command_audit),
        "next_codex_task": next_codex_task,
        "operator_do_not_run": [
            "Do not submit, cancel, replace, or amend live/demo exchange orders.",
            "Do not create paper trades from FlightAware diagnostics.",
            "Do not mark FlightAware link-safe from a relative live page.",
            "Do not mark FlightAware forecast-safe without date-stable evidence.",
            "Do not use sibling/fuzzy ticker matching or inferred cancellation totals.",
        ],
    }


def write_phase3bb_r4_flightaware_review_link_gate_report(
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    reports_dir: Path = DEFAULT_REPORTS_DIR,
    registered_commands: set[str] | None = None,
) -> Phase3BBR4FlightAwareArtifacts:
    payload = build_phase3bb_r4_flightaware_review_link_gate(
        reports_dir=reports_dir,
        registered_commands=registered_commands,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    executive_summary_path = output_dir / "EXECUTIVE_SUMMARY.md"
    next_actions_path = output_dir / "NEXT_ACTIONS.md"
    next_codex_task_path = output_dir / "NEXT_CODEX_TASK.md"
    gate_json_path = output_dir / "flightaware_review_link_gate.json"
    gate_markdown_path = output_dir / "flightaware_review_link_gate.md"
    review_checks_path = output_dir / "flightaware_review_checks.json"
    command_audit_path = output_dir / "flightaware_command_audit.json"
    manifest_path = output_dir / "MANIFEST.sha256"

    _write_json(gate_json_path, payload)
    _write_json(review_checks_path, payload["flightaware_review_checks"])
    _write_json(command_audit_path, payload["command_registry_audit"])
    executive_summary_path.write_text(_render_executive_summary(payload), encoding="utf-8")
    next_actions_path.write_text(_render_next_actions(payload), encoding="utf-8")
    next_codex_task_path.write_text(_render_next_codex_task(payload), encoding="utf-8")
    gate_markdown_path.write_text(_render_gate_markdown(payload), encoding="utf-8")
    _write_manifest(
        manifest_path,
        [
            executive_summary_path,
            next_actions_path,
            next_codex_task_path,
            gate_json_path,
            gate_markdown_path,
            review_checks_path,
            command_audit_path,
        ],
    )
    return Phase3BBR4FlightAwareArtifacts(
        output_dir=output_dir,
        executive_summary_path=executive_summary_path,
        next_actions_path=next_actions_path,
        next_codex_task_path=next_codex_task_path,
        gate_json_path=gate_json_path,
        gate_markdown_path=gate_markdown_path,
        review_checks_path=review_checks_path,
        command_audit_path=command_audit_path,
        manifest_path=manifest_path,
    )


def _review_checks(
    *,
    evidence_rows: list[dict[str, Any]],
    availability_row: dict[str, Any],
    date_report: dict[str, Any],
    activation_row: dict[str, Any],
) -> list[dict[str, Any]]:
    exact_source_ok = date_report.get("exact_july_3_report_found") is True and bool(
        date_report.get("observed_value_filled")
    )
    reviewer_approved = _truthy(date_report.get("review_approved")) or _truthy(
        activation_row.get("review_approved")
    )
    return [
        _check(
            "exact_date_stable_source",
            exact_source_ok,
            "DATE_STABLE_FLIGHTAWARE_SOURCE_MISSING",
            "No audited official FlightAware July 3, 2026 historical aggregate is present.",
        ),
        _check(
            "entity_scope_mapping",
            _entity_scope_ok(evidence_rows, date_report),
            "ENTITY_SCOPE_AMBIGUOUS",
            "Rows must map only to United States total flight cancellations.",
            review_only=True,
        ),
        _check(
            "time_window_mapping",
            _time_window_ok(evidence_rows, date_report),
            "TIME_WINDOW_NOT_PROVEN",
            "Rows must map to the week ending July 3, 2026.",
            review_only=True,
        ),
        _check(
            "freshness_and_finality",
            exact_source_ok,
            "FINAL_DATE_STABLE_TOTAL_MISSING",
            "Relative public pages are mutable and cannot prove final July 3 data.",
        ),
        _check(
            "no_leakage",
            _no_leakage_ok(evidence_rows, availability_row, date_report),
            "FAMILY_OR_TICKER_LEAKAGE",
            "Only KXUSFLYCAN-26JUL03 rows may be considered.",
            review_only=True,
        ),
        _check(
            "review_approval",
            reviewer_approved,
            "REVIEW_APPROVAL_MISSING",
            "A human or reviewed report flag must approve the exact source mapping.",
        ),
    ]


def _summary(
    *,
    evidence_rows: list[dict[str, Any]],
    availability_row: dict[str, Any],
    date_report: dict[str, Any],
    activation_row: dict[str, Any],
    checks: list[dict[str, Any]],
) -> dict[str, Any]:
    affected_rows = max(
        len(evidence_rows),
        _int_value(availability_row.get("affected_diagnostic_rows")),
        _int_value(activation_row.get("affected_rows")),
    )
    hard_failures = [row for row in checks if row["status"] == "FAIL"]
    hard_blocker = _first_hard_blocker(hard_failures)
    exact_ready = sum(
        1
        for row in evidence_rows
        if row.get("evidence_status") == "EXACT_EVIDENCE_READY_FOR_REVIEW"
    )
    all_checks_pass = not hard_failures and all(row["status"] == "PASS" for row in checks)
    return {
        "review_gate_status": "READY_FOR_LINK_DRY_RUN" if all_checks_pass else "BLOCKED",
        "activation_readiness": "READY" if all_checks_pass else "NOT_READY",
        "first_hard_blocker": hard_blocker,
        "affected_rows": affected_rows,
        "affected_tickers": _affected_tickers(evidence_rows, availability_row, date_report),
        "evidence_ready_rows": exact_ready,
        "source_value_available_for_review": exact_ready > 0,
        "observed_value": availability_row.get("observed_value")
        or _first_observed_value(evidence_rows),
        "date_stable_evidence_available": date_report.get("exact_july_3_report_found")
        is True,
        "public_relative_page_accepted": _truthy(
            _nested(
                date_report,
                "latest_public_recent_snapshot",
                "accepted_as_exact_july_3_evidence",
            )
        ),
        "link_safe_decision": "ALLOW_REPORT_ONLY" if all_checks_pass else "BLOCK",
        "forecast_safe_decision": "ALLOW_REPORT_ONLY" if all_checks_pass else "BLOCK",
        "link_safe_rows": affected_rows if all_checks_pass else 0,
        "forecast_safe_rows": affected_rows if all_checks_pass else 0,
        "promoted_to_link_safe_rows": 0,
        "promoted_to_forecast_safe_rows": 0,
        "proposed_db_writes": 0,
        "paper_trade_writes": False,
        "live_or_demo_execution": False,
        "thresholds_lowered": False,
        "fabricated_evidence": False,
        "review_check_counts": _status_counts(checks),
        "next_action": _next_action(hard_blocker),
    }


def _check(
    gate_name: str,
    passed: bool,
    blocker: str,
    evidence: str,
    *,
    review_only: bool = False,
) -> dict[str, Any]:
    status = "PASS" if passed and not review_only else "PASS_REVIEW_ONLY" if passed else "FAIL"
    return {
        "gate": gate_name,
        "status": status,
        "passes_link_safe_gate": status == "PASS",
        "passes_forecast_safe_gate": status == "PASS",
        "blocker_code": "NONE" if passed else blocker,
        "evidence": evidence,
    }


def _next_codex_task(summary: dict[str, Any]) -> dict[str, Any]:
    if summary["first_hard_blocker"] == "DATE_STABLE_FLIGHTAWARE_SOURCE_MISSING":
        phase = "Phase 3BB-R5 FlightAware Date-Stable Evidence Capture"
        reason = (
            "R4 review gates are defined, but FlightAware still lacks an official "
            "date-stable July 3, 2026 aggregate."
        )
        problem = (
            "Capture or document the unavailable audited FlightAware historical "
            "aggregate without using relative live pages as exact evidence."
        )
    elif summary["first_hard_blocker"] == "REVIEW_APPROVAL_MISSING":
        phase = "Phase 3BB-R5 FlightAware Manual Review Approval"
        reason = "FlightAware source mapping is otherwise review-ready but lacks approval."
        problem = "Add report-only reviewed approval evidence before promotion."
    else:
        phase = "Phase 3BB-R5 General Source Promotion Dry Run"
        reason = "FlightAware review gates are ready for a report-only promotion dry run."
        problem = "Dry-run link-safe/forecast-safe promotion without DB or trade writes."
    return {
        "task_phase_name": phase,
        "reason": reason,
        "problem_statement": problem,
        "acceptance_criteria": [
            "Keep everything PAPER / READ-ONLY.",
            "Do not submit/cancel/replace/amend live or demo exchange orders.",
            "Do not create paper trades from source diagnostics.",
            "Do not use relative FlightAware live pages as date-stable evidence.",
            "NEXT_ACTIONS must reference only registered kalshi-bot commands.",
        ],
        "full_codex_prompt": _render_codex_prompt(phase, reason, problem),
        "estimated_risk_level": "MEDIUM",
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
                "recommended_fix": "No action." if registered else "Register or remove.",
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


def _render_executive_summary(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    return "\n".join(
        [
            "# Phase 3BB-R4 Executive Summary",
            "",
            f"- Generated at: `{payload['generated_at']}`",
            f"- Review gate status: `{summary['review_gate_status']}`",
            f"- First hard blocker: `{summary['first_hard_blocker']}`",
            f"- Evidence-ready rows: `{summary['evidence_ready_rows']}`",
            f"- Link-safe rows: `{summary['link_safe_rows']}`",
            f"- Forecast-safe rows: `{summary['forecast_safe_rows']}`",
            f"- Promoted rows: `{summary['promoted_to_link_safe_rows']}` link-safe, "
            f"`{summary['promoted_to_forecast_safe_rows']}` forecast-safe",
            f"- Next Codex task: `{payload['next_codex_task']['task_phase_name']}`",
            "",
            "No live/demo exchange writes, paper trades, threshold changes, or fabricated "
            "evidence were produced by this report.",
            "",
        ]
    )


def _render_next_actions(payload: dict[str, Any]) -> str:
    lines = [
        "# Phase 3BB-R4 Next Actions",
        "",
        f"- Review gate status: `{payload['summary']['review_gate_status']}`",
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
            "- Obtain an audited FlightAware export, API-backed aggregate, or Rapid Report.",
            "- Keep relative live pages diagnostic-only.",
            "- Keep all FlightAware rows review-gated until date-stable evidence exists.",
            "",
            "Missing command details are recorded in `flightaware_command_audit.json`.",
            "",
        ]
    )
    return "\n".join(lines)


def _render_next_codex_task(payload: dict[str, Any]) -> str:
    task = payload["next_codex_task"]
    lines = [
        "# Phase 3BB-R4 Next Codex Task",
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


def _render_gate_markdown(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    lines = [
        "# Phase 3BB-R4 FlightAware Review-to-Link Gate",
        "",
        f"- Generated at: `{payload['generated_at']}`",
        f"- Mode: `{payload['mode']}`",
        f"- Gate status: `{summary['review_gate_status']}`",
        f"- First hard blocker: `{summary['first_hard_blocker']}`",
        "",
        "## Checks",
        "",
        "| Gate | Status | Blocker | Evidence |",
        "| --- | --- | --- | --- |",
    ]
    for row in payload["flightaware_review_checks"]:
        lines.append(
            "| {gate} | {status} | {blocker} | {evidence} |".format(
                gate=_md(row["gate"]),
                status=_md(row["status"]),
                blocker=_md(row["blocker_code"]),
                evidence=_md(row["evidence"]),
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
            "5. Use exact FlightAware evidence only; no sibling/fuzzy source matching.",
            "6. NEXT_ACTIONS and operator docs must reference only registered commands.",
            "",
            "Acceptance:",
            "- The selected source gap is fixed or reported with exact evidence.",
            "- Unsafe source rows remain diagnostic-only.",
            "- The safety guard remains intact.",
        ]
    )


def _flightaware_evidence_reference(
    *,
    evidence_rows: list[dict[str, Any]],
    availability_row: dict[str, Any],
    date_report: dict[str, Any],
) -> dict[str, Any]:
    return {
        "source_adapter_key": FLIGHTAWARE_ADAPTER,
        "source_name": availability_row.get("source_name")
        or _first_nested_text(evidence_rows, "matched_evidence", "source_name"),
        "source_url": availability_row.get("source_url")
        or _first_nested_text(evidence_rows, "matched_evidence", "source_url"),
        "underlying_source_name": _first_nested_text(
            evidence_rows, "matched_evidence", "underlying_source_name"
        ),
        "underlying_source_url": _first_nested_text(
            evidence_rows, "matched_evidence", "underlying_source_url"
        ),
        "observed_value": availability_row.get("observed_value")
        or _first_observed_value(evidence_rows),
        "latest_public_recent_snapshot": date_report.get("latest_public_recent_snapshot"),
        "target": date_report.get("target"),
    }


def _entity_scope_ok(evidence_rows: list[dict[str, Any]], date_report: dict[str, Any]) -> bool:
    target_region = str(_nested(date_report, "target", "region") or "United States").lower()
    regions = {
        str(_nested(row, "parsed_fields", "region") or "").lower()
        for row in evidence_rows
    }
    return bool(evidence_rows) and regions <= {target_region} and target_region in regions


def _time_window_ok(evidence_rows: list[dict[str, Any]], date_report: dict[str, Any]) -> bool:
    target_date = str(_nested(date_report, "target", "target_date") or "July 3, 2026")
    for row in evidence_rows:
        parsed_window = str(_nested(row, "parsed_fields", "time_window") or "")
        period_end = str(_nested(row, "matched_evidence", "period_end") or "")
        if target_date not in {parsed_window, period_end}:
            return False
    return bool(evidence_rows)


def _no_leakage_ok(
    evidence_rows: list[dict[str, Any]],
    availability_row: dict[str, Any],
    date_report: dict[str, Any],
) -> bool:
    tickers = _affected_tickers(evidence_rows, availability_row, date_report)
    return bool(tickers) and all(ticker.startswith(TARGET_FAMILY) for ticker in tickers)


def _affected_tickers(
    evidence_rows: list[dict[str, Any]],
    availability_row: dict[str, Any],
    date_report: dict[str, Any],
) -> list[str]:
    values = _list_value(_nested(date_report, "target", "tickers"))
    if not values:
        values = _list_value(availability_row.get("affected_tickers"))
    if not values:
        values = [row.get("ticker") for row in evidence_rows]
    return sorted({str(value) for value in values if value})


def _first_hard_blocker(failures: list[dict[str, Any]]) -> str:
    priority = (
        "DATE_STABLE_FLIGHTAWARE_SOURCE_MISSING",
        "FINAL_DATE_STABLE_TOTAL_MISSING",
        "REVIEW_APPROVAL_MISSING",
        "TIME_WINDOW_NOT_PROVEN",
        "ENTITY_SCOPE_AMBIGUOUS",
        "FAMILY_OR_TICKER_LEAKAGE",
    )
    blockers = {str(row.get("blocker_code")) for row in failures}
    for blocker in priority:
        if blocker in blockers:
            return blocker
    return str(failures[0]["blocker_code"]) if failures else "NONE"


def _next_action(blocker: str) -> str:
    if blocker == "DATE_STABLE_FLIGHTAWARE_SOURCE_MISSING":
        return "Capture an audited date-stable FlightAware July 3, 2026 aggregate."
    if blocker == "REVIEW_APPROVAL_MISSING":
        return "Record reviewed approval after exact source mapping is proven."
    if blocker == "NONE":
        return "Run a report-only promotion dry run; do not write links or forecasts here."
    return "Keep FlightAware diagnostic-only until the failed review gate clears."


def _source_activation_row(activation_report: dict[str, Any]) -> dict[str, Any]:
    for row in _list_value(activation_report.get("source_activation_decisions")):
        if row.get("source_adapter_key") == FLIGHTAWARE_ADAPTER:
            return row
    return {}


def _first_row(rows: list[Any], key: str, value: str) -> dict[str, Any]:
    for row in rows:
        if isinstance(row, dict) and row.get(key) == value:
            return row
    return {}


def _first_observed_value(evidence_rows: list[dict[str, Any]]) -> Any:
    for row in evidence_rows:
        value = _nested(row, "matched_evidence", "cancellation_count")
        if value not in (None, ""):
            return value
    return None


def _first_nested_text(rows: list[dict[str, Any]], parent: str, key: str) -> str | None:
    for row in rows:
        value = _nested(row, parent, key)
        if value not in (None, ""):
            return str(value)
    return None


def _status_counts(checks: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in checks:
        status = str(row.get("status") or "UNKNOWN")
        counts[status] = counts.get(status, 0) + 1
    return dict(sorted(counts.items()))


def _nested(payload: Any, *keys: str) -> Any:
    current = payload
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "y", "approved"}


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
    lines = []
    for artifact in files:
        if artifact.exists():
            lines.append(f"{hashlib.sha256(artifact.read_bytes()).hexdigest()}  {artifact.name}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _md(value: Any) -> str:
    return str(value).replace("|", "/")
