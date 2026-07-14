from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from kalshi_predictor.data.repositories import decode_json
from kalshi_predictor.data.schema import Market
from kalshi_predictor.paper.settlement_reconciliation import PAPER_ONLY_SAFETY
from kalshi_predictor.utils.decimals import decimal_to_str
from kalshi_predictor.utils.time import utc_now

PHASE3AA_R5_VERSION = "phase3aa_r5_v1"
DEFAULT_OUTPUT_DIR = Path("reports/phase3aa_r5")
SOURCE_CLOSED_STATUSES = {"closed"}
SOURCE_SETTLED_STATUSES = {"settled", "resolved", "finalized"}
KNOWN_OUTCOME_FIELDS = (
    "result",
    "settlement_value_dollars",
    "settlement_value",
    "yes_settlement_value",
)
TIMESTAMP_OUTCOME_FIELDS = ("settlement_ts", "settled_time", "settled_at")
NONSTANDARD_CANDIDATE_FIELDS = ("expiration_value",)


@dataclass(frozen=True)
class Phase3AAR5ArtifactSet:
    output_dir: Path
    json_path: Path
    markdown_path: Path
    rows_path: Path


def build_phase3aa_r5_closed_market_outcome_capture(
    session: Session,
    *,
    reports_dir: Path = Path("reports"),
    sample_limit: int = 25,
) -> dict[str, Any]:
    session.flush()
    r2_rows = _load_r2_rows(reports_dir)
    candidates = _candidate_rows(r2_rows)
    rows = [_capture_row(session, row) for row in candidates]
    summary = _summary(rows, r2_rows)
    return {
        "generated_at": utc_now().isoformat(),
        "phase": "3AA-R5",
        "phase_version": PHASE3AA_R5_VERSION,
        "mode": "READ_ONLY_CLOSED_MARKET_OUTCOME_FIELD_CAPTURE",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "safety": {
            "live_or_demo_execution": False,
            "exchange_writes": False,
            "paper_pnl_writes": False,
            "exact_ticker_settlement_required": True,
            "sibling_resolution_allowed": False,
        },
        "input_paths": _input_paths(reports_dir),
        "summary": summary,
        "classification_counts": _counts(rows, "classification"),
        "source_status_counts": _counts(rows, "source_status"),
        "outcome_field_presence_counts": _outcome_field_presence_counts(rows),
        "groups": _groups(rows, sample_limit=sample_limit),
        "rows": rows,
        "next_commands": _next_commands(summary),
        "recommended_next_action": _recommended_next_action(summary),
    }


