from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from kalshi_predictor.paper.settlement_reconciliation import PAPER_ONLY_SAFETY
from kalshi_predictor.utils.time import utc_now

PHASE3BB_R5_FLIGHTAWARE_VERSION = "phase3bb_r5_flightaware_date_stable_evidence_v1"
DEFAULT_OUTPUT_DIR = Path("reports/phase3bb_r5_flightaware")
DEFAULT_REPORTS_DIR = Path("reports")
DEFAULT_EVIDENCE_DIR = Path("data/general_source_evidence")
FLIGHTAWARE_ADAPTER = "transportation_flight_cancellation_source"
TARGET_DATE = "July 3, 2026"

NEXT_COMMAND_CANDIDATES = (
    "kalshi-bot phase3bb-r5-flightaware-date-stable-evidence "
    "--output-dir reports/phase3bb_r5_flightaware --reports-dir reports",
    "kalshi-bot phase3bb-r4-flightaware-review-link-gate "
    "--output-dir reports/phase3bb_r4_flightaware --reports-dir reports",
    "kalshi-bot phase3bb-r2-general-source-availability "
    "--output-dir reports/phase3bb_r2_sources",
    "kalshi-bot phase3ax-gap-analysis --output-dir reports/phase3ax --reports-dir reports",
)


@dataclass(frozen=True)
class Phase3BBR5FlightAwareArtifacts:
    output_dir: Path
    executive_summary_path: Path
    next_actions_path: Path
    next_codex_task_path: Path
    evidence_json_path: Path
    evidence_markdown_path: Path
    candidate_rows_path: Path
    command_audit_path: Path
    manifest_path: Path


def build_phase3bb_r5_flightaware_date_stable_evidence(
    *,
    reports_dir: Path = DEFAULT_REPORTS_DIR,
    evidence_dir: Path = DEFAULT_EVIDENCE_DIR,
    registered_commands: set[str] | None = None,
) -> dict[str, Any]:
    """Audit whether any exact date-stable FlightAware evidence is available."""

    generated_at = utc_now()
    date_report = _read_json(
        reports_dir
        / "phase3bb_r2_sources"
        / "flightaware_cancellation_date_resolution.json"
    )
    r4_gate = _read_json(
        reports_dir / "phase3bb_r4_flightaware" / "flightaware_review_link_gate.json"
    )
    canonical_evidence = _read_json(
        evidence_dir / "transportation_flight_cancellation_source.json"
    )
    candidates = _candidate_rows(
        date_report=date_report,
        r4_gate=r4_gate,
        canonical_evidence=canonical_evidence,
        evidence_dir=evidence_dir,
    )
    command_audit = _command_registry_audit(set(registered_commands or ()))
    summary = _summary(candidates=candidates, date_report=date_report, r4_gate=r4_gate)
    next_codex_task = _next_codex_task(summary)
    return {
        "generated_at": generated_at.isoformat(),
        "phase": "3BB-R5",
        "phase_version": PHASE3BB_R5_FLIGHTAWARE_VERSION,
        "mode": "PAPER_ONLY_FLIGHTAWARE_DATE_STABLE_EVIDENCE_CAPTURE",
        "reports_dir": str(reports_dir),
        "evidence_dir": str(evidence_dir),
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "live_or_demo_execution": False,
        "paper_trade_creation": False,
        "order_submission": False,
        "thresholds_lowered": False,
        "fabricated_evidence": False,
        "network_fetches_performed": False,
        "source_reports_used": {
            "flightaware_cancellation_date_resolution": str(
                reports_dir
                / "phase3bb_r2_sources"
                / "flightaware_cancellation_date_resolution.json"
            ),
            "phase3bb_r4_flightaware_gate": str(
                reports_dir
                / "phase3bb_r4_flightaware"
                / "flightaware_review_link_gate.json"
            ),
            "canonical_evidence_file": str(
                evidence_dir / "transportation_flight_cancellation_source.json"
            ),
        },
        "summary": summary,
        "candidate_evidence_rows": candidates,
        "accepted_date_stable_evidence_rows": [
            row for row in candidates if row["accepted_as_date_stable_evidence"]
        ],
        "command_registry_audit": command_audit,
        "next_actions": _registered_next_commands(command_audit),
        "next_codex_task": next_codex_task,
        "operator_do_not_run": [
            "Do not submit, cancel, replace, or amend live/demo exchange orders.",
            "Do not create paper trades from FlightAware diagnostics.",
            "Do not accept relative FlightAware live pages as exact historical evidence.",
            "Do not treat the Kalshi outcome page as an official FlightAware aggregate.",
            "Do not infer cancellation totals from thresholds or sibling markets.",
        ],
    }


