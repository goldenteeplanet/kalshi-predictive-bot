from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from kalshi_predictor.data.schema import PaperOrder, PaperPnl, PaperPosition, Settlement
from kalshi_predictor.paper.models import ORDER_FILLED
from kalshi_predictor.paper.settlement_reconciliation import (
    PAPER_ONLY_SAFETY,
    build_paper_settlement_reconciliation,
)
from kalshi_predictor.utils.time import utc_now

PHASE_3AA_R3_VERSION = "phase3aa_r3_v1"
REALIZED_PNL_NOTE = "settled market realized paper p&l"


@dataclass(frozen=True)
class Phase3AAR3ArtifactSet:
    output_dir: Path
    json_path: Path
    markdown_path: Path
    rows_path: Path


def write_phase3aa_r3_residual_audit_report(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3aa_r3"),
    limit: int | None = None,
) -> Phase3AAR3ArtifactSet:
    payload = build_phase3aa_r3_residual_audit(session, limit=limit)
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "phase3aa_r3_residual_settlement_audit.json"
    markdown_path = output_dir / "phase3aa_r3_residual_settlement_audit.md"
    rows_path = output_dir / "phase3aa_r3_residual_rows.json"
    json_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    rows_path.write_text(
        json.dumps(payload["rows"], indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    markdown_path.write_text(_render_markdown(payload), encoding="utf-8")
    return Phase3AAR3ArtifactSet(output_dir, json_path, markdown_path, rows_path)


def build_phase3aa_r3_residual_audit(
    session: Session,
    *,
    limit: int | None = None,
) -> dict[str, Any]:
    session.flush()
    reconciliation = build_paper_settlement_reconciliation(session, limit=limit)
    candidate_rows = [
        row
        for row in reconciliation["rows"]
        if row.get("eligible_to_settle_now") or row.get("reason") == "SETTLEMENT_RESULT_UNUSABLE"
    ]
    rows = [_audit_row(session, row) for row in candidate_rows]
    summary = _summary(rows, reconciliation)
    return {
        "generated_at": utc_now().isoformat(),
        "phase": "3AA-R3",
        "phase_version": PHASE_3AA_R3_VERSION,
        "mode": "READ_ONLY_RESIDUAL_EXACT_SETTLEMENT_AUDIT",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "safety": {
            "live_or_demo_execution": False,
            "exchange_writes": False,
            "paper_pnl_writes": False,
            "exact_ticker_settlement_required": True,
            "sibling_resolution_allowed": False,
        },
        "limit": limit,
        "source_reconciliation_summary": reconciliation["summary"],
        "summary": summary,
        "classification_counts": _classification_counts(rows),
        "rows": rows,
        "next_commands": _next_commands(summary),
        "recommended_next_action": _recommended_next_action(summary),
    }


def _audit_row(session: Session, row: dict[str, Any]) -> dict[str, Any]:
    ticker = str(row["ticker"])
    settlement = session.get(Settlement, ticker)
    position = session.get(PaperPosition, ticker)
    pnl_rows = _pnl_rows(session, ticker)
    order_count = _filled_order_count(session, ticker)
    classification = _classify_residual(
        row=row,
        settlement=settlement,
        position=position,
        pnl_rows=pnl_rows,
    )
    return {
        "paper_order_id": row["paper_order_id"],
        "ticker": ticker,
        "side": row.get("side"),
        "doctor_reason": row.get("reason"),
        "classification": classification,
        "classification_explanation": _classification_explanation(classification),
        "exact_settlement_found": settlement is not None,
        "settlement_result": settlement.result if settlement is not None else None,
        "yes_settlement_value": settlement.yes_settlement_value
        if settlement is not None
        else None,
        "source_outcome_supported_by_current_realizer": _source_supported_by_realizer(settlement),
        "has_paper_position": position is not None,
        "position_contracts": _position_contracts(position),
        "position_realized_pnl": position.realized_pnl if position is not None else None,
        "filled_orders_for_ticker": order_count,
        "duplicate_filled_order_for_ticker": order_count > 1,
        "latest_pnl": _pnl_summary(pnl_rows[0]) if pnl_rows else None,
        "matching_realized_pnl": _pnl_summary(
            _matching_realized_pnl(pnl_rows=pnl_rows, settlement=settlement)
        ),
        "safe_repair_action": _safe_repair_action(classification),
        "live_or_demo_execution": False,
    }


def _classify_residual(
    *,
    row: dict[str, Any],
    settlement: Settlement | None,
    position: PaperPosition | None,
    pnl_rows: list[PaperPnl],
) -> str:
    if _matching_realized_pnl(pnl_rows=pnl_rows, settlement=settlement) is not None:
        return "ALREADY_REALIZED_JOIN_MISS"
    if settlement is None:
        return "EXACT_SETTLEMENT_MISSING"
    if not _source_supported_by_realizer(settlement):
        return "SOURCE_OUTCOME_UNSUPPORTED"
    if position is None:
        return "MISSING_PAPER_POSITION"
    if position.yes_contracts == 0 and position.no_contracts == 0:
        return "ZERO_CONTRACT_POSITION"
    if _is_scalar_settlement(settlement):
        return "SCALAR_READY_FOR_REALIZER"
    if row.get("eligible_to_settle_now"):
        return "BINARY_READY_FOR_REALIZER"
    return str(row.get("reason") or "UNKNOWN_RESIDUAL")


def _pnl_rows(session: Session, ticker: str) -> list[PaperPnl]:
    return list(
        session.scalars(
            select(PaperPnl)
            .where(PaperPnl.ticker == ticker)
            .order_by(desc(PaperPnl.calculated_at), desc(PaperPnl.id))
        )
    )


def _filled_order_count(session: Session, ticker: str) -> int:
    return int(
        session.scalar(
            select(func.count())
            .select_from(PaperOrder)
            .where(PaperOrder.ticker == ticker, PaperOrder.status == ORDER_FILLED)
        )
        or 0
    )


def _matching_realized_pnl(
    *,
    pnl_rows: list[PaperPnl],
    settlement: Settlement | None,
) -> PaperPnl | None:
    settlement_result = _normalized_settlement_result(settlement)
    if settlement_result is None:
        return None
    for pnl in pnl_rows:
        if (pnl.notes or "").strip().lower() != REALIZED_PNL_NOTE:
            continue
        if _normalize_result(pnl.settlement_result) == settlement_result:
            return pnl
    return None


def _source_supported_by_realizer(settlement: Settlement | None) -> bool:
    if settlement is None:
        return False
    return _yes_settlement_value(settlement) is not None or _normalize_result(
        settlement.result
    ) in {"yes", "no"}


def _is_scalar_settlement(settlement: Settlement | None) -> bool:
    if settlement is None:
        return False
    value = _yes_settlement_value(settlement)
    if value is None:
        return False
    return value not in {Decimal("0"), Decimal("1")}


def _yes_settlement_value(settlement: Settlement) -> Decimal | None:
    value = _decimal_or_none(settlement.yes_settlement_value)
    if value is not None and Decimal("0") <= value <= Decimal("1"):
        return value
    normalized = _normalize_result(settlement.result)
    if normalized == "yes":
        return Decimal("1")
    if normalized == "no":
        return Decimal("0")
    return None


def _normalized_settlement_result(settlement: Settlement | None) -> str | None:
    if settlement is None:
        return None
    normalized = _normalize_result(settlement.result)
    if normalized in {"yes", "no"}:
        return normalized
    value = _yes_settlement_value(settlement)
    if value == Decimal("1"):
        return "yes"
    if value == Decimal("0"):
        return "no"
    if value is not None:
        return normalized or "scalar"
    return normalized


def _normalize_result(value: object) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip().lower()
    if normalized in {"yes", "y", "1", "true"}:
        return "yes"
    if normalized in {"no", "n", "0", "false"}:
        return "no"
    return normalized or None


def _decimal_or_none(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _position_contracts(position: PaperPosition | None) -> dict[str, int] | None:
    if position is None:
        return None
    return {"yes": position.yes_contracts, "no": position.no_contracts}


def _pnl_summary(row: PaperPnl | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "id": row.id,
        "calculated_at": row.calculated_at.isoformat() if row.calculated_at else None,
        "settlement_result": row.settlement_result,
        "realized_pnl": row.realized_pnl,
        "unrealized_pnl": row.unrealized_pnl,
        "total_pnl": row.total_pnl,
        "notes": row.notes,
    }


def _summary(rows: list[dict[str, Any]], reconciliation: dict[str, Any]) -> dict[str, Any]:
    counts = _classification_counts(rows)
    ready = counts.get("BINARY_READY_FOR_REALIZER", 0) + counts.get(
        "SCALAR_READY_FOR_REALIZER",
        0,
    )
    return {
        "residual_rows": len(rows),
        "eligible_to_settle_now": reconciliation["summary"]["eligible_to_settle_now"],
        "ready_for_realizer_rows": ready,
        "scalar_ready_for_realizer_rows": counts.get("SCALAR_READY_FOR_REALIZER", 0),
        "binary_ready_for_realizer_rows": counts.get("BINARY_READY_FOR_REALIZER", 0),
        "already_realized_join_miss_rows": counts.get("ALREADY_REALIZED_JOIN_MISS", 0),
        "missing_position_rows": counts.get("MISSING_PAPER_POSITION", 0),
        "zero_contract_position_rows": counts.get("ZERO_CONTRACT_POSITION", 0),
        "unsupported_source_outcome_rows": counts.get("SOURCE_OUTCOME_UNSUPPORTED", 0),
        "duplicate_ticker_rows": sum(
            1 for row in rows if row["duplicate_filled_order_for_ticker"]
        ),
        "safe_to_run_phase3aa_realize": ready > 0,
        "residue_cleared": len(rows) == 0,
    }


def _classification_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        classification = str(row["classification"])
        counts[classification] = counts.get(classification, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def _classification_explanation(classification: str) -> str:
    explanations = {
        "SCALAR_READY_FOR_REALIZER": (
            "Exact ticker settlement has a supported scalar yes_settlement_value and a "
            "paper position exists."
        ),
        "BINARY_READY_FOR_REALIZER": (
            "Exact ticker binary settlement and a paper position exist."
        ),
        "ALREADY_REALIZED_JOIN_MISS": (
            "A matching settled P&L row already exists; reconciliation should not keep "
            "counting this as eligible."
        ),
        "MISSING_PAPER_POSITION": (
            "The filled paper order has exact settlement evidence but no paper position "
            "for the P&L engine to realize."
        ),
        "ZERO_CONTRACT_POSITION": (
            "The paper position exists but has no YES or NO contracts."
        ),
        "SOURCE_OUTCOME_UNSUPPORTED": (
            "The exact settlement source cannot be mapped to a binary or scalar payout."
        ),
        "EXACT_SETTLEMENT_MISSING": "No exact ticker settlement exists locally.",
    }
    return explanations.get(classification, "No specialized classification is available.")


def _safe_repair_action(classification: str) -> str:
    if classification in {"SCALAR_READY_FOR_REALIZER", "BINARY_READY_FOR_REALIZER"}:
        return "RUN_PHASE3AA_REALIZE_EXACT_TICKER_ONLY"
    if classification == "ALREADY_REALIZED_JOIN_MISS":
        return "FIX_RECONCILIATION_REALIZED_STATE_JOIN"
    if classification in {"MISSING_PAPER_POSITION", "ZERO_CONTRACT_POSITION"}:
        return "AUDIT_PAPER_LEDGER_POSITION_INTEGRITY"
    if classification == "SOURCE_OUTCOME_UNSUPPORTED":
        return "AUDIT_EXACT_SOURCE_OUTCOME_FIELDS"
    return "KEEP_WATCHING_EXACT_TICKER_SETTLEMENTS"


def _next_commands(summary: dict[str, Any]) -> list[str]:
    commands = [
        "kalshi-bot phase3aa-r3-residual-settlement-audit --output-dir reports/phase3aa_r3",
    ]
    if summary["safe_to_run_phase3aa_realize"]:
        commands.append("kalshi-bot phase3aa-realize --no-dry-run --no-sync-settlements")
    commands.extend(
        [
            (
                "kalshi-bot paper-settlement-doctor "
                "--output-dir reports/paper_settlement_reconciliation"
            ),
            "kalshi-bot phase3az-gap-analysis --output-dir reports/phase3az --reports-dir reports",
            (
                "kalshi-bot phase-orchestrator --analyze "
                "--output reports/phase_orchestrator.md "
                "--json-output reports/phase_orchestrator.json "
                "--next-prompt prompts/next_phase.md"
            ),
        ]
    )
    return commands


def _recommended_next_action(summary: dict[str, Any]) -> str:
    if summary["residue_cleared"]:
        return "Residual exact-settlement eligibility is clear; proceed to Phase 3AE evidence use."
    if summary["safe_to_run_phase3aa_realize"]:
        return (
            "Run Phase 3AA without dry-run; residual rows have exact ticker settlement "
            "and supported payout evidence."
        )
    if summary["missing_position_rows"] or summary["zero_contract_position_rows"]:
        return "Audit paper ledger position integrity before another realization pass."
    if summary["already_realized_join_miss_rows"]:
        return "Fix realized-state reconciliation so already-realized rows stop showing eligible."
    if summary["unsupported_source_outcome_rows"]:
        return "Audit exact source outcome fields before changing settlement parsing."
    return "Keep exact-ticker settlement watch running; no safe realization action is available."


def _render_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Phase 3AA-R3 Residual Exact Settlement Realization Audit",
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
    for key, value in payload["summary"].items():
        lines.append(f"- {key}: {value}")
    lines.extend(
        [
            "",
            "## Classification Counts",
            "",
            "| Classification | Count |",
            "| --- | ---: |",
        ]
    )
    for classification, count in payload["classification_counts"].items():
        lines.append(f"| {classification} | {count} |")
    if not payload["classification_counts"]:
        lines.append("| None | 0 |")
    lines.extend(
        [
            "",
            "## Residual Rows",
            "",
            (
                "| Order | Ticker | Doctor reason | R3 classification | "
                "Settlement | Position | Action |"
            ),
            "| ---: | --- | --- | --- | --- | --- | --- |",
        ]
    )
    for row in payload["rows"][:50]:
        lines.append(
            f"| {row['paper_order_id']} | {_md(row['ticker'])} | "
            f"{row['doctor_reason']} | {row['classification']} | "
            f"{_md(row.get('settlement_result'))}/{_md(row.get('yes_settlement_value'))} | "
            f"{row.get('position_contracts')} | {row['safe_repair_action']} |"
        )
    if not payload["rows"]:
        lines.append("|  | None |  |  |  |  | No residual eligible rows. |")
    lines.extend(["", "## Next Commands", ""])
    for command in payload["next_commands"]:
        lines.append(f"- `{command}`")
    lines.extend(["", "## Recommended Next Action", "", payload["recommended_next_action"], ""])
    return "\n".join(lines)


def _md(value: object) -> str:
    if value is None:
        return ""
    return str(value).replace("|", "\\|").replace("\n", " ")
