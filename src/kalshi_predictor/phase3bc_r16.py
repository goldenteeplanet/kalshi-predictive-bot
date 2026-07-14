from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.crypto.ticker_windows import crypto_ticker_close_time_utc
from kalshi_predictor.paper.settlement_reconciliation import PAPER_ONLY_SAFETY
from kalshi_predictor.phase3bc_r4 import write_phase3bc_r4_crypto_ev_risk_diagnostics_report
from kalshi_predictor.phase3bc_r5 import write_phase3bc_r5_crypto_freshness_watch_report
from kalshi_predictor.utils.decimals import decimal_to_str, to_decimal
from kalshi_predictor.utils.time import utc_now

PHASE3BC_R16_VERSION = "phase3bc_r16_crypto_paper_ready_edge_hunt"
MODEL_NAME = "crypto_v2"


@dataclass(frozen=True)
class Phase3BCR16ArtifactSet:
    output_dir: Path
    json_path: Path
    markdown_path: Path
    rows_path: Path
    phase3bc_json_path: Path
    phase3bc_rows_path: Path
    phase3bc_r4_json_path: Path
    phase3bc_r5_json_path: Path | None


def write_phase3bc_r16_crypto_paper_ready_edge_hunt_report(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3bc_r16"),
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
    risk_preflight: bool = True,
    exact_snapshot_refresh: bool = True,
) -> Phase3BCR16ArtifactSet:
    """Build the no-paid-data crypto edge hunt report.

    R16 does not introduce a new execution path. When refresh is enabled it delegates
    bounded refresh, ranking repair, and paper-only Phase 3M/3N preflight to R5.
    """
    resolved = settings or get_settings()
    r5_payload: dict[str, Any] | None = None
    r5_json_path: Path | None = None
    if run_refresh:
        r5_artifacts = write_phase3bc_r5_crypto_freshness_watch_report(
            session,
            output_dir=phase3bc_r5_output_dir,
            phase3bc_output_dir=phase3bc_output_dir,
            phase3bc_r3_output_dir=phase3bc_r3_output_dir,
            phase3bc_r4_output_dir=phase3bc_r4_output_dir,
            phase3bc_r7_output_dir=phase3bc_r7_output_dir,
            settings=resolved,
            phase3bc_limit=limit,
            freshness_minutes=freshness_minutes,
            max_preflight=max_preflight,
            risk_preflight=risk_preflight,
            exact_snapshot_refresh=exact_snapshot_refresh,
        )
        r5_json_path = r5_artifacts.json_path
        r5_payload = _read_json(r5_artifacts.json_path)
        r4_json_path = r5_artifacts.phase3bc_r4_json_path
        r4_payload = _read_json(r4_json_path)
        phase3bc_rows_path = Path(str(r5_payload["reports"]["phase3bc_rows"]))
        phase3bc_json_path = Path(str(r5_payload["reports"]["phase3bc_json"]))
    else:
        r4_artifacts = write_phase3bc_r4_crypto_ev_risk_diagnostics_report(
            session,
            output_dir=phase3bc_r4_output_dir,
            phase3bc_output_dir=phase3bc_output_dir,
            settings=resolved,
            limit=limit,
            freshness_minutes=freshness_minutes,
        )
        r4_json_path = r4_artifacts.json_path
        r4_payload = _read_json(r4_artifacts.json_path)
        phase3bc_rows_path = r4_artifacts.phase3bc_rows_path
        phase3bc_json_path = r4_artifacts.phase3bc_json_path

    phase3bc_payload = _read_json(phase3bc_json_path)
    phase3bc_rows = list(_read_json(phase3bc_rows_path))
    payload = build_phase3bc_r16_payload(
        phase3bc_rows,
        phase3bc_payload=phase3bc_payload,
        r4_payload=r4_payload,
        r5_payload=r5_payload,
        run_refresh=run_refresh,
        freshness_minutes=freshness_minutes,
        limit=limit,
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "phase3bc_r16_crypto_paper_ready_edge_hunt.json"
    markdown_path = output_dir / "phase3bc_r16_crypto_paper_ready_edge_hunt.md"
    rows_path = output_dir / "phase3bc_r16_crypto_paper_ready_edge_hunt_rows.json"
    json_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    rows_path.write_text(
        json.dumps(payload["crypto_decision_rows"], indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    markdown_path.write_text(_render_markdown(payload), encoding="utf-8")
    return Phase3BCR16ArtifactSet(
        output_dir=output_dir,
        json_path=json_path,
        markdown_path=markdown_path,
        rows_path=rows_path,
        phase3bc_json_path=phase3bc_json_path,
        phase3bc_rows_path=phase3bc_rows_path,
        phase3bc_r4_json_path=r4_json_path,
        phase3bc_r5_json_path=r5_json_path,
    )


def build_phase3bc_r16_payload(
    rows: list[dict[str, Any]],
    *,
    phase3bc_payload: dict[str, Any],
    r4_payload: dict[str, Any],
    r5_payload: dict[str, Any] | None = None,
    run_refresh: bool = False,
    freshness_minutes: int = 15,
    limit: int = 2000,
    now: Any | None = None,
) -> dict[str, Any]:
    generated_at = now or utc_now()
    diagnostics_by_ticker = _diagnostics_by_ticker(r4_payload)
    active_pure_rows = [
        row
        for row in rows
        if row.get("active_market") and row.get("structure_status") == "PURE_CRYPTO"
    ]
    current_rows = [
        row for row in active_pure_rows if _active_window_status(row, generated_at) != "EXPIRED"
    ]
    decision_rows = [
        _decision_row(row, diagnostics_by_ticker.get(str(row.get("ticker"))))
        for row in current_rows
    ]
    decision_rows.sort(key=_executable_ev_sort_key, reverse=True)
    paper_ready_rows = [
        row for row in decision_rows if row["paper_ready_candidate"]
    ]
    positive_ev_rows = [
        row for row in decision_rows if (to_decimal(row.get("expected_value")) or Decimal("0")) > 0
    ]
    positive_ev_blocked_rows = [
        row for row in positive_ev_rows if not row["paper_ready_candidate"]
    ]
    clean_execution_rows = [
        row for row in decision_rows if row["execution_quality"] == "CLEAN"
    ]
    risk_ready_rows = [
        row
        for row in decision_rows
        if row.get("phase3n_risk_state") not in {"MISSING", "STALE", "UNKNOWN", None}
    ]
    blocker_counts = Counter(row["primary_blocker"] for row in decision_rows)
    readiness_counts = Counter(row.get("readiness_status") for row in decision_rows)
    r5_summary = (r5_payload or {}).get("summary", {})
    expired_active_pure_rows = len(active_pure_rows) - len(current_rows)
    summary = {
        "rows_checked": len(rows),
        "scan_limit": limit,
        "active_pure_crypto_rows": len(active_pure_rows),
        "current_active_pure_crypto_rows": len(current_rows),
        "expired_active_pure_crypto_rows": expired_active_pure_rows,
        "paper_ready_candidates": len(paper_ready_rows),
        "positive_ev_rows": len(positive_ev_rows),
        "positive_ev_blocked_rows": len(positive_ev_blocked_rows),
        "clean_execution_rows": len(clean_execution_rows),
        "risk_ready_rows": len(risk_ready_rows),
        "top_primary_blocker": blocker_counts.most_common(1)[0][0]
        if blocker_counts
        else None,
        "phase3m_phase3n_preflight_attempted": r5_summary.get(
            "phase3m_phase3n_preflight_attempted",
            0,
        ),
        "positive_ev_preflight_candidates": r5_summary.get(
            "positive_ev_preflight_candidates",
            0,
        ),
        "true_ranking_gap_after_repair": r5_summary.get(
            "true_ranking_gap_after_repair",
            r4_payload.get("summary", {}).get("true_ranking_gap_after_repair", 0),
        ),
        "snapshot_stale_rows": r5_summary.get(
            "snapshot_stale_rows",
            r4_payload.get("summary", {}).get("snapshot_stale_rows", 0),
        ),
        "forecast_stale_rows": r5_summary.get(
            "forecast_stale_rows",
            r4_payload.get("summary", {}).get("forecast_stale_rows", 0),
        ),
        "refresh_mode": "R5_REFRESH_RANKING_AND_PREFLIGHT"
        if run_refresh
        else "R4_DIAGNOSTIC_ONLY",
    }
    return {
        "generated_at": generated_at.isoformat(),
        "phase": "3BC-R16",
        "phase_version": PHASE3BC_R16_VERSION,
        "mode": "PAPER_ONLY_NO_PAID_DATA_CRYPTO_EDGE_HUNT",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "live_or_demo_execution": False,
        "order_submission": False,
        "order_cancel_replace": False,
        "risk_preflight_only": True,
        "model_name": MODEL_NAME,
        "freshness_minutes": freshness_minutes,
        "summary": summary,
        "phase3bc_summary": phase3bc_payload.get("summary", {}),
        "phase3bc_r4_summary": r4_payload.get("summary", {}),
        "phase3bc_r5_summary": r5_summary,
        "readiness_counts": dict(sorted(readiness_counts.items())),
        "primary_blocker_counts": dict(sorted(blocker_counts.items())),
        "paper_ready_rows": paper_ready_rows[:50],
        "positive_ev_blocked_rows": positive_ev_blocked_rows[:50],
        "best_no_paid_data_rows": decision_rows[:50],
        "crypto_decision_rows": decision_rows,
        "recommended_next_action": _recommended_next_action(summary),
        "next_commands": _next_commands(summary),
    }


def _decision_row(
    row: dict[str, Any],
    diagnostic: dict[str, Any] | None,
) -> dict[str, Any]:
    readiness = str(row.get("readiness_status") or "UNKNOWN")
    expected_value = decimal_to_str(to_decimal(row.get("expected_value")))
    freshness_issue = (diagnostic or {}).get("freshness_issue")
    risk_state = (diagnostic or {}).get("phase3n_risk_state")
    primary_blocker = _primary_blocker(row, diagnostic)
    return {
        "ticker": row.get("ticker"),
        "clean_title": row.get("clean_title") or row.get("title"),
        "event_ticker": row.get("event_ticker"),
        "series_ticker": row.get("series_ticker"),
        "market_status": row.get("market_status"),
        "active_market": bool(row.get("active_market")),
        "active_window_status": _active_window_status(row, utc_now()),
        "structure_status": row.get("structure_status"),
        "best_side": row.get("best_side"),
        "best_price": decimal_to_str(to_decimal(row.get("best_price"))),
        "model_probability": decimal_to_str(to_decimal(row.get("model_probability"))),
        "expected_value": expected_value,
        "estimated_edge": decimal_to_str(to_decimal(row.get("estimated_edge"))),
        "opportunity_score": decimal_to_str(to_decimal(row.get("opportunity_score"))),
        "liquidity_score": decimal_to_str(to_decimal(row.get("liquidity_score"))),
        "spread": decimal_to_str(to_decimal(row.get("spread"))),
        "book_state": row.get("book_state"),
        "book_usable": bool(row.get("book_usable")),
        "book_reason": row.get("book_reason"),
        "bid_depth": decimal_to_str(to_decimal(row.get("bid_depth"))),
        "ask_depth": decimal_to_str(to_decimal(row.get("ask_depth"))),
        "book_bid_price": decimal_to_str(to_decimal(row.get("book_bid_price"))),
        "book_ask_price": decimal_to_str(to_decimal(row.get("book_ask_price"))),
        "book_spread": decimal_to_str(to_decimal(row.get("book_spread"))),
        "confidence_score": decimal_to_str(to_decimal(row.get("confidence_score"))),
        "time_to_close_minutes": decimal_to_str(to_decimal(row.get("time_to_close_minutes"))),
        "latest_snapshot_at": row.get("latest_snapshot_at"),
        "latest_forecast_at": row.get("latest_forecast_at"),
        "latest_ranking_at": row.get("latest_ranking_at"),
        "freshness_issue": freshness_issue,
        "readiness_status": readiness,
        "final_action": row.get("final_action"),
        "paper_ready_candidate": readiness == "PAPER_READY_CANDIDATE",
        "execution_quality": _execution_quality(row),
        "primary_blocker": primary_blocker,
        "blocker_categories": list((diagnostic or {}).get("blocker_categories") or []),
        "blocking_gates": list((diagnostic or {}).get("blocking_gates") or []),
        "blockers": list(row.get("blockers") or []),
        "phase3n_risk_state": risk_state or "UNKNOWN",
        "phase3n_risk_reason": (diagnostic or {}).get("phase3n_risk_reason"),
        "what_would_make_paper_ready": _what_would_make_paper_ready(row, diagnostic),
        "rank_basis": "expected_value_then_liquidity_then_spread",
        "kalshi_lookup": row.get("kalshi_lookup", {}),
    }


def _diagnostics_by_ticker(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for key in (
        "top_blocked_rows",
        "paper_ready_missing_risk_rows",
        "no_positive_ev_examples",
        "stale_or_unranked_examples",
        "snapshot_freshness_examples",
        "forecast_freshness_examples",
        "primary_gap_examples",
        "expired_crypto_window_examples",
    ):
        for row in payload.get(key, []) or []:
            ticker = row.get("ticker")
            if ticker:
                rows[str(ticker)] = row
    return rows


def _active_window_status(row: dict[str, Any], now: Any) -> str:
    close_time = crypto_ticker_close_time_utc(row.get("ticker"))
    if close_time is None:
        return "CURRENT_OR_UNKNOWN"
    if close_time <= now:
        return "EXPIRED"
    return "CURRENT"


def _execution_quality(row: dict[str, Any]) -> str:
    readiness = str(row.get("readiness_status") or "")
    book_state = str(row.get("book_state") or "")
    if book_state == "CLEAN_BOOK":
        return "CLEAN" if readiness == "PAPER_READY_CANDIDATE" else "WATCH"
    if book_state == "NO_EXECUTABLE_BOOK":
        return "NOT_EXECUTABLE"
    if book_state == "THIN_BOOK":
        return "NO_LIQUIDITY"
    if book_state == "WIDE_SPREAD":
        return "WIDE_SPREAD"
    if (to_decimal(row.get("liquidity_score")) or Decimal("0")) <= 0:
        return "NO_LIQUIDITY"
    if readiness == "PAPER_READY_CANDIDATE":
        return "CLEAN"
    if readiness == "BLOCKED_NO_LIQUIDITY":
        return "NO_LIQUIDITY"
    if readiness == "BLOCKED_WIDE_SPREAD":
        return "WIDE_SPREAD"
    if readiness in {"BLOCKED_MISSING_EXECUTABLE_PRICE", "BLOCKED_MISSING_ACTIVE_SNAPSHOT"}:
        return "NOT_EXECUTABLE"
    if readiness.startswith("BLOCKED_"):
        return "BLOCKED"
    return "WATCH"


def _primary_blocker(
    row: dict[str, Any],
    diagnostic: dict[str, Any] | None,
) -> str:
    if row.get("readiness_status") == "PAPER_READY_CANDIDATE":
        risk_state = (diagnostic or {}).get("phase3n_risk_state")
        if risk_state in {None, "MISSING", "STALE"}:
            return "RISK_MISSING"
        return "PAPER_READY"
    if (
        (to_decimal(row.get("expected_value")) or Decimal("0")) > 0
        and row.get("book_state") == "NO_EXECUTABLE_BOOK"
    ):
        return "LIQUIDITY_BLOCKED"
    if (
        (to_decimal(row.get("expected_value")) or Decimal("0")) > 0
        and (to_decimal(row.get("liquidity_score")) or Decimal("0")) <= 0
    ):
        return "LIQUIDITY_BLOCKED"
    categories = list((diagnostic or {}).get("blocker_categories") or [])
    if categories:
        return str(categories[0])
    readiness = str(row.get("readiness_status") or "UNKNOWN")
    return readiness


def _what_would_make_paper_ready(
    row: dict[str, Any],
    diagnostic: dict[str, Any] | None,
) -> list[str]:
    blocker = _primary_blocker(row, diagnostic)
    if blocker == "LIQUIDITY_BLOCKED":
        return ["Wait for executable liquidity above the configured threshold."]
    actions = list((diagnostic or {}).get("what_would_make_paper_ready") or [])
    if actions:
        return actions
    row_actions = list(row.get("what_would_make_tradable") or [])
    if row_actions:
        return row_actions
    fallback = {
        "RISK_MISSING": "Run paper-only Phase 3M/3N preflight after all market gates are clean.",
        "EV_NOT_POSITIVE": "Wait for a better executable price or stronger crypto_v2 probability.",
        "SNAPSHOT_STALE": "Refresh the exact active crypto ticker snapshot/orderbook.",
        "SNAPSHOT_MISSING": "Fetch an exact active crypto ticker snapshot/orderbook.",
        "FORECAST_STALE": "Rebuild crypto features and rerun crypto_v2 forecasts.",
        "FORECAST_MISSING": "Build crypto features and create a crypto_v2 forecast.",
        "RANKING_MISSING": "Rerun Phase 3BC-R7 ranking coverage repair.",
        "RANKING_STALE": "Rerun Phase 3BC-R7 ranking coverage repair.",
        "RANKING_BEFORE_FORECAST": "Rerun ranking after the latest crypto_v2 forecast.",
        "LIQUIDITY_BLOCKED": "Wait for executable liquidity above the configured threshold.",
        "SPREAD_BLOCKED": "Wait for spread below the configured threshold.",
    }
    return [fallback.get(blocker, "Keep this row in watch mode until its blocking gates clear.")]


def _executable_ev_sort_key(row: dict[str, Any]) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    spread = to_decimal(row.get("spread"))
    spread_key = Decimal("999") if spread is None else -spread
    return (
        to_decimal(row.get("expected_value")) or Decimal("-999"),
        to_decimal(row.get("liquidity_score")) or Decimal("0"),
        spread_key,
        to_decimal(row.get("opportunity_score")) or Decimal("0"),
    )


def _recommended_next_action(summary: dict[str, Any]) -> str:
    if summary["paper_ready_candidates"] > 0:
        return (
            "Clean pure-crypto paper candidates exist. Run or review paper-only Phase 3M/3N "
            "preflight; keep live/demo execution blocked."
        )
    if summary["active_pure_crypto_rows"] > 0 and summary["current_active_pure_crypto_rows"] == 0:
        return (
            "The scanned pure-crypto rows are expired windows. Run the bounded R5 refresh so "
            "R16 can evaluate current active crypto markets before ranking EV."
        )
    if summary["positive_ev_rows"] > 0:
        return (
            "Positive-EV pure crypto rows exist, but execution, freshness, or risk gates still "
            "block them. Use the R16 row blockers to target the next refresh."
        )
    if summary["true_ranking_gap_after_repair"] > 0:
        return "Repair current-window crypto ranking gaps with Phase 3BC-R7/R5."
    if summary["snapshot_stale_rows"] > 0:
        return "Refresh exact active crypto snapshots; stale snapshots are blocking ranking truth."
    return "Keep the 15-minute crypto watch running and wait for positive executable EV."


def _next_commands(summary: dict[str, Any]) -> list[str]:
    commands = [
        (
            "kalshi-bot phase3bc-r16-crypto-paper-ready-edge-hunt "
            "--output-dir reports/phase3bc_r16 --run-refresh"
        ),
    ]
    if summary["paper_ready_candidates"] > 0:
        commands.append(
            "Review reports/phase3bc_r16/phase3bc_r16_crypto_paper_ready_edge_hunt_rows.json"
        )
    elif (
        summary["active_pure_crypto_rows"] > 0
        and summary["current_active_pure_crypto_rows"] == 0
    ):
        commands.append(
            "kalshi-bot phase3bc-r5-crypto-freshness-watch --output-dir reports/phase3bc_r5"
        )
    elif summary["true_ranking_gap_after_repair"] > 0:
        commands.append(
            "kalshi-bot phase3bc-r7-crypto-ranking-coverage-repair "
            "--output-dir reports/phase3bc_r7 --repair-rankings"
        )
    else:
        commands.append(
            "kalshi-bot phase3bc-r5-crypto-freshness-watch --output-dir reports/phase3bc_r5"
        )
    return commands


def _render_markdown(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    lines = [
        "# Phase 3BC-R16 Crypto Paper-Ready Edge Hunt",
        "",
        f"- Generated at: `{payload['generated_at']}`",
        f"- Mode: `{payload['mode']}`",
        f"- Safety: `{payload['paper_only_safety']}`",
        "- Live/demo execution: blocked",
        "- Order submission/cancel/replace: blocked",
        "- Paid data required: no",
        "",
        "## Summary",
        "",
    ]
    for key, value in summary.items():
        lines.append(f"- {key}: `{value}`")
    lines.extend(
        [
            "",
            "## Best No-Paid-Data Rows",
            "",
            (
                "| Ticker | Market | Status | Side | Price | EV | Score | Liquidity | "
                "Spread | Blocker |"
            ),
            "|---|---|---|---|---:|---:|---:|---:|---:|---|",
        ]
    )
    _append_rows(lines, payload["best_no_paid_data_rows"], empty="No current pure crypto rows.")
    lines.extend(
        [
            "",
            "## Positive-EV Blocked Rows",
            "",
            (
                "| Ticker | Market | EV | Execution | Risk | Primary Blocker | "
                "What Would Make It Ready |"
            ),
            "|---|---|---:|---|---|---|---|",
        ]
    )
    _append_blocked_rows(
        lines,
        payload["positive_ev_blocked_rows"],
        empty="No positive-EV blocked rows.",
    )
    lines.extend(
        [
            "",
            "## Paper-Ready Rows",
            "",
            "| Ticker | Market | Side | Price | EV | Risk | Next Action |",
            "|---|---|---|---:|---:|---|---|",
        ]
    )
    _append_ready_rows(lines, payload["paper_ready_rows"])
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


def _append_rows(lines: list[str], rows: list[dict[str, Any]], *, empty: str) -> None:
    if not rows:
        lines.append(f"| _{empty}_ |  |  |  |  |  |  |  |  |  |")
        return
    for row in rows:
        lines.append(
            "| "
            f"{row['ticker']} | "
            f"{_cell(row['clean_title'])} | "
            f"{row['readiness_status']} | "
            f"{row.get('best_side') or ''} | "
            f"{row.get('best_price') or ''} | "
            f"{row.get('expected_value') or ''} | "
            f"{row.get('opportunity_score') or ''} | "
            f"{row.get('liquidity_score') or ''} | "
            f"{row.get('spread') or ''} | "
            f"{row.get('primary_blocker') or ''} |"
        )


def _append_blocked_rows(
    lines: list[str],
    rows: list[dict[str, Any]],
    *,
    empty: str,
) -> None:
    if not rows:
        lines.append(f"| _{empty}_ |  |  |  |  |  |  |")
        return
    for row in rows:
        lines.append(
            "| "
            f"{row['ticker']} | "
            f"{_cell(row['clean_title'])} | "
            f"{row.get('expected_value') or ''} | "
            f"{row.get('execution_quality') or ''} | "
            f"{row.get('phase3n_risk_state') or ''} | "
            f"{row.get('primary_blocker') or ''} | "
            f"{_cell('; '.join(row.get('what_would_make_paper_ready') or []))} |"
        )


def _append_ready_rows(lines: list[str], rows: list[dict[str, Any]]) -> None:
    if not rows:
        lines.append("| _No paper-ready rows._ |  |  |  |  |  |  |")
        return
    for row in rows:
        lines.append(
            "| "
            f"{row['ticker']} | "
            f"{_cell(row['clean_title'])} | "
            f"{row.get('best_side') or ''} | "
            f"{row.get('best_price') or ''} | "
            f"{row.get('expected_value') or ''} | "
            f"{row.get('phase3n_risk_state') or ''} | "
            f"{_cell('; '.join(row.get('what_would_make_paper_ready') or []))} |"
        )


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _cell(value: Any) -> str:
    return str(value or "").replace("|", "\\|").replace("\n", " ")