def write_phase3bb_r5_flightaware_date_stable_evidence_report(
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    reports_dir: Path = DEFAULT_REPORTS_DIR,
    evidence_dir: Path = DEFAULT_EVIDENCE_DIR,
    registered_commands: set[str] | None = None,
) -> Phase3BBR5FlightAwareArtifacts:
    payload = build_phase3bb_r5_flightaware_date_stable_evidence(
        reports_dir=reports_dir,
        evidence_dir=evidence_dir,
        registered_commands=registered_commands,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    executive_summary_path = output_dir / "EXECUTIVE_SUMMARY.md"
    next_actions_path = output_dir / "NEXT_ACTIONS.md"
    next_codex_task_path = output_dir / "NEXT_CODEX_TASK.md"
    evidence_json_path = output_dir / "flightaware_date_stable_evidence.json"
    evidence_markdown_path = output_dir / "flightaware_date_stable_evidence.md"
    candidate_rows_path = output_dir / "flightaware_evidence_candidates.json"
    command_audit_path = output_dir / "flightaware_command_audit.json"
    manifest_path = output_dir / "MANIFEST.sha256"

    _write_json(evidence_json_path, payload)
    _write_json(candidate_rows_path, payload["candidate_evidence_rows"])
    _write_json(command_audit_path, payload["command_registry_audit"])
    executive_summary_path.write_text(_render_executive_summary(payload), encoding="utf-8")
    next_actions_path.write_text(_render_next_actions(payload), encoding="utf-8")
    next_codex_task_path.write_text(_render_next_codex_task(payload), encoding="utf-8")
    evidence_markdown_path.write_text(_render_evidence_markdown(payload), encoding="utf-8")
    _write_manifest(
        manifest_path,
        [
            executive_summary_path,
            next_actions_path,
            next_codex_task_path,
            evidence_json_path,
            evidence_markdown_path,
            candidate_rows_path,
            command_audit_path,
        ],
    )
    return Phase3BBR5FlightAwareArtifacts(
        output_dir=output_dir,
        executive_summary_path=executive_summary_path,
        next_actions_path=next_actions_path,
        next_codex_task_path=next_codex_task_path,
        evidence_json_path=evidence_json_path,
        evidence_markdown_path=evidence_markdown_path,
        candidate_rows_path=candidate_rows_path,
        command_audit_path=command_audit_path,
        manifest_path=manifest_path,
    )


def _candidate_rows(
    *,
    date_report: dict[str, Any],
    r4_gate: dict[str, Any],
    canonical_evidence: dict[str, Any],
    evidence_dir: Path,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for index, record in enumerate(_records(canonical_evidence), start=1):
        rows.append(
            _candidate(
                candidate_id=f"canonical_local_record_{index}",
                source_kind="canonical_local_source_record",
                source_name=record.get("source_name"),
                source_url=record.get("source_url"),
                underlying_source_name=record.get("underlying_source_name"),
                underlying_source_url=record.get("underlying_source_url"),
                observed_value=record.get("cancellation_count"),
                target_date=record.get("period_end"),
                local_artifact=str(evidence_dir / "transportation_flight_cancellation_source.json"),
                raw_evidence=record,
                accepted_flag=(
                    record.get("accepted_as_exact_july_3_evidence")
                    or record.get("date_stable")
                    or record.get("verification_status")
                ),
            )
        )
    r4_evidence = r4_gate.get("flightaware_evidence")
    if isinstance(r4_evidence, dict):
        rows.append(
            _candidate(
                candidate_id="phase3bb_r4_flightaware_evidence",
                source_kind="r4_review_gate_evidence",
                source_name=r4_evidence.get("source_name"),
                source_url=r4_evidence.get("source_url"),
                underlying_source_name=r4_evidence.get("underlying_source_name"),
                underlying_source_url=r4_evidence.get("underlying_source_url"),
                observed_value=r4_evidence.get("observed_value"),
                target_date=_nested(r4_evidence, "target", "target_date"),
                local_artifact="reports/phase3bb_r4_flightaware/flightaware_review_link_gate.json",
                raw_evidence=r4_evidence,
            )
        )
    latest = date_report.get("latest_public_recent_snapshot")
    if isinstance(latest, dict):
        rows.append(
            _candidate(
                candidate_id="flightaware_latest_public_recent_snapshot",
                source_kind="flightaware_public_recent_snapshot",
                source_name="FlightAware public recent cancellation page",
                source_url=latest.get("url"),
                underlying_source_name="FlightAware",
                underlying_source_url=latest.get("url"),
                observed_value=latest.get("us_scope_cancellations"),
                target_date=latest.get("us_scope_label") or latest.get("headline_period"),
                local_artifact=latest.get("local_html"),
                raw_evidence=latest,
                accepted_flag=latest.get("accepted_as_exact_july_3_evidence"),
            )
        )
    for index, checked in enumerate(_list_value(date_report.get("sources_checked")), start=1):
        rows.extend(_source_checked_candidates(index, checked))
    return _dedupe_candidates(rows)


def _source_checked_candidates(index: int, checked: Any) -> list[dict[str, Any]]:
    if not isinstance(checked, dict):
        return []
    name = str(checked.get("name") or f"source_checked_{index}")
    if "Rapid Reports" in name or "AeroAPI" in name:
        return [
            _candidate(
                candidate_id=f"sources_checked_{index}_access_product",
                source_kind="flightaware_access_product",
                source_name=name,
                source_url=", ".join(str(url) for url in _list_value(checked.get("urls"))),
                underlying_source_name="FlightAware",
                underlying_source_url=None,
                observed_value=None,
                target_date=TARGET_DATE,
                local_artifact=None,
                raw_evidence=checked,
            )
        ]
    if "exact-date" in name.lower():
        return [
            _candidate(
                candidate_id=f"sources_checked_{index}_exact_date_url_probe",
                source_kind="flightaware_exact_date_url_probe",
                source_name=name,
                source_url=", ".join(str(url) for url in _list_value(checked.get("urls"))),
                underlying_source_name="FlightAware",
                underlying_source_url=None,
                observed_value=None,
                target_date=TARGET_DATE,
                local_artifact=", ".join(
                    str(path) for path in _list_value(checked.get("local_files"))
                ),
                raw_evidence=checked,
            )
        ]
    if "live cancellation" in name.lower() or "relative" in name.lower():
        return [
            _candidate(
                candidate_id=f"sources_checked_{index}_relative_page",
                source_kind="flightaware_relative_live_page",
                source_name=name,
                source_url=checked.get("url"),
                underlying_source_name="FlightAware",
                underlying_source_url=checked.get("url"),
                observed_value=checked.get("parsed_us_scope_cancellations"),
                target_date=checked.get("headline_period"),
                local_artifact=checked.get("local_file"),
                raw_evidence=checked,
                accepted_flag=checked.get("accepted_as_evidence"),
            )
        ]
    if "local archived" in name.lower():
        return [
            _candidate(
                candidate_id=f"sources_checked_{index}_local_archive_search",
                source_kind="local_archive_search",
                source_name=name,
                source_url=None,
                underlying_source_name="FlightAware",
                underlying_source_url=None,
                observed_value=None,
                target_date=TARGET_DATE,
                local_artifact=", ".join(
                    str(path) for path in _list_value(checked.get("paths_searched"))
                ),
                raw_evidence=checked,
            )
        ]
    return []


def _candidate(
    *,
    candidate_id: str,
    source_kind: str,
    source_name: Any,
    source_url: Any,
    underlying_source_name: Any,
    underlying_source_url: Any,
    observed_value: Any,
    target_date: Any,
    local_artifact: Any,
    raw_evidence: dict[str, Any],
    accepted_flag: Any = None,
) -> dict[str, Any]:
    source_url_text = _text(source_url)
    underlying_url_text = _text(underlying_source_url)
    official_url = _is_flightaware_url(source_url_text) or _is_flightaware_url(
        underlying_url_text
    )
    mutable_relative = _is_relative_live_page(source_url_text) or _is_relative_live_page(
        underlying_url_text
    )
    exact_date = str(target_date or "").strip() == TARGET_DATE
    value_present = observed_value not in (None, "")
    is_kalshi_outcome = "kalshi" in _host(source_url_text)
    access_product = source_kind == "flightaware_access_product"
    accepted = (
        _truthy(accepted_flag)
        and official_url
        and exact_date
        and value_present
        and not mutable_relative
        and not is_kalshi_outcome
    )
    rejection_code = "NONE" if accepted else _rejection_code(
        official_url=official_url,
        mutable_relative=mutable_relative,
        exact_date=exact_date,
        value_present=value_present,
        is_kalshi_outcome=is_kalshi_outcome,
        access_product=access_product,
    )
    return {
        "candidate_id": candidate_id,
        "source_kind": source_kind,
        "source_name": _text(source_name),
        "source_url": source_url_text,
        "underlying_source_name": _text(underlying_source_name),
        "underlying_source_url": underlying_url_text,
        "observed_value": observed_value,
        "target_date": _text(target_date),
        "local_artifact": _text(local_artifact),
        "official_flightaware_url_present": official_url,
        "exact_target_date_present": exact_date,
        "observed_value_present": value_present,
        "relative_or_mutable_live_page": mutable_relative,
        "kalshi_outcome_page": is_kalshi_outcome,
        "access_or_paid_product_required": access_product,
        "accepted_as_date_stable_evidence": accepted,
        "rejection_code": rejection_code,
        "classification": "ACCEPTED_DATE_STABLE_EVIDENCE" if accepted else "REJECTED",
        "sha256": _candidate_sha(raw_evidence),
    }


def _summary(
    *,
    candidates: list[dict[str, Any]],
    date_report: dict[str, Any],
    r4_gate: dict[str, Any],
) -> dict[str, Any]:
    accepted = [row for row in candidates if row["accepted_as_date_stable_evidence"]]
    access_required = [
        row for row in candidates if row["rejection_code"] == "ACCESS_REQUIRED"
    ]
    relative_rejected = [
        row for row in candidates if row["rejection_code"] == "RELATIVE_OR_MUTABLE_PAGE"
    ]
    kalshi_rejected = [
        row for row in candidates if row["rejection_code"] == "KALSHI_OUTCOME_NOT_OFFICIAL"
    ]
    evidence_status = "FOUND_READY_FOR_REVIEW" if accepted else "NOT_FOUND"
    first_blocker = "NONE" if accepted else "OFFICIAL_FLIGHTAWARE_HISTORICAL_AGGREGATE_UNAVAILABLE"
    observed_value = _nested(r4_gate, "summary", "observed_value") or _first_observed(candidates)
    affected_rows = _int_value(_nested(r4_gate, "summary", "affected_rows"))
    return {
        "date_stable_evidence_status": evidence_status,
        "first_hard_blocker": first_blocker,
        "candidate_evidence_rows": len(candidates),
        "accepted_date_stable_evidence_rows": len(accepted),
        "access_required_rows": len(access_required),
        "rejected_relative_live_page_rows": len(relative_rejected),
        "rejected_kalshi_outcome_page_rows": len(kalshi_rejected),
        "observed_value_ready_for_review": observed_value,
        "source_value_available_for_review": observed_value not in (None, ""),
        "affected_rows": affected_rows,
        "exact_july_3_report_found": bool(date_report.get("exact_july_3_report_found")),
        "observed_value_filled_in_group_review": bool(date_report.get("observed_value_filled")),
        "signup_or_paid_product_likely_required": bool(
            _nested(
                date_report,
                "flightaware_signup_assessment",
                "signup_or_paid_product_likely_required_for_audited_date_stable_historical_aggregate",
            )
        ),
        "link_safe_decision": "BLOCK",
        "forecast_safe_decision": "BLOCK",
        "link_safe_rows": 0,
        "forecast_safe_rows": 0,
        "promoted_to_link_safe_rows": 0,
        "promoted_to_forecast_safe_rows": 0,
        "proposed_db_writes": 0,
        "paper_trade_writes": False,
        "live_or_demo_execution": False,
        "thresholds_lowered": False,
        "fabricated_evidence": False,
        "network_fetches_performed": False,
        "candidate_classification_counts": _classification_counts(candidates),
        "next_action": (
            "External FlightAware historical aggregate access is required before "
            "link/forecast promotion."
            if not accepted
            else "Run manual review approval before any link/forecast promotion."
        ),
    }


def _next_codex_task(summary: dict[str, Any]) -> dict[str, Any]:
    if summary["accepted_date_stable_evidence_rows"] > 0:
        phase = "Phase 3BB-R6 FlightAware Manual Review Approval"
        reason = "A date-stable FlightAware candidate exists but still needs reviewed approval."
        problem = (
            "Record report-only review approval before any link-safe or "
            "forecast-safe dry run."
        )
    else:
        phase = "Phase 3AH-R3 Sports Provenance Repair"
        reason = (
            "FlightAware date-stable evidence is blocked on external source access; "
            "move to the next code-repairable app gap."
        )
        problem = (
            "Create safe sports provenance repair rows only where schedule/roster "
            "evidence is exact."
        )
    return {
        "task_phase_name": phase,
        "reason": reason,
        "problem_statement": problem,
        "acceptance_criteria": [
            "Keep everything PAPER / READ-ONLY.",
            "Do not submit/cancel/replace/amend live or demo exchange orders.",
            "Do not create paper trades from diagnostics.",
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
            "# Phase 3BB-R5 Executive Summary",
            "",
            f"- Generated at: `{payload['generated_at']}`",
            f"- Date-stable evidence status: `{summary['date_stable_evidence_status']}`",
            f"- First hard blocker: `{summary['first_hard_blocker']}`",
            f"- Candidate evidence rows: `{summary['candidate_evidence_rows']}`",
            f"- Accepted date-stable rows: `{summary['accepted_date_stable_evidence_rows']}`",
            f"- Access-required rows: `{summary['access_required_rows']}`",
            f"- Relative live pages rejected: `{summary['rejected_relative_live_page_rows']}`",
            f"- Kalshi outcome pages rejected as official source: "
            f"`{summary['rejected_kalshi_outcome_page_rows']}`",
            f"- Link-safe rows: `{summary['link_safe_rows']}`",
            f"- Forecast-safe rows: `{summary['forecast_safe_rows']}`",
            f"- Next Codex task: `{payload['next_codex_task']['task_phase_name']}`",
            "",
            "No live/demo exchange writes, paper trades, threshold changes, network fetches, "
            "or fabricated evidence were produced by this report.",
            "",
        ]
    )


def _render_next_actions(payload: dict[str, Any]) -> str:
    lines = [
        "# Phase 3BB-R5 Next Actions",
        "",
        f"- Date-stable evidence status: `{payload['summary']['date_stable_evidence_status']}`",
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
            "- Obtain a FlightAware AeroAPI export, Rapid Report, or audited historical aggregate.",
            "- Keep the Kalshi outcome page review-only unless official "
            "FlightAware evidence is attached.",
            "- Keep relative live pages diagnostic-only.",
            "",
            "Missing command details are recorded in `flightaware_command_audit.json`.",
            "",
        ]
    )
    return "\n".join(lines)


def _render_next_codex_task(payload: dict[str, Any]) -> str:
    task = payload["next_codex_task"]
    lines = [
        "# Phase 3BB-R5 Next Codex Task",
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


def _render_evidence_markdown(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    lines = [
        "# Phase 3BB-R5 FlightAware Date-Stable Evidence",
        "",
        f"- Generated at: `{payload['generated_at']}`",
        f"- Mode: `{payload['mode']}`",
        f"- Evidence status: `{summary['date_stable_evidence_status']}`",
        f"- First hard blocker: `{summary['first_hard_blocker']}`",
        "",
        "## Candidates",
        "",
        "| Candidate | Kind | Accepted | Rejection | Observed | URL |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for row in payload["candidate_evidence_rows"]:
        lines.append(
            "| {candidate} | {kind} | {accepted} | {rejection} | {observed} | {url} |".format(
                candidate=_md(row["candidate_id"]),
                kind=_md(row["source_kind"]),
                accepted="yes" if row["accepted_as_date_stable_evidence"] else "no",
                rejection=_md(row["rejection_code"]),
                observed=_md(row["observed_value"]),
                url=_md(row["source_url"] or row["underlying_source_url"]),
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
            "3. Do not create paper trades from diagnostics.",
            "4. Do not lower thresholds or fabricate evidence.",
            "5. Use exact evidence only; no sibling/fuzzy matching.",
            "6. NEXT_ACTIONS and operator docs must reference only registered commands.",
            "",
            "Acceptance:",
            "- The selected gap is fixed or reported with exact evidence.",
            "- Unsafe rows remain diagnostic-only.",
            "- The safety guard remains intact.",
        ]
    )


def _rejection_code(
    *,
    official_url: bool,
    mutable_relative: bool,
    exact_date: bool,
    value_present: bool,
    is_kalshi_outcome: bool,
    access_product: bool,
) -> str:
    if access_product:
        return "ACCESS_REQUIRED"
    if is_kalshi_outcome:
        return "KALSHI_OUTCOME_NOT_OFFICIAL"
    if mutable_relative:
        return "RELATIVE_OR_MUTABLE_PAGE"
    if not official_url:
        return "NOT_OFFICIAL_FLIGHTAWARE"
    if not exact_date:
        return "TARGET_DATE_NOT_EXACT"
    if not value_present:
        return "OBSERVED_VALUE_MISSING"
    return "REVIEW_APPROVAL_OR_ACCEPTANCE_FLAG_MISSING"


def _is_flightaware_url(url: str) -> bool:
    return "flightaware.com" in _host(url)


def _is_relative_live_page(url: str) -> bool:
    lowered = url.lower()
    if "flightaware.com/live/cancelled" not in lowered:
        return False
    return any(
        token in lowered
        for token in (
            "today",
            "yesterday",
            "minus",
            "/week",
            "/live/cancelled",
        )
    )


def _host(url: str) -> str:
    try:
        return urlparse(url).netloc.lower()
    except ValueError:
        return ""


def _records(payload: dict[str, Any]) -> list[dict[str, Any]]:
    records = payload.get("records")
    if isinstance(records, list):
        return [row for row in records if isinstance(row, dict)]
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    return []


def _dedupe_candidates(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str, str]] = set()
    unique: list[dict[str, Any]] = []
    for row in rows:
        key = (
            str(row.get("source_kind")),
            str(row.get("source_url")),
            str(row.get("underlying_source_url")),
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(row)
    return unique


def _classification_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        key = str(row.get("rejection_code") or "UNKNOWN")
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def _first_observed(candidates: list[dict[str, Any]]) -> Any:
    for row in candidates:
        value = row.get("observed_value")
        if value not in (None, ""):
            return value
    return None


def _candidate_sha(raw_evidence: dict[str, Any]) -> str:
    data = json.dumps(raw_evidence, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


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
    return str(value).strip().lower() in {
        "1",
        "true",
        "yes",
        "y",
        "verified",
        "approved",
        "accepted",
    }


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
    return value if isinstance(value, list) else []


def _text(value: Any) -> str:
    return "" if value is None else str(value)


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
