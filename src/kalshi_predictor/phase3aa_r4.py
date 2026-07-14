from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kalshi_predictor.paper.settlement_reconciliation import PAPER_ONLY_SAFETY
from kalshi_predictor.utils.time import utc_now

PHASE3AA_R4_VERSION = "phase3aa_r4_v1"
DEFAULT_OUTPUT_DIR = Path("reports/phase3aa_r4")


@dataclass(frozen=True)
class Phase3AAR4ArtifactSet:
    output_dir: Path
    json_path: Path
    markdown_path: Path
    rows_path: Path


def build_phase3aa_r4_settlement_fetch_recovery(
    *,
    reports_dir: Path = Path("reports"),
    sample_limit: int = 25,
) -> dict[str, Any]:
    inputs = _load_inputs(reports_dir)
    r2_rows = _r2_rows(inputs)
    fetch_error_rows = [
        row for row in r2_rows if row.get("source_fetch_status") == "FETCH_ERROR"
    ]
    closed_without_outcome_rows = _closed_without_outcome_rows(r2_rows)
    unusable_rows = [
        row
        for row in r2_rows
        if row.get("source_fetch_status") == "SOURCE_SETTLED_WITHOUT_USABLE_OUTCOME"
        and row not in closed_without_outcome_rows
    ]
    freshness = _freshness_reconciliation(inputs)
    summary = {
        "fetch_error_rows": len(fetch_error_rows),
        "source_closed_without_outcome_rows": len(closed_without_outcome_rows),
        "source_settled_without_usable_outcome_rows": len(unusable_rows),
        "source_outcome_blocked_rows": len(closed_without_outcome_rows) + len(unusable_rows),
        "rows_reviewed": len(r2_rows),
        "exact_settlement_rows_now_realizable": 0,
        "sibling_tickers_used_for_settlement": 0,
        "stale_realization_prompt_detected": freshness["stale_realization_prompt_detected"],
        "stale_realization_prompt_fixed_by_r4_logic": freshness[
            "stale_realization_prompt_detected"
        ],
        "phase3aa_eligible_after_realize": freshness["phase3aa_eligible_after_realize"],
        "paper_eligible_to_settle_now": freshness["paper_eligible_to_settle_now"],
        "phase3aa_r3_residue_cleared": freshness["phase3aa_r3_residue_cleared"],
        "recommended_status": _recommended_status(
            fetch_error_rows,
            closed_without_outcome_rows,
            unusable_rows,
            freshness,
        ),
    }
    return {
        "generated_at": utc_now().isoformat(),
        "phase": "3AA-R4",
        "phase_version": PHASE3AA_R4_VERSION,
        "mode": "READ_ONLY_EXACT_SETTLEMENT_FETCH_RECOVERY",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "safety": {
            "live_or_demo_execution": False,
            "exchange_writes": False,
            "paper_pnl_writes": False,
            "exact_ticker_settlement_required": True,
            "sibling_resolution_allowed": False,
        },
        "input_paths": _input_paths(reports_dir),
        "source_availability": {
            key: value is not None for key, value in inputs.items()
        },
        "summary": summary,
        "freshness_reconciliation": freshness,
        "fetch_error_groups": _fetch_error_groups(fetch_error_rows, sample_limit=sample_limit),
        "source_unusable_outcome_groups": _unusable_outcome_groups(
            unusable_rows,
            r3_non_actionable=bool(freshness["phase3aa_r3_residue_cleared"]),
            sample_limit=sample_limit,
        ),
        "source_closed_without_outcome_groups": _unusable_outcome_groups(
            closed_without_outcome_rows,
            r3_non_actionable=bool(freshness["phase3aa_r3_residue_cleared"]),
            sample_limit=sample_limit,
        ),
        "diagnostic_rows": _diagnostic_rows(
            fetch_error_rows,
            closed_without_outcome_rows,
            unusable_rows,
        ),
        "next_commands": _next_commands(summary),
        "recommended_next_action": _recommended_next_action(summary),
    }