def write_phase3aa_r5_closed_market_outcome_capture_report(
    session: Session,
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    reports_dir: Path = Path("reports"),
    sample_limit: int = 25,
) -> Phase3AAR5ArtifactSet:
    payload = build_phase3aa_r5_closed_market_outcome_capture(
        session,
        reports_dir=reports_dir,
        sample_limit=sample_limit,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "phase3aa_r5_closed_market_outcome_capture.json"
    markdown_path = output_dir / "phase3aa_r5_closed_market_outcome_capture.md"
    rows_path = output_dir / "phase3aa_r5_closed_market_outcome_capture_rows.json"
    json_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    rows_path.write_text(
        json.dumps(payload["rows"], indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    markdown_path.write_text(_render_markdown(payload), encoding="utf-8")
    return Phase3AAR5ArtifactSet(output_dir, json_path, markdown_path, rows_path)


def _input_paths(reports_dir: Path) -> dict[str, Path]:
    return {
        "phase3aa_r2_rows": reports_dir
        / "phase3aa_r2"
        / "phase3aa_r2_exact_settlement_harvest_rows.json",
        "phase3aa_r2": reports_dir
        / "phase3aa_r2"
        / "phase3aa_r2_exact_settlement_harvest.json",
    }


def _load_r2_rows(reports_dir: Path) -> list[dict[str, Any]]:
    rows_path = _input_paths(reports_dir)["phase3aa_r2_rows"]
    if rows_path.exists():
        payload = json.loads(rows_path.read_text(encoding="utf-8"))
        if isinstance(payload, list):
            return [row for row in payload if isinstance(row, dict)]
    report_path = _input_paths(reports_dir)["phase3aa_r2"]
    if report_path.exists():
        payload = json.loads(report_path.read_text(encoding="utf-8"))
        if isinstance(payload, dict) and isinstance(payload.get("rows"), list):
            return [row for row in payload["rows"] if isinstance(row, dict)]
    return []


def _candidate_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    candidates = []
    for row in rows:
        fetch_status = str(row.get("source_fetch_status") or "")
        source_status = str(row.get("source_status") or "").strip().lower()
        if fetch_status in {
            "SOURCE_CLOSED_WITHOUT_OUTCOME",
            "SOURCE_SETTLED_WITHOUT_USABLE_OUTCOME",
        }:
            candidates.append(row)
            continue
        if source_status in SOURCE_CLOSED_STATUSES and not _row_has_known_outcome(row):
            candidates.append(row)
    return candidates


def _capture_row(session: Session, r2_row: dict[str, Any]) -> dict[str, Any]:
    ticker = str(r2_row.get("ticker") or "")
    market = session.get(Market, ticker) if ticker else None
    payload = decode_json(market.raw_json) if market is not None else {}
    if not payload:
        payload = _payload_from_r2_row(r2_row)
    source_status = str(payload.get("status") or r2_row.get("source_status") or "").strip()
    custom_strike = payload.get("custom_strike")
    outcome_candidates = _outcome_candidates(payload)
    classification = _classification(payload, source_status)
    safe_parser_candidate = classification == "EXACT_OUTCOME_FIELD_USABLE"
    return {
        "ticker": ticker,
        "r2_source_fetch_status": r2_row.get("source_fetch_status"),
        "source_status": source_status or None,
        "source_payload_found_in_db": market is not None,
        "classification": classification,
        "safe_to_write_exact_settlement_from_current_parser": safe_parser_candidate,
        "paper_pnl_realization_allowed": False,
        "sibling_resolution_allowed": False,
        "source_result": _empty_to_none(payload.get("result")),
        "source_settlement_value_dollars": _empty_to_none(
            payload.get("settlement_value_dollars")
        ),
        "source_settlement_value": _empty_to_none(payload.get("settlement_value")),
        "source_yes_settlement_value": _empty_to_none(payload.get("yes_settlement_value")),
        "source_expiration_value": _empty_to_none(payload.get("expiration_value")),
        "source_settlement_ts": _first_present(payload, *TIMESTAMP_OUTCOME_FIELDS),
        "source_close_time": payload.get("close_time"),
        "source_expected_expiration_time": payload.get("expected_expiration_time"),
        "source_latest_expiration_time": payload.get("latest_expiration_time"),
        "source_settlement_timer_seconds": payload.get("settlement_timer_seconds"),
        "market_type": payload.get("market_type"),
        "event_ticker": payload.get("event_ticker"),
        "series_ticker": payload.get("series_ticker"),
        "title": payload.get("title"),
        "outcome_field_candidates": outcome_candidates,
        "known_outcome_field_present": any(
            candidate["present"] for candidate in outcome_candidates if candidate["known_field"]
        ),
        "nonstandard_outcome_candidate_present": any(
            candidate["present"]
            for candidate in outcome_candidates
            if candidate["nonstandard_candidate"]
        ),
        "custom_strike_keys": _custom_strike_keys(custom_strike),
        "associated_events_count": _nested_count(custom_strike, "associated_events"),
        "associated_markets_count": _nested_count(custom_strike, "associated_markets"),
        "associated_market_sides_count": _nested_count(
            custom_strike,
            "associated_market_sides",
        ),
        "mve_selected_legs_count": _nested_count(payload, "mve_selected_legs"),
        "payload_keys": sorted(str(key) for key in payload.keys()),
        "blocked_reason": _blocked_reason(classification),
        "next_action": _row_next_action(classification),
    }


def _payload_from_r2_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "ticker": row.get("source_ticker") or row.get("ticker"),
        "status": row.get("source_status"),
        "result": row.get("source_result"),
        "settlement_value_dollars": row.get("source_settlement_value_dollars"),
        "settlement_value": row.get("source_settlement_value"),
        "yes_settlement_value": row.get("source_yes_settlement_value"),
        "expiration_value": row.get("source_expiration_value"),
        "settlement_ts": row.get("source_settlement_ts"),
        "close_time": row.get("source_close_time"),
        "expected_expiration_time": row.get("source_expected_expiration_time"),
        "latest_expiration_time": row.get("source_latest_expiration_time"),
        "settlement_timer_seconds": row.get("source_settlement_timer_seconds"),
        "event_ticker": row.get("source_event_ticker"),
        "series_ticker": row.get("source_series_ticker"),
        "title": row.get("source_title"),
    }


def _outcome_candidates(payload: dict[str, Any]) -> list[dict[str, Any]]:
    fields = (*KNOWN_OUTCOME_FIELDS, *TIMESTAMP_OUTCOME_FIELDS, *NONSTANDARD_CANDIDATE_FIELDS)
    candidates = []
    for field in fields:
        value = _empty_to_none(payload.get(field))
        candidates.append(
            {
                "field": field,
                "value": value,
                "present": value is not None,
                "known_field": field in KNOWN_OUTCOME_FIELDS,
                "timestamp_field": field in TIMESTAMP_OUTCOME_FIELDS,
                "nonstandard_candidate": field in NONSTANDARD_CANDIDATE_FIELDS,
                "usable_for_exact_settlement": _field_is_usable(field, value),
            }
        )
    return candidates


def _field_is_usable(field: str, value: Any) -> bool:
    if value is None:
        return False
    if field == "result":
        return str(value).strip().lower() in {"yes", "y", "no", "n", "1", "0", "true", "false"}
    if field in {"settlement_value_dollars", "settlement_value", "yes_settlement_value"}:
        return decimal_to_str(value) is not None
    return False


def _classification(payload: dict[str, Any], source_status: str) -> str:
    if any(candidate["usable_for_exact_settlement"] for candidate in _outcome_candidates(payload)):
        return "EXACT_OUTCOME_FIELD_USABLE"
    normalized_status = source_status.strip().lower()
    if normalized_status in SOURCE_CLOSED_STATUSES:
        return "SOURCE_CLOSED_WITHOUT_OUTCOME"
    if normalized_status in SOURCE_SETTLED_STATUSES or _first_present(
        payload,
        *TIMESTAMP_OUTCOME_FIELDS,
    ):
        return "SOURCE_SETTLED_WITHOUT_USABLE_OUTCOME"
    if normalized_status:
        return "SOURCE_NOT_SETTLED"
    return "SOURCE_STATUS_UNKNOWN"


def _summary(rows: list[dict[str, Any]], r2_rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "r2_rows_reviewed": len(r2_rows),
        "rows_reviewed": len(rows),
        "closed_without_outcome_rows": sum(
            1 for row in rows if row["classification"] == "SOURCE_CLOSED_WITHOUT_OUTCOME"
        ),
        "source_settled_without_usable_outcome_rows": sum(
            1
            for row in rows
            if row["classification"] == "SOURCE_SETTLED_WITHOUT_USABLE_OUTCOME"
        ),
        "usable_outcome_candidate_rows": sum(
            1 for row in rows if row["safe_to_write_exact_settlement_from_current_parser"]
        ),
        "safe_to_settle_rows": 0,
        "paper_pnl_realization_allowed_rows": 0,
        "empty_result_rows": sum(1 for row in rows if row.get("source_result") is None),
        "empty_expiration_value_rows": sum(
            1 for row in rows if row.get("source_expiration_value") is None
        ),
        "custom_strike_rows": sum(1 for row in rows if row["custom_strike_keys"]),
        "associated_market_rows": sum(
            1 for row in rows if int(row["associated_markets_count"] or 0) > 0
        ),
        "associated_event_rows": sum(
            1 for row in rows if int(row["associated_events_count"] or 0) > 0
        ),
        "nonstandard_outcome_candidate_rows": sum(
            1 for row in rows if row["nonstandard_outcome_candidate_present"]
        ),
        "fetch_error_rows_ignored": sum(
            1 for row in r2_rows if row.get("source_fetch_status") == "FETCH_ERROR"
        ),
        "parser_fix_applied": True,
        "closed_is_not_settled_policy": True,
    }


def _counts(rows: list[dict[str, Any]], key: str) -> dict[str, int]:
    counts = Counter(str(row.get(key) or "unknown") for row in rows)
    return dict(sorted(counts.items()))


def _outcome_field_presence_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for row in rows:
        for candidate in row["outcome_field_candidates"]:
            if candidate["present"]:
                counts[candidate["field"]] += 1
    return dict(sorted(counts.items()))


def _groups(rows: list[dict[str, Any]], *, sample_limit: int) -> list[dict[str, Any]]:
    grouped: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in rows:
        key = (
            row.get("classification"),
            row.get("source_status"),
            row.get("market_type") or "unknown",
            bool(row.get("custom_strike_keys")),
            bool(row.get("nonstandard_outcome_candidate_present")),
        )
        group = grouped.setdefault(
            key,
            {
                "classification": key[0],
                "source_status": key[1],
                "market_type": key[2],
                "has_custom_strike": key[3],
                "has_nonstandard_outcome_candidate": key[4],
                "count": 0,
                "example_tickers": [],
            },
        )
        group["count"] += 1
        if len(group["example_tickers"]) < sample_limit:
            group["example_tickers"].append(row.get("ticker"))
    return sorted(grouped.values(), key=lambda group: (-group["count"], group["classification"]))


def _next_commands(summary: dict[str, Any]) -> list[str]:
    commands = [
        "kalshi-bot phase3aa-r5-closed-market-outcome-capture --output-dir reports/phase3aa_r5",
        "kalshi-bot phase3aa-r2-exact-settlement-harvest --output-dir reports/phase3aa_r2",
        "kalshi-bot phase3aa-r4-settlement-fetch-recovery --output-dir reports/phase3aa_r4",
        "kalshi-bot paper-settlement-doctor --output-dir reports/paper_settlement_reconciliation",
    ]
    if int(summary.get("usable_outcome_candidate_rows") or 0) > 0:
        commands.append("kalshi-bot phase3aa-realize --dry-run --no-sync-settlements")
    commands.append(
        "kalshi-bot phase-orchestrator --analyze --output reports/phase_orchestrator.md "
        "--json-output reports/phase_orchestrator.json --next-prompt prompts/next_phase.md"
    )
    return commands


def _recommended_next_action(summary: dict[str, Any]) -> str:
    if int(summary.get("usable_outcome_candidate_rows") or 0) > 0:
        return (
            "Rerun the exact settlement harvest so the parser can write exact settlement "
            "rows, then dry-run Phase 3AA realization."
        )
    if int(summary.get("closed_without_outcome_rows") or 0) > 0:
        return (
            "Kalshi returned closed exact-market payloads but no supported outcome field. "
            "Keep these rows blocked and continue exact-ticker settlement watching."
        )
    return "No closed-market outcome parser repair is currently actionable."


def _row_has_known_outcome(row: dict[str, Any]) -> bool:
    return any(row.get(key) not in {None, ""} for key in _r2_known_outcome_keys())


def _r2_known_outcome_keys() -> tuple[str, ...]:
    return (
        "source_result",
        "source_settlement_value_dollars",
        "source_settlement_value",
        "source_yes_settlement_value",
    )


def _blocked_reason(classification: str) -> str | None:
    if classification == "EXACT_OUTCOME_FIELD_USABLE":
        return None
    if classification == "SOURCE_CLOSED_WITHOUT_OUTCOME":
        return "Exact source status is closed, but no supported outcome field is exposed."
    if classification == "SOURCE_SETTLED_WITHOUT_USABLE_OUTCOME":
        return "Exact source appears settled, but the returned outcome shape is unsupported."
    return "Exact source is not settled or its settlement state is unknown."


def _row_next_action(classification: str) -> str:
    if classification == "EXACT_OUTCOME_FIELD_USABLE":
        return "Rerun Phase 3AA-R2 exact settlement harvest; do not realize P&L here."
    if classification == "SOURCE_CLOSED_WITHOUT_OUTCOME":
        return "Keep blocked until the exact ticker exposes result or settlement value."
    if classification == "SOURCE_SETTLED_WITHOUT_USABLE_OUTCOME":
        return "Keep diagnostic-only until parser support is proven for this outcome shape."
    return "Keep exact-ticker watch active."


def _custom_strike_keys(value: Any) -> list[str]:
    if not isinstance(value, dict):
        return []
    return sorted(str(key) for key in value.keys())


def _nested_count(value: Any, key: str) -> int:
    if not isinstance(value, dict):
        return 0
    child = value.get(key)
    if isinstance(child, list):
        return len(child)
    if isinstance(child, dict):
        return len(child)
    return 0


def _first_present(payload: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = _empty_to_none(payload.get(key))
        if value is not None:
            return value
    return None


def _empty_to_none(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    return value


def _render_markdown(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    lines = [
        "# Phase 3AA-R5 Closed Market Outcome Capture",
        "",
        f"- Generated at: {payload['generated_at']}",
        f"- Mode: {payload['mode']}",
        f"- Safety: {payload['paper_only_safety']}",
        "- Live/demo execution: false",
        "- Paper P&L writes: false",
        "- Settlement policy: exact ticker only",
        "- Closed-market policy: closed is not treated as settled without a supported outcome",
        "",
        "## Summary",
        "",
    ]
    for key, value in summary.items():
        lines.append(f"- {key}: {value}")
    lines.extend(
        [
            "",
            "## Outcome Field Presence",
            "",
        ]
    )
    for key, value in payload["outcome_field_presence_counts"].items():
        lines.append(f"- {key}: {value}")
    lines.extend(
        [
            "",
            "## Groups",
            "",
            "| Classification | Status | Market type | Custom strike | "
            "Nonstandard candidate | Count | Examples |",
            "| --- | --- | --- | --- | --- | ---: | --- |",
        ]
    )
    for group in payload["groups"][:50]:
        examples = ", ".join(str(item) for item in group["example_tickers"][:5])
        lines.append(
            f"| {_md(group['classification'])} | {_md(group['source_status'])} | "
            f"{_md(group['market_type'])} | {group['has_custom_strike']} | "
            f"{group['has_nonstandard_outcome_candidate']} | {group['count']} | "
            f"{_md(examples)} |"
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
