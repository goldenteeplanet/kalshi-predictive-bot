from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.paper.settlement_reconciliation import PAPER_ONLY_SAFETY
from kalshi_predictor.phase3bc_r16 import (
    write_phase3bc_r16_crypto_paper_ready_edge_hunt_report,
)
from kalshi_predictor.utils.decimals import decimal_to_str, to_decimal
from kalshi_predictor.utils.time import utc_now

PHASE3BC_R17_VERSION = "phase3bc_r17_crypto_liquidity_actionability"
MODEL_NAME = "crypto_v2"
MIN_ACTIONABLE_LIQUIDITY_SCORE = Decimal("30")
MAX_ACTIONABLE_SPREAD = Decimal("0.02")


@dataclass(frozen=True)
class Phase3BCR17ArtifactSet:
    output_dir: Path
    json_path: Path
    markdown_path: Path
    rows_path: Path
    phase3bc_r16_json_path: Path


def write_phase3bc_r17_crypto_liquidity_actionability_report(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3bc_r17"),
    phase3bc_r16_output_dir: Path = Path("reports/phase3bc_r16"),
    phase3bc_output_dir: Path = Path("reports/phase3bc"),
    phase3bc_r3_output_dir: Path = Path("reports/phase3bc_r3"),
    phase3bc_r4_output_dir: Path = Path("reports/phase3bc_r4"),
    phase3bc_r5_output_dir: Path = Path("reports/phase3bc_r5"),
    phase3bc_r7_output_dir: Path = Path("reports/phase3bc_r7"),
    settings: Settings | None = None,
    limit: int = 2000,
    freshness_minutes: int = 15,
    run_refresh: bool = False,
    max_preflight: int = 10,
) -> Phase3BCR17ArtifactSet:
    resolved = settings or get_settings()
    r16_artifacts = write_phase3bc_r16_crypto_paper_ready_edge_hunt_report(
        session,
        output_dir=phase3bc_r16_output_dir,
        phase3bc_output_dir=phase3bc_output_dir,
        phase3bc_r3_output_dir=phase3bc_r3_output_dir,
        phase3bc_r4_output_dir=phase3bc_r4_output_dir,
        phase3bc_r5_output_dir=phase3bc_r5_output_dir,
        phase3bc_r7_output_dir=phase3bc_r7_output_dir,
        settings=resolved,
        limit=limit,
        freshness_minutes=freshness_minutes,
        run_refresh=run_refresh,
        max_preflight=max_preflight,
        risk_preflight=True,
        exact_snapshot_refresh=True,
    )
    r16_payload = _read_json(r16_artifacts.json_path)
    payload = build_phase3bc_r17_payload(r16_payload)

    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "phase3bc_r17_crypto_liquidity_actionability.json"
    markdown_path = output_dir / "phase3bc_r17_crypto_liquidity_actionability.md"
    rows_path = output_dir / "phase3bc_r17_crypto_liquidity_actionability_rows.json"
    json_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    rows_path.write_text(
        json.dumps(payload["rows"], indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    markdown_path.write_text(_render_markdown(payload), encoding="utf-8")
    return Phase3BCR17ArtifactSet(
        output_dir=output_dir,
        json_path=json_path,
        markdown_path=markdown_path,
        rows_path=rows_path,
        phase3bc_r16_json_path=r16_artifacts.json_path,
    )


def build_phase3bc_r17_payload(r16_payload: dict[str, Any]) -> dict[str, Any]:
    rows = [
        _liquidity_row(row)
        for row in r16_payload.get("crypto_decision_rows", [])
        if row.get("structure_status") == "PURE_CRYPTO"
    ]
    rows.sort(key=_sort_key, reverse=True)
    positive_ev_rows = [row for row in rows if _positive_ev(row)]
    no_book_rows = [row for row in positive_ev_rows if row["book_state"] == "NO_EXECUTABLE_BOOK"]
    thin_book_rows = [row for row in positive_ev_rows if row["book_state"] == "THIN_BOOK"]
    wide_spread_rows = [row for row in positive_ev_rows if row["book_state"] == "WIDE_SPREAD"]
    clean_book_rows = [row for row in positive_ev_rows if row["book_state"] == "CLEAN_BOOK"]
    paper_ready_rows = [row for row in rows if row["paper_ready_candidate"]]
    risk_waiting_rows = [
        row
        for row in clean_book_rows
        if row.get("phase3n_risk_state") in {"MISSING", "STALE", "UNKNOWN", None}
    ]
    book_counts = Counter(row["book_state"] for row in rows)
    actionability_counts = Counter(row["actionability_state"] for row in rows)
    summary = {
        "rows_checked": len(rows),
        "positive_ev_rows": len(positive_ev_rows),
        "positive_ev_no_executable_book_rows": len(no_book_rows),
        "positive_ev_thin_book_rows": len(thin_book_rows),
        "positive_ev_wide_spread_rows": len(wide_spread_rows),
        "positive_ev_clean_book_rows": len(clean_book_rows),
        "paper_ready_candidates": len(paper_ready_rows),
        "clean_book_waiting_for_risk_rows": len(risk_waiting_rows),
        "liquidity_positive_candidates": len(thin_book_rows) + len(clean_book_rows),
        "watch_target": _watch_target(
            paper_ready_rows=paper_ready_rows,
            clean_book_rows=clean_book_rows,
            thin_book_rows=thin_book_rows,
            no_book_rows=no_book_rows,
            positive_ev_rows=positive_ev_rows,
        ),
        "source_r16_refresh_mode": r16_payload.get("summary", {}).get("refresh_mode"),
    }
    return {
        "generated_at": utc_now().isoformat(),
        "phase": "3BC-R17",
        "phase_version": PHASE3BC_R17_VERSION,
        "mode": "PAPER_ONLY_CRYPTO_LIQUIDITY_ACTIONABILITY",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "live_or_demo_execution": False,
        "order_submission": False,
        "order_cancel_replace": False,
        "risk_preflight_only": True,
        "model_name": MODEL_NAME,
        "thresholds": {
            "min_actionable_liquidity_score": str(MIN_ACTIONABLE_LIQUIDITY_SCORE),
            "max_actionable_spread": str(MAX_ACTIONABLE_SPREAD),
        },
        "summary": summary,
        "book_state_counts": dict(sorted(book_counts.items())),
        "actionability_state_counts": dict(sorted(actionability_counts.items())),
        "paper_ready_rows": paper_ready_rows[:50],
        "clean_book_waiting_for_risk_rows": risk_waiting_rows[:50],
        "positive_ev_no_executable_book_rows": no_book_rows[:50],
        "positive_ev_liquidity_positive_rows": [*thin_book_rows, *clean_book_rows][:50],
        "rows": rows,
        "recommended_next_action": _recommended_next_action(summary),
        "next_commands": _next_commands(summary),
    }


def _liquidity_row(row: dict[str, Any]) -> dict[str, Any]:
    expected_value = to_decimal(row.get("expected_value"))
    liquidity_score = to_decimal(row.get("liquidity_score"))
    spread = to_decimal(row.get("spread"))
    book_state = _book_state(row, liquidity_score=liquidity_score, spread=spread)
    actionability = _actionability_state(row, book_state=book_state)
    return {
        "ticker": row.get("ticker"),
        "clean_title": row.get("clean_title"),
        "event_ticker": row.get("event_ticker"),
        "series_ticker": row.get("series_ticker"),
        "readiness_status": row.get("readiness_status"),
        "paper_ready_candidate": bool(row.get("paper_ready_candidate")),
        "best_side": row.get("best_side"),
        "best_price": row.get("best_price"),
        "expected_value": decimal_to_str(expected_value),
        "expected_value_cents": _cents(expected_value),
        "liquidity_score": decimal_to_str(liquidity_score),
        "spread": decimal_to_str(spread),
        "book_reason": row.get("book_reason"),
        "bid_depth": row.get("bid_depth"),
        "ask_depth": row.get("ask_depth"),
        "opportunity_score": row.get("opportunity_score"),
        "confidence_score": row.get("confidence_score"),
        "phase3n_risk_state": row.get("phase3n_risk_state"),
        "book_state": book_state,
        "actionability_state": actionability,
        "primary_blocker": row.get("primary_blocker"),
        "what_would_make_paper_ready": _action_for_book_state(row, book_state),
        "latest_snapshot_at": row.get("latest_snapshot_at"),
        "latest_forecast_at": row.get("latest_forecast_at"),
        "latest_ranking_at": row.get("latest_ranking_at"),
        "kalshi_lookup": row.get("kalshi_lookup", {}),
    }


def _book_state(
    row: dict[str, Any],
    *,
    liquidity_score: Decimal | None,
    spread: Decimal | None,
) -> str:
    source_state = str(row.get("book_state") or "")
    if source_state in {"CLEAN_BOOK", "NO_EXECUTABLE_BOOK", "THIN_BOOK", "WIDE_SPREAD"}:
        return source_state
    if to_decimal(row.get("best_price")) is None:
        return "NO_EXECUTABLE_BOOK"
    if liquidity_score is None or liquidity_score <= 0:
        return "NO_EXECUTABLE_BOOK"
    if liquidity_score < MIN_ACTIONABLE_LIQUIDITY_SCORE:
        return "THIN_BOOK"
    if spread is None or spread > MAX_ACTIONABLE_SPREAD:
        return "WIDE_SPREAD"
    return "CLEAN_BOOK"


def _actionability_state(row: dict[str, Any], *, book_state: str) -> str:
    if not _positive_ev(row):
        return "WAITING_FOR_POSITIVE_EV"
    if book_state == "NO_EXECUTABLE_BOOK":
        return "POSITIVE_EV_NO_EXECUTABLE_BOOK"
    if book_state == "THIN_BOOK":
        return "POSITIVE_EV_THIN_BOOK"
    if book_state == "WIDE_SPREAD":
        return "POSITIVE_EV_WIDE_SPREAD"
    if row.get("paper_ready_candidate"):
        return "PAPER_READY_CANDIDATE"
    if row.get("phase3n_risk_state") in {"MISSING", "STALE", "UNKNOWN", None}:
        return "CLEAN_BOOK_WAITING_FOR_RISK"
    return "CLEAN_BOOK_WATCH"


def _action_for_book_state(row: dict[str, Any], book_state: str) -> list[str]:
    if book_state == "NO_EXECUTABLE_BOOK":
        return ["Wait for an executable YES/NO book with non-zero liquidity."]
    if book_state == "THIN_BOOK":
        return ["Wait for liquidity score to rise above the configured threshold."]
    if book_state == "WIDE_SPREAD":
        return ["Wait for spread to tighten below the configured threshold."]
    if row.get("paper_ready_candidate"):
        return ["Review paper-only Phase 3M/3N preflight evidence."]
    actions = row.get("what_would_make_paper_ready") or []
    return list(actions) or ["Keep watching until the remaining risk gates clear."]


def _watch_target(
    *,
    paper_ready_rows: list[dict[str, Any]],
    clean_book_rows: list[dict[str, Any]],
    thin_book_rows: list[dict[str, Any]],
    no_book_rows: list[dict[str, Any]],
    positive_ev_rows: list[dict[str, Any]],
) -> str:
    if paper_ready_rows:
        return "PAPER_READY_REVIEW"
    if clean_book_rows:
        return "RUN_PAPER_ONLY_RISK_PREFLIGHT"
    if thin_book_rows:
        return "WAIT_FOR_CLEAN_LIQUIDITY"
    if no_book_rows:
        return "WAIT_FOR_EXECUTABLE_BOOK"
    if positive_ev_rows:
        return "WAIT_FOR_EXECUTION_QUALITY"
    return "WAIT_FOR_POSITIVE_EV"


def _recommended_next_action(summary: dict[str, Any]) -> str:
    target = summary["watch_target"]
    if target == "PAPER_READY_REVIEW":
        return (
            "Paper-ready crypto rows exist. Review the paper-only risk evidence before "
            "any next step."
        )
    if target == "RUN_PAPER_ONLY_RISK_PREFLIGHT":
        return "Clean-book positive-EV rows exist; run paper-only Phase 3M/3N preflight."
    if target == "WAIT_FOR_CLEAN_LIQUIDITY":
        return "Liquidity-positive rows exist, but the book is still too thin or wide."
    if target == "WAIT_FOR_EXECUTABLE_BOOK":
        return (
            "Positive EV exists, but no executable book is available. Keep the 15-minute "
            "watch running."
        )
    return "Keep the 15-minute crypto watch running until positive EV and clean liquidity coincide."


def _next_commands(summary: dict[str, Any]) -> list[str]:
    if summary["watch_target"] in {"PAPER_READY_REVIEW", "RUN_PAPER_ONLY_RISK_PREFLIGHT"}:
        return [
            (
                "kalshi-bot phase3bc-r16-crypto-paper-ready-edge-hunt "
                "--output-dir reports/phase3bc_r16 --run-refresh --max-preflight 5"
            )
        ]
    return [
        (
            "kalshi-bot phase3bc-r5-crypto-freshness-watch "
            "--output-dir reports/phase3bc_r5 --max-preflight 5"
        ),
        (
            "kalshi-bot phase3bc-r17-crypto-liquidity-actionability "
            "--output-dir reports/phase3bc_r17"
        ),
    ]


def _render_markdown(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    lines = [
        "# Phase 3BC-R17 Crypto Liquidity Actionability",
        "",
        f"- Generated at: `{payload['generated_at']}`",
        f"- Mode: `{payload['mode']}`",
        "- PAPER ONLY: no live/demo execution and no order submission.",
        "",
        "## Summary",
        "",
    ]
    for key, value in summary.items():
        lines.append(f"- {key}: `{value}`")
    lines.extend(
        [
            "",
            "## Positive-EV Without Executable Book",
            "",
            "| Ticker | Market | EV cents | Liquidity | Spread | Action |",
            "|---|---|---:|---:|---:|---|",
        ]
    )
    _append_rows(lines, payload["positive_ev_no_executable_book_rows"])
    lines.extend(
        [
            "",
            "## Liquidity-Positive Rows",
            "",
            "| Ticker | Market | EV cents | Book | Liquidity | Spread | Action |",
            "|---|---|---:|---|---:|---:|---|",
        ]
    )
    _append_rows(lines, payload["positive_ev_liquidity_positive_rows"], include_book=True)
    lines.extend(
        [
            "",
            "## Recommended Next Action",
            "",
            payload["recommended_next_action"],
            "",
            "## Next Commands",
            "",
            "```bash",
            *payload["next_commands"],
            "```",
            "",
        ]
    )
    return "\n".join(lines)


def _append_rows(
    lines: list[str],
    rows: list[dict[str, Any]],
    *,
    include_book: bool = False,
) -> None:
    if not rows:
        suffix = " |" if include_book else ""
        lines.append(f"| _No rows._ |  |  |  |  |  |{suffix}")
        return
    for row in rows:
        action = "; ".join(row.get("what_would_make_paper_ready") or [])
        if include_book:
            lines.append(
                "| "
                f"{row.get('ticker')} | "
                f"{_cell(row.get('clean_title'))} | "
                f"{row.get('expected_value_cents') or ''} | "
                f"{row.get('book_state') or ''} | "
                f"{row.get('liquidity_score') or ''} | "
                f"{row.get('spread') or ''} | "
                f"{_cell(action)} |"
            )
        else:
            lines.append(
                "| "
                f"{row.get('ticker')} | "
                f"{_cell(row.get('clean_title'))} | "
                f"{row.get('expected_value_cents') or ''} | "
                f"{row.get('liquidity_score') or ''} | "
                f"{row.get('spread') or ''} | "
                f"{_cell(action)} |"
            )


def _positive_ev(row: dict[str, Any]) -> bool:
    ev = to_decimal(row.get("expected_value"))
    return ev is not None and ev > 0


def _sort_key(row: dict[str, Any]) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    book_priority = {
        "CLEAN_BOOK": Decimal("4"),
        "THIN_BOOK": Decimal("3"),
        "WIDE_SPREAD": Decimal("2"),
        "NO_EXECUTABLE_BOOK": Decimal("1"),
    }.get(str(row.get("book_state")), Decimal("0"))
    return (
        to_decimal(row.get("expected_value")) or Decimal("-999"),
        book_priority,
        to_decimal(row.get("liquidity_score")) or Decimal("0"),
        to_decimal(row.get("opportunity_score")) or Decimal("0"),
    )


def _cents(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return decimal_to_str(value * Decimal("100"))


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _cell(value: Any) -> str:
    return str(value or "").replace("|", "\\|").replace("\n", " ")