def write_phase3aa_r4_settlement_fetch_recovery_report(
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    reports_dir: Path = Path("reports"),
    sample_limit: int = 25,
) -> Phase3AAR4ArtifactSet:
    payload = build_phase3aa_r4_settlement_fetch_recovery(
        reports_dir=reports_dir,
        sample_limit=sample_limit,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "phase3aa_r4_settlement_fetch_recovery.json"
    markdown_path = output_dir / "phase3aa_r4_settlement_fetch_recovery.md"
    rows_path = output_dir / "phase3aa_r4_settlement_fetch_recovery_rows.json"
    json_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    rows_path.write_text(
        json.dumps(payload["diagnostic_rows"], indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    markdown_path.write_text(_render_markdown(payload), encoding="utf-8")
    return Phase3AAR4ArtifactSet(output_dir, json_path, markdown_path, rows_path)


def _load_inputs(reports_dir: Path) -> dict[str, dict[str, Any] | list[Any] | None]:
    paths = _input_paths(reports_dir)
    return {key: _load_json(path) for key, path in paths.items()}


def _input_paths(reports_dir: Path) -> dict[str, Path]:
    return {
        "phase3aa": reports_dir / "phase3aa" / "phase3aa_outcome_realizer.json",
        "phase3aa_r2": reports_dir
        / "phase3aa_r2"
        / "phase3aa_r2_exact_settlement_harvest.json",
        "phase3aa_r2_rows": reports_dir
        / "phase3aa_r2"
        / "phase3aa_r2_exact_settlement_harvest_rows.json",
        "phase3aa_r3": reports_dir
        / "phase3aa_r3"
        / "phase3aa_r3_residual_settlement_audit.json",
        "paper_settlement": reports_dir
        / "paper_settlement_reconciliation"
        / "paper_settlement_reconciliation.json",
        "phase3ah_placeholder_watch": reports_dir
        / "phase3ah_sports"
        / "phase3ah_sports_placeholder_watch.json",
    }


def _load_json(path: Path) -> dict[str, Any] | list[Any] | None:
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, (dict, list)) else None


def _r2_rows(inputs: dict[str, dict[str, Any] | list[Any] | None]) -> list[dict[str, Any]]:
    rows_payload = inputs.get("phase3aa_r2_rows")
    if isinstance(rows_payload, list):
        return [row for row in rows_payload if isinstance(row, dict)]
    r2_payload = inputs.get("phase3aa_r2")
    if isinstance(r2_payload, dict) and isinstance(r2_payload.get("rows"), list):
        return [row for row in r2_payload["rows"] if isinstance(row, dict)]
    return []


def _closed_without_outcome_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        row
        for row in rows
        if row.get("source_fetch_status") == "SOURCE_CLOSED_WITHOUT_OUTCOME"
        or (
            row.get("source_fetch_status") == "SOURCE_SETTLED_WITHOUT_USABLE_OUTCOME"
            and str(row.get("source_status") or "").strip().lower() == "closed"
        )
    ]


def _fetch_error_groups(rows: list[dict[str, Any]], *, sample_limit: int) -> list[dict[str, Any]]:
    groups: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in rows:
        error = str(row.get("error") or "")
        http_status = _http_status(error)
        key = (
            _error_type(error, http_status),
            http_status,
            _ticker_family(row.get("ticker")),
            _single_value(row.get("before_close_time_buckets")),
            _single_value(row.get("before_market_statuses")),
            _retryable(error, http_status),
            _identity_confidence(row),
        )
        group = groups.setdefault(
            key,
            {
                "error_type": key[0],
                "http_status": key[1],
                "ticker_family": key[2],
                "close_time_bucket": key[3],
                "source_or_local_status": key[4],
                "retryable": key[5],
                "exact_ticker_identity_confidence": key[6],
                "count": 0,
                "example_tickers": [],
                "example_errors": [],
            },
        )
        group["count"] += 1
        if len(group["example_tickers"]) < sample_limit:
            group["example_tickers"].append(row.get("ticker"))
        if error and len(group["example_errors"]) < 3:
            group["example_errors"].append(error)
    return sorted(groups.values(), key=lambda group: (-group["count"], group["error_type"]))


def _unusable_outcome_groups(
    rows: list[dict[str, Any]],
    *,
    r3_non_actionable: bool,
    sample_limit: int,
) -> list[dict[str, Any]]:
    groups: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in rows:
        available = _available_outcome_fields(row)
        missing = _missing_outcome_fields(row)
        shape = _outcome_shape(row)
        key = (
            row.get("source_status") or "unknown",
            tuple(available),
            tuple(missing),
            shape,
            r3_non_actionable,
        )
        group = groups.setdefault(
            key,
            {
                "source_status": key[0],
                "available_source_fields": list(key[1]),
                "missing_outcome_fields": list(key[2]),
                "outcome_shape": key[3],
                "phase3aa_r3_already_proved_non_actionable": key[4],
                "count": 0,
                "example_tickers": [],
            },
        )
        group["count"] += 1
        if len(group["example_tickers"]) < sample_limit:
            group["example_tickers"].append(row.get("ticker"))
    return sorted(groups.values(), key=lambda group: (-group["count"], group["source_status"]))


def _freshness_reconciliation(
    inputs: dict[str, dict[str, Any] | list[Any] | None],
) -> dict[str, Any]:
    phase3aa = _dict(inputs.get("phase3aa"))
    r2 = _dict(inputs.get("phase3aa_r2"))
    r3 = _dict(inputs.get("phase3aa_r3"))
    paper = _dict(inputs.get("paper_settlement"))
    watch = _dict(inputs.get("phase3ah_placeholder_watch"))
    phase3aa_summary = (phase3aa.get("eta_schedule") or {}).get("summary") or {}
    r2_summary = _summary_payload(r2)
    r3_summary = _summary_payload(r3)
    paper_summary = _summary_payload(paper)
    watch_summary = _summary_payload(watch)
    watch_settlement = watch.get("settlement_watch") if isinstance(watch, dict) else {}
    eligible_after = int(phase3aa.get("eligible_after_realize") or 0)
    r3_cleared = bool(r3_summary.get("residue_cleared"))
    paper_eligible = int(paper_summary.get("eligible_to_settle_now") or 0)
    stale_prompt = (
        eligible_after == 0
        and r3_cleared
        and paper_eligible == 0
        and (
            str(watch.get("recommended_next_action") or "").startswith("Realize")
            or (isinstance(watch_settlement, dict)
                and watch_settlement.get("status") == "EXACT_SETTLEMENTS_READY_TO_REALIZE")
        )
    )
    return {
        "phase3aa_generated_at": phase3aa.get("generated_at"),
        "phase3aa_eligible_after_realize": eligible_after,
        "phase3aa_due_or_overdue": int(phase3aa_summary.get("due_or_overdue") or 0),
        "phase3aa_active_unsettled": int(phase3aa_summary.get("active_unsettled") or 0),
        "phase3aa_r2_generated_at": r2.get("generated_at"),
        "phase3aa_r2_exact_settlements_written": int(
            r2_summary.get("exact_settlements_written") or 0
        ),
        "phase3aa_r2_fetch_errors": int(r2_summary.get("fetch_errors") or 0),
        "phase3aa_r2_source_settled_without_usable_outcome": int(
            r2_summary.get("source_settled_without_usable_outcome") or 0
        ),
        "phase3aa_r2_source_closed_without_outcome": int(
            r2_summary.get("source_closed_without_outcome") or 0
        ),
        "phase3aa_r3_generated_at": r3.get("generated_at"),
        "phase3aa_r3_residue_cleared": r3_cleared,
        "phase3aa_r3_residual_rows": int(r3_summary.get("residual_rows") or 0),
        "paper_generated_at": paper.get("generated_at"),
        "paper_eligible_to_settle_now": paper_eligible,
        "paper_missing_exact_settlement": int(paper_summary.get("missing_exact_settlement") or 0),
        "paper_sibling_different_contract_leg": int(
            paper_summary.get("sibling_different_contract_leg") or 0
        ),
        "placeholder_watch_generated_at": watch.get("generated_at"),
        "placeholder_watch_settlement_status": watch_settlement.get("status")
        if isinstance(watch_settlement, dict)
        else None,
        "placeholder_watch_recommended_next_action": watch.get("recommended_next_action"),
        "placeholder_watch_r2_exact_settlements_written": int(
            watch_summary.get("settlement_exact_settlements_written") or 0
        ),
        "stale_realization_prompt_detected": stale_prompt,
        "realization_cleared_by_fresher_reports": eligible_after == 0
        and r3_cleared
        and paper_eligible == 0,
    }


def _diagnostic_rows(
    fetch_errors: list[dict[str, Any]],
    closed_without_outcome_rows: list[dict[str, Any]],
    unusable_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in fetch_errors:
        error = str(row.get("error") or "")
        status = _http_status(error)
        rows.append(
            {
                "ticker": row.get("ticker"),
                "diagnostic_type": "FETCH_ERROR",
                "ticker_family": _ticker_family(row.get("ticker")),
                "http_status": status,
                "error_type": _error_type(error, status),
                "retryable": _retryable(error, status),
                "identity_confidence": _identity_confidence(row),
                "close_time_bucket": _single_value(row.get("before_close_time_buckets")),
                "local_market_status": _single_value(row.get("before_market_statuses")),
                "safe_to_realize": False,
                "sibling_resolution_allowed": False,
                "error": error,
            }
        )
    for row in closed_without_outcome_rows:
        rows.append(_source_outcome_diagnostic_row(row, "SOURCE_CLOSED_WITHOUT_OUTCOME"))
    for row in unusable_rows:
        rows.append(
            _source_outcome_diagnostic_row(row, "SOURCE_SETTLED_WITHOUT_USABLE_OUTCOME")
        )
    return rows


def _source_outcome_diagnostic_row(row: dict[str, Any], diagnostic_type: str) -> dict[str, Any]:
    return {
        "ticker": row.get("ticker"),
        "diagnostic_type": diagnostic_type,
        "ticker_family": _ticker_family(row.get("ticker")),
        "source_status": row.get("source_status"),
        "available_source_fields": _available_outcome_fields(row),
        "missing_outcome_fields": _missing_outcome_fields(row),
        "outcome_shape": _outcome_shape(row),
        "safe_to_realize": False,
        "sibling_resolution_allowed": False,
    }


def _recommended_status(
    fetch_errors: list[dict[str, Any]],
    closed_without_outcome_rows: list[dict[str, Any]],
    unusable_rows: list[dict[str, Any]],
    freshness: dict[str, Any],
) -> str:
    if freshness["phase3aa_eligible_after_realize"] or freshness["paper_eligible_to_settle_now"]:
        return "REALIZATION_READY"
    if closed_without_outcome_rows:
        return "CLOSED_MARKET_OUTCOME_FIELDS_BLOCKING_EXACT_SETTLEMENTS"
    if fetch_errors:
        return "FETCH_ERRORS_BLOCKING_EXACT_SETTLEMENTS"
    if unusable_rows:
        return "SOURCE_OUTCOME_FIELDS_BLOCKING_EXACT_SETTLEMENTS"
    return "WATCHING_EXACT_SETTLEMENTS"


def _next_commands(summary: dict[str, Any]) -> list[str]:
    commands = [
        "kalshi-bot phase3aa-r4-settlement-fetch-recovery --output-dir reports/phase3aa_r4",
        "kalshi-bot phase3aa-r5-closed-market-outcome-capture --output-dir reports/phase3aa_r5",
        "kalshi-bot phase3aa-r2-exact-settlement-harvest --output-dir reports/phase3aa_r2",
        "kalshi-bot paper-settlement-doctor --output-dir reports/paper_settlement_reconciliation",
        "kalshi-bot phase3aa-r3-residual-settlement-audit --output-dir reports/phase3aa_r3",
    ]
    if int(summary.get("exact_settlement_rows_now_realizable") or 0) > 0:
        commands.append("kalshi-bot phase3aa-realize --no-dry-run --no-sync-settlements")
    commands.extend(
        [
            "kalshi-bot phase3ah-sports-placeholder-watch --output-dir reports/phase3ah_sports",
            "kalshi-bot phase3az-gap-analysis --output-dir reports/phase3az --reports-dir reports",
            (
                "kalshi-bot phase-orchestrator --analyze --output reports/phase_orchestrator.md "
                "--json-output reports/phase_orchestrator.json --next-prompt prompts/next_phase.md"
            ),
        ]
    )
    return commands


def _recommended_next_action(summary: dict[str, Any]) -> str:
    if int(summary.get("exact_settlement_rows_now_realizable") or 0) > 0:
        return "Run Phase 3AA realizer using exact ticker settlement evidence only."
    if int(summary.get("source_closed_without_outcome_rows") or 0) > 0:
        return (
            "Run Phase 3AA-R5 to capture closed exact-market source fields. Keep these "
            "rows blocked unless an exact usable outcome field is proven."
        )
    if int(summary.get("fetch_error_rows") or 0) > 0:
        return (
            "Retry exact ticker harvesting and inspect the grouped fetch errors; do not "
            "use sibling markets for settlement."
        )
    if int(summary.get("source_settled_without_usable_outcome_rows") or 0) > 0:
        return (
            "Keep these rows diagnostic-only until the exact source exposes a supported "
            "binary/scalar outcome."
        )
    return "No immediate settlement action is available; keep the exact-ticker watch active."


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _summary_payload(value: Any) -> dict[str, Any]:
    payload = _dict(value)
    summary = payload.get("summary")
    return summary if isinstance(summary, dict) else {}


def _http_status(error: str) -> int | None:
    match = re.search(r"HTTP\s+(\d{3})", error)
    return int(match.group(1)) if match else None


def _error_type(error: str, http_status: int | None) -> str:
    lowered = error.lower()
    if http_status == 404 or "not_found" in lowered or "not found" in lowered:
        return "HTTP_404_NOT_FOUND"
    if http_status == 429:
        return "HTTP_429_RATE_LIMIT"
    if http_status and http_status >= 500:
        return "HTTP_5XX_SERVER_ERROR"
    if "timeout" in lowered:
        return "TIMEOUT"
    if "connection" in lowered:
        return "CONNECTION_ERROR"
    if http_status:
        return f"HTTP_{http_status}"
    return "UNKNOWN_FETCH_ERROR"


def _retryable(error: str, http_status: int | None) -> bool:
    lowered = error.lower()
    if http_status in {408, 409, 425, 429}:
        return True
    if http_status is not None:
        return http_status >= 500
    return any(token in lowered for token in ("timeout", "connection", "retry"))


def _identity_confidence(row: dict[str, Any]) -> str:
    if row.get("identity_match") is True:
        return "EXACT_TICKER_CONFIRMED"
    if row.get("identity_match") is False:
        return "IDENTITY_MISMATCH"
    if row.get("source_fetch_status") == "FETCH_ERROR":
        return "EXACT_TICKER_REQUESTED_NO_RESPONSE"
    return "UNKNOWN"


def _available_outcome_fields(row: dict[str, Any]) -> list[str]:
    fields = []
    for key in (
        "source_result",
        "source_settlement_value_dollars",
        "source_settlement_value",
        "source_yes_settlement_value",
        "source_expiration_value",
        "source_settlement_ts",
    ):
        if row.get(key) not in {None, ""}:
            fields.append(key)
    return fields


def _missing_outcome_fields(row: dict[str, Any]) -> list[str]:
    fields = []
    if not row.get("source_result"):
        fields.append("source_result")
    if (
        row.get("source_settlement_value_dollars") in {None, ""}
        and row.get("source_settlement_value") in {None, ""}
        and row.get("source_yes_settlement_value") in {None, ""}
    ):
        fields.append("settlement_value")
    return fields


def _outcome_shape(row: dict[str, Any]) -> str:
    result = str(row.get("source_result") or "").strip().lower()
    value = row.get("source_settlement_value_dollars") or row.get(
        "source_settlement_value"
    ) or row.get(
        "source_yes_settlement_value"
    )
    if result in {"yes", "no"}:
        return "BINARY_RESULT"
    if value not in {None, ""}:
        try:
            number = float(str(value))
        except ValueError:
            return "UNSUPPORTED_VALUE"
        if number in {0.0, 1.0}:
            return "BINARY_VALUE"
        return "SCALAR_VALUE"
    if result in {"void", "cancelled", "canceled"}:
        return "VOID_OR_CANCELLED"
    return "MISSING_OUTCOME"


def _ticker_family(ticker: Any) -> str:
    value = str(ticker or "")
    if "-S" in value:
        return value.split("-S", 1)[0]
    if "-" in value:
        return value.split("-", 1)[0]
    return value or "UNKNOWN"


def _single_value(value: Any) -> str:
    if isinstance(value, list) and value:
        return str(value[0])
    if isinstance(value, list):
        return "unknown"
    return str(value or "unknown")


def _render_markdown(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    freshness = payload["freshness_reconciliation"]
    lines = [
        "# Phase 3AA-R4 Exact Settlement Fetch Recovery",
        "",
        f"- Generated at: {payload['generated_at']}",
        f"- Mode: {payload['mode']}",
        f"- Safety: {payload['paper_only_safety']}",
        "- Live/demo execution: false",
        "- Paper P&L writes: false",
        "- Settlement policy: exact ticker only",
        "",
        "## Summary",
        "",
    ]
    for key, value in summary.items():
        lines.append(f"- {key}: {value}")
    lines.extend(["", "## Freshness Reconciliation", ""])
    for key, value in freshness.items():
        lines.append(f"- {key}: {value}")
    lines.extend(
        [
            "",
            "## Fetch Error Groups",
            "",
            "| Error | HTTP | Family | Bucket | Status | Retryable | Identity | Count | Examples |",
            "| --- | ---: | --- | --- | --- | --- | --- | ---: | --- |",
        ]
    )
    for group in payload["fetch_error_groups"][:50]:
        lines.append(
            f"| {_md(group['error_type'])} | {_md(group['http_status'])} | "
            f"{_md(group['ticker_family'])} | {_md(group['close_time_bucket'])} | "
            f"{_md(group['source_or_local_status'])} | {group['retryable']} | "
            f"{_md(group['exact_ticker_identity_confidence'])} | {group['count']} | "
            f"{_md(', '.join(str(item) for item in group['example_tickers'][:5]))} |"
        )
    lines.extend(
        [
            "",
            "## Closed Source Without Usable Outcome",
            "",
            "| Source status | Shape | R3 non-actionable | Count | Missing fields | Examples |",
            "| --- | --- | --- | ---: | --- | --- |",
        ]
    )
    for group in payload["source_closed_without_outcome_groups"][:50]:
        lines.append(
            f"| {_md(group['source_status'])} | {_md(group['outcome_shape'])} | "
            f"{group['phase3aa_r3_already_proved_non_actionable']} | {group['count']} | "
            f"{_md(', '.join(group['missing_outcome_fields']))} | "
            f"{_md(', '.join(str(item) for item in group['example_tickers'][:5]))} |"
        )
    lines.extend(
        [
            "",
            "## Source Settled Without Usable Outcome",
            "",
            "| Source status | Shape | R3 non-actionable | Count | Missing fields | Examples |",
            "| --- | --- | --- | ---: | --- | --- |",
        ]
    )
    for group in payload["source_unusable_outcome_groups"][:50]:
        lines.append(
            f"| {_md(group['source_status'])} | {_md(group['outcome_shape'])} | "
            f"{group['phase3aa_r3_already_proved_non_actionable']} | {group['count']} | "
            f"{_md(', '.join(group['missing_outcome_fields']))} | "
            f"{_md(', '.join(str(item) for item in group['example_tickers'][:5]))} |"
        )
    lines.extend(["", "## Next Commands", ""])
    for command in payload["next_commands"]:
        lines.append(f"- `{command}`")
    lines.extend(["", "## Recommended Next Action", "", payload["recommended_next_action"], ""])
    return "\n".join(lines)


def _md(value: Any) -> str:
    if value is None:
        return ""
    return str(value).replace("|", "\\|").replace("\n", " ")
