from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.crypto.ticker_windows import crypto_ticker_close_time_utc
from kalshi_predictor.data.repositories import decode_json
from kalshi_predictor.data.schema import AdvancedRiskDecisionLog
from kalshi_predictor.paper.models import BUY_NO, BUY_YES
from kalshi_predictor.paper.settlement_reconciliation import PAPER_ONLY_SAFETY
from kalshi_predictor.phase3bc import write_phase3bc_crypto_clean_opportunity_report
from kalshi_predictor.utils.decimals import decimal_to_str, to_decimal
from kalshi_predictor.utils.time import parse_datetime, utc_now

PHASE3BC_R4_VERSION = "phase3bc_r4_crypto_ev_risk_readiness_diagnostics"
MODEL_NAME = "crypto_v2"
CURRENT_WINDOW_DIAGNOSTIC_EXPORT_LIMIT = 5000

FRESHNESS_OK = "FRESH"
RANKING_MISSING = "RANKING_MISSING"
RANKING_STALE = "RANKING_STALE"
RANKING_BEFORE_FORECAST = "RANKING_BEFORE_FORECAST"
SNAPSHOT_MISSING = "SNAPSHOT_MISSING"
SNAPSHOT_STALE = "SNAPSHOT_STALE"
EXPIRED_CRYPTO_WINDOW = "EXPIRED_CRYPTO_WINDOW"
FORECAST_MISSING = "FORECAST_MISSING"
FORECAST_STALE = "FORECAST_STALE"
EV_NOT_POSITIVE = "EV_NOT_POSITIVE"
LIQUIDITY_BLOCKED = "LIQUIDITY_BLOCKED"
SPREAD_BLOCKED = "SPREAD_BLOCKED"
RISK_MISSING = "RISK_MISSING"

TRUE_RANKING_GAPS = {
    RANKING_MISSING,
    RANKING_STALE,
    RANKING_BEFORE_FORECAST,
}


@dataclass(frozen=True)
class Phase3BCR4ArtifactSet:
    output_dir: Path
    json_path: Path
    markdown_path: Path
    phase3bc_json_path: Path
    phase3bc_rows_path: Path


def write_phase3bc_r4_crypto_ev_risk_diagnostics_report(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3bc_r4"),
    phase3bc_output_dir: Path = Path("reports/phase3bc"),
    settings: Settings | None = None,
    limit: int = 1000,
    freshness_minutes: int = 15,
) -> Phase3BCR4ArtifactSet:
    """Diagnose why active pure crypto rows are not paper-ready."""
    resolved = settings or get_settings()
    phase3bc_artifacts = write_phase3bc_crypto_clean_opportunity_report(
        session,
        output_dir=phase3bc_output_dir,
        settings=resolved,
        limit=limit,
    )
    phase3bc_payload = _read_json(phase3bc_artifacts.json_path)
    rows = list(phase3bc_payload.get("rows", []))
    risk_by_ticker = _latest_risk_decisions_by_ticker(
        session,
        [str(row.get("ticker")) for row in rows if row.get("ticker")],
    )
    payload = build_phase3bc_r4_payload(
        rows,
        risk_by_ticker=risk_by_ticker,
        phase3bc_summary=phase3bc_payload.get("summary", {}),
        thresholds=phase3bc_payload.get("thresholds", {}),
        freshness_minutes=freshness_minutes,
        phase3bc_artifacts=phase3bc_artifacts,
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "phase3bc_r4_crypto_ev_risk_diagnostics.json"
    markdown_path = output_dir / "phase3bc_r4_crypto_ev_risk_diagnostics.md"
    json_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    markdown_path.write_text(_render_markdown(payload), encoding="utf-8")
    return Phase3BCR4ArtifactSet(
        output_dir=output_dir,
        json_path=json_path,
        markdown_path=markdown_path,
        phase3bc_json_path=phase3bc_artifacts.json_path,
        phase3bc_rows_path=phase3bc_artifacts.rows_path,
    )


def build_phase3bc_r4_payload(
    rows: list[dict[str, Any]],
    *,
    risk_by_ticker: dict[str, dict[str, Any]],
    phase3bc_summary: dict[str, Any],
    thresholds: dict[str, Any],
    freshness_minutes: int = 15,
    phase3bc_artifacts: Any | None = None,
    now: Any | None = None,
) -> dict[str, Any]:
    now = now or utc_now()
    active_pure_rows = [
        row
        for row in rows
        if row.get("active_market") and row.get("structure_status") == "PURE_CRYPTO"
    ]
    diagnostics = [
        _diagnose_row(
            row,
            risk=risk_by_ticker.get(str(row.get("ticker"))),
            thresholds=thresholds,
            freshness_minutes=freshness_minutes,
            now=now,
        )
        for row in active_pure_rows
    ]
    diagnostics.sort(key=_diagnostic_sort_key, reverse=True)
    current_window_diagnostics = [
        row for row in diagnostics if row["freshness_issue"] != EXPIRED_CRYPTO_WINDOW
    ]
    expired_window_diagnostics = [
        row for row in diagnostics if row["freshness_issue"] == EXPIRED_CRYPTO_WINDOW
    ]

    readiness_counts = Counter(row.get("readiness_status") for row in active_pure_rows)
    gate_counts = Counter(gate for row in diagnostics for gate in row["blocking_gates"])
    blocker_counts = Counter(
        category for row in diagnostics for category in row["blocker_categories"]
    )
    current_blocker_counts = Counter(
        category
        for row in current_window_diagnostics
        for category in row["blocker_categories"]
    )
    freshness_counts = Counter(row["freshness_issue"] for row in diagnostics)
    risk_counts = Counter(row["phase3n_risk_state"] for row in diagnostics)
    current_risk_counts = Counter(
        row["phase3n_risk_state"] for row in current_window_diagnostics
    )
    true_ranking_gap_rows = sum(
        freshness_counts.get(issue, 0) for issue in TRUE_RANKING_GAPS
    )
    positive_ev_rows = sum(
        1
        for row in current_window_diagnostics
        if (to_decimal(row.get("expected_value")) or Decimal("-1")) > 0
    )
    clean_execution_rows = sum(
        1 for row in current_window_diagnostics if _has_clean_execution(row)
    )
    risk_ready_rows = sum(
        1
        for row in current_window_diagnostics
        if row["phase3n_risk_state"] not in {"MISSING", "STALE"}
    )
    summary = {
        "phase3bc_rows_checked": len(rows),
        "active_pure_crypto_rows": len(active_pure_rows),
        "current_active_window_rows": len(current_window_diagnostics),
        "expired_crypto_window_rows": len(expired_window_diagnostics),
        "paper_ready_candidates": sum(
            1
            for row in current_window_diagnostics
            if row.get("readiness_status") == "PAPER_READY_CANDIDATE"
        ),
        "watch_only_rows": sum(
            1 for row in current_window_diagnostics if row.get("final_action") == "WATCH_ONLY"
        ),
        "blocked_rows": sum(
            1 for row in current_window_diagnostics if row.get("final_action") == "BLOCKED"
        ),
        "no_positive_ev_rows": sum(
            1
            for row in current_window_diagnostics
            if row.get("readiness_status") == "WATCH_NO_POSITIVE_EXPECTED_VALUE"
        ),
        "low_edge_rows": sum(
            1
            for row in current_window_diagnostics
            if row.get("readiness_status") == "WATCH_LOW_EDGE"
        ),
        "missing_or_stale_ranking_rows": true_ranking_gap_rows,
        "true_ranking_gap_after_repair": true_ranking_gap_rows,
        "ranking_missing_rows": freshness_counts.get(RANKING_MISSING, 0),
        "ranking_stale_rows": freshness_counts.get(RANKING_STALE, 0),
        "ranking_before_forecast_rows": freshness_counts.get(RANKING_BEFORE_FORECAST, 0),
        "snapshot_missing_rows": freshness_counts.get(SNAPSHOT_MISSING, 0),
        "snapshot_stale_rows": freshness_counts.get(SNAPSHOT_STALE, 0),
        "current_snapshot_stale_rows": freshness_counts.get(SNAPSHOT_STALE, 0),
        "forecast_missing_rows": freshness_counts.get(FORECAST_MISSING, 0),
        "forecast_stale_rows": freshness_counts.get(FORECAST_STALE, 0),
        "positive_ev_rows": positive_ev_rows,
        "clean_execution_rows": clean_execution_rows,
        "risk_ready_rows": risk_ready_rows,
        "spread_or_liquidity_blocked_rows": sum(
            1
            for row in current_window_diagnostics
            if "spread_block" in row["blocking_gates"]
            or "liquidity_block" in row["blocking_gates"]
        ),
        "missing_phase3n_risk_rows": sum(
            1 for row in current_window_diagnostics if row["phase3n_risk_state"] == "MISSING"
        ),
        "missing_phase3n_for_paper_ready_rows": sum(
            1
            for row in current_window_diagnostics
            if row["phase3n_risk_state"] == "MISSING"
            and row["readiness_status"] == "PAPER_READY_CANDIDATE"
        ),
        "phase3n_risk_existing_rows": sum(
            1
            for row in current_window_diagnostics
            if row["phase3n_risk_state"] != "MISSING"
        ),
        "primary_gap": _primary_gap(
            current_blocker_counts,
            current_risk_counts,
            current_window_diagnostics,
            expired_window_rows=len(expired_window_diagnostics),
            total_rows=len(diagnostics),
        ),
        "primary_gap_scope": "CURRENT_ACTIVE_CRYPTO_WINDOWS",
    }
    primary_gap_examples = _primary_gap_examples(
        summary["primary_gap"],
        current_window_diagnostics=current_window_diagnostics,
        expired_window_diagnostics=expired_window_diagnostics,
    )
    return {
        "generated_at": now.isoformat(),
        "phase": "3BC-R4",
        "phase_version": PHASE3BC_R4_VERSION,
        "mode": "PAPER_ONLY_CRYPTO_EV_AND_RISK_READINESS_DIAGNOSTICS",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "live_or_demo_execution": False,
        "model_name": MODEL_NAME,
        "freshness_minutes": freshness_minutes,
        "phase3bc_summary": phase3bc_summary,
        "thresholds": thresholds,
        "summary": summary,
        "readiness_counts": dict(sorted(readiness_counts.items())),
        "blocking_gate_counts": dict(sorted(gate_counts.items())),
        "blocker_category_counts": dict(sorted(blocker_counts.items())),
        "current_window_blocker_category_counts": dict(
            sorted(current_blocker_counts.items())
        ),
        "freshness_issue_counts": dict(sorted(freshness_counts.items())),
        "phase3n_risk_counts": dict(sorted(risk_counts.items())),
        "current_window_diagnostics": current_window_diagnostics[
            :CURRENT_WINDOW_DIAGNOSTIC_EXPORT_LIMIT
        ],
        "top_blocked_rows": current_window_diagnostics[:50],
        "paper_ready_missing_risk_rows": [
            row
            for row in current_window_diagnostics
            if row["readiness_status"] == "PAPER_READY_CANDIDATE"
            and row["phase3n_risk_state"] == "MISSING"
        ][:50],
        "no_positive_ev_examples": [
            row
            for row in current_window_diagnostics
            if row["readiness_status"] == "WATCH_NO_POSITIVE_EXPECTED_VALUE"
        ][:50],
        "stale_or_unranked_examples": [
            row
            for row in current_window_diagnostics
            if row["freshness_issue"] in TRUE_RANKING_GAPS
        ][:50],
        "snapshot_freshness_rows": [
            row
            for row in current_window_diagnostics
            if row["freshness_issue"] in {SNAPSHOT_MISSING, SNAPSHOT_STALE}
        ][:CURRENT_WINDOW_DIAGNOSTIC_EXPORT_LIMIT],
        "snapshot_freshness_examples": [
            row
            for row in current_window_diagnostics
            if row["freshness_issue"] in {SNAPSHOT_MISSING, SNAPSHOT_STALE}
        ][:50],
        "forecast_freshness_rows": [
            row
            for row in current_window_diagnostics
            if row["freshness_issue"] in {FORECAST_MISSING, FORECAST_STALE}
        ][:CURRENT_WINDOW_DIAGNOSTIC_EXPORT_LIMIT],
        "forecast_freshness_examples": [
            row
            for row in current_window_diagnostics
            if row["freshness_issue"] in {FORECAST_MISSING, FORECAST_STALE}
        ][:50],
        "primary_gap_examples": primary_gap_examples,
        "expired_crypto_window_examples": expired_window_diagnostics[:50],
        "reports": {
            "phase3bc_json": str(phase3bc_artifacts.json_path)
            if phase3bc_artifacts is not None
            else None,
            "phase3bc_rows": str(phase3bc_artifacts.rows_path)
            if phase3bc_artifacts is not None
            else None,
        },
        "recommended_next_action": _recommended_next_action(summary),
        "next_commands": _next_commands(summary),
    }


def _diagnose_row(
    row: dict[str, Any],
    *,
    risk: dict[str, Any] | None,
    thresholds: dict[str, Any],
    freshness_minutes: int,
    now: Any,
) -> dict[str, Any]:
    ranked_at = parse_datetime(row.get("latest_ranking_at"))
    forecast_at = parse_datetime(row.get("latest_forecast_at"))
    snapshot_at = parse_datetime(row.get("latest_snapshot_at"))
    ticker_close_time = crypto_ticker_close_time_utc(row.get("ticker"))
    expired_crypto_window = ticker_close_time is not None and ticker_close_time <= now
    freshness_issue = (
        EXPIRED_CRYPTO_WINDOW
        if expired_crypto_window
        else _freshness_issue(
            snapshot_at=snapshot_at,
            forecast_at=forecast_at,
            ranked_at=ranked_at,
            freshness_minutes=freshness_minutes,
            now=now,
        )
    )
    gates = _blocking_gates(
        row,
        freshness_issue=freshness_issue,
    )
    side_probability = _side_probability(row)
    best_price = to_decimal(row.get("best_price"))
    ev = _expected_value(row)
    risk_state, risk_reason = _risk_state(row, risk=risk, ranked_at=ranked_at, now=now)
    categories = _blocker_categories(
        row,
        gates=gates,
        freshness_issue=freshness_issue,
        risk_state=risk_state,
    )
    return {
        "ticker": row.get("ticker"),
        "clean_title": row.get("clean_title") or row.get("title"),
        "event_ticker": row.get("event_ticker"),
        "series_ticker": row.get("series_ticker"),
        "market_status": row.get("market_status"),
        "active_market": bool(row.get("active_market")),
        "active_window_status": "EXPIRED" if expired_crypto_window else "CURRENT_OR_UNKNOWN",
        "ticker_close_time_utc": ticker_close_time.isoformat() if ticker_close_time else None,
        "structure_status": row.get("structure_status"),
        "readiness_status": row.get("readiness_status"),
        "final_action": row.get("final_action"),
        "best_side": row.get("best_side"),
        "best_price": decimal_to_str(best_price),
        "side_probability": decimal_to_str(side_probability),
        "expected_value": decimal_to_str(ev),
        "expected_value_cents": _cents(ev),
        "price_improvement_needed_for_positive_ev": _price_improvement_needed(
            best_price,
            side_probability,
        ),
        "estimated_edge": decimal_to_str(to_decimal(row.get("estimated_edge"))),
        "opportunity_score": decimal_to_str(to_decimal(row.get("opportunity_score"))),
        "liquidity_score": decimal_to_str(to_decimal(row.get("liquidity_score"))),
        "spread": decimal_to_str(to_decimal(row.get("spread"))),
        "confidence_score": decimal_to_str(to_decimal(row.get("confidence_score"))),
        "time_to_close_minutes": decimal_to_str(to_decimal(row.get("time_to_close_minutes"))),
        "latest_snapshot_at": snapshot_at.isoformat() if snapshot_at else None,
        "latest_forecast_at": forecast_at.isoformat() if forecast_at else None,
        "latest_ranking_at": ranked_at.isoformat() if ranked_at else None,
        "snapshot_age_minutes": _age_minutes(snapshot_at, now=now),
        "forecast_age_minutes": _age_minutes(forecast_at, now=now),
        "ranking_age_minutes": _age_minutes(ranked_at, now=now),
        "freshness_issue": freshness_issue,
        "blocking_gates": gates,
        "blocker_categories": categories,
        "phase3n_risk_state": risk_state,
        "phase3n_risk_reason": risk_reason,
        "phase3n_latest": risk,
        "what_would_make_paper_ready": _what_would_make_ready(row, gates, risk_state),
        "kalshi_lookup": row.get("kalshi_lookup", {}),
    }


def _blocking_gates(
    row: dict[str, Any],
    *,
    freshness_issue: str,
) -> list[str]:
    gates: list[str] = []
    status = str(row.get("readiness_status") or "")
    freshness_gate = _freshness_gate(freshness_issue)
    if freshness_gate is not None:
        gates.append(freshness_gate)
    if status in {
        "BLOCKED_FORECAST_NOT_RANKED",
        "RANKING_NOT_GENERATED_FOR_CURRENT_FORECAST",
        "CURRENT_FORECAST_MISSING_RANKING",
        "FORECAST_NOT_RANKED",
    }:
        gates.append("ranking_missing")
    if status == "WATCH_NO_POSITIVE_EXPECTED_VALUE":
        gates.append("ev_not_positive")
    if status == "WATCH_LOW_EDGE":
        gates.append("edge_below_threshold")
    if status == "WATCH_LOW_SCORE":
        gates.append("score_below_threshold")
    if status == "WATCH_LOW_CONFIDENCE":
        gates.append("confidence_below_threshold")
    if status == "BLOCKED_WIDE_SPREAD":
        gates.append("spread_block")
    if status == "BLOCKED_NO_LIQUIDITY":
        gates.append("liquidity_block")
    if status == "BLOCKED_NO_EXECUTABLE_BOOK":
        gates.append("executable_book_missing")
    if status == "BLOCKED_MISSING_EXECUTABLE_PRICE":
        gates.append("missing_executable_price")
    if status == "WATCH_PAYOUT_FILTER_NOT_MET":
        gates.append("payout_filter")
    if status == "WATCH_TOO_CLOSE_TO_SETTLEMENT":
        gates.append("time_to_close")
    return _unique(gates)


def _freshness_issue(
    *,
    snapshot_at: Any,
    forecast_at: Any,
    ranked_at: Any,
    freshness_minutes: int,
    now: Any,
) -> str:
    if snapshot_at is None:
        return SNAPSHOT_MISSING
    if (now - snapshot_at).total_seconds() / 60 > freshness_minutes:
        return SNAPSHOT_STALE
    if forecast_at is None:
        return FORECAST_MISSING
    if (now - forecast_at).total_seconds() / 60 > freshness_minutes:
        return FORECAST_STALE
    if ranked_at is None:
        return RANKING_MISSING
    if ranked_at < forecast_at:
        return RANKING_BEFORE_FORECAST
    if (now - ranked_at).total_seconds() / 60 > freshness_minutes:
        return RANKING_STALE
    return FRESHNESS_OK


def _freshness_gate(issue: str) -> str | None:
    return {
        SNAPSHOT_MISSING: "snapshot_missing",
        SNAPSHOT_STALE: "snapshot_stale",
        EXPIRED_CRYPTO_WINDOW: "expired_crypto_window",
        FORECAST_MISSING: "forecast_missing",
        FORECAST_STALE: "forecast_stale",
        RANKING_MISSING: "ranking_missing",
        RANKING_STALE: "ranking_stale",
        RANKING_BEFORE_FORECAST: "ranking_before_forecast",
    }.get(issue)


def _blocker_categories(
    row: dict[str, Any],
    *,
    gates: list[str],
    freshness_issue: str,
    risk_state: str,
) -> list[str]:
    categories: list[str] = []
    if freshness_issue != FRESHNESS_OK:
        categories.append(freshness_issue)
    if "ev_not_positive" in gates:
        categories.append(EV_NOT_POSITIVE)
    if "liquidity_block" in gates:
        categories.append(LIQUIDITY_BLOCKED)
    if "executable_book_missing" in gates:
        categories.append(LIQUIDITY_BLOCKED)
    if "spread_block" in gates:
        categories.append(SPREAD_BLOCKED)
    if risk_state == "MISSING" and row.get("readiness_status") == "PAPER_READY_CANDIDATE":
        categories.append(RISK_MISSING)
    return _unique(categories)


def _risk_state(
    row: dict[str, Any],
    *,
    risk: dict[str, Any] | None,
    ranked_at: Any,
    now: Any,
) -> tuple[str, str]:
    if risk is None:
        if row.get("readiness_status") == "PAPER_READY_CANDIDATE":
            return "MISSING", "Paper-ready candidate has not passed through Phase 3N yet."
        return (
            "MISSING",
            "Expected until EV/ranking/execution gates produce a candidate for Phase 3M/3N.",
        )
    decision_at = parse_datetime(risk.get("decision_timestamp"))
    if decision_at is not None and ranked_at is not None and decision_at < ranked_at:
        return "STALE", "Phase 3N decision predates the latest crypto ranking."
    if decision_at is not None and (now - decision_at).total_seconds() > 24 * 60 * 60:
        return "STALE", "Phase 3N decision is older than 24 hours."
    action = str(risk.get("action") or "UNKNOWN").upper()
    if action == "BLOCK":
        return "BLOCKED", "Phase 3N blocked the latest known decision for this ticker."
    if action == "REDUCE":
        return "REDUCED", "Phase 3N reduced the latest known decision for this ticker."
    if action == "ALLOW":
        return "AVAILABLE", "Phase 3N evidence exists for this ticker."
    return "AVAILABLE", "Phase 3N evidence exists with an unrecognized action."


def _what_would_make_ready(
    row: dict[str, Any],
    gates: list[str],
    risk_state: str,
) -> list[str]:
    actions: list[str] = []
    if "ev_not_positive" in gates:
        needed = _price_improvement_needed(
            to_decimal(row.get("best_price")),
            _side_probability(row),
        )
        if needed is not None:
            actions.append(
                f"Best ask must improve by about {needed} cents or model probability must rise."
            )
        else:
            actions.append("Need positive expected value from price or model probability movement.")
    if "ranking_missing" in gates:
        actions.append("Rerun crypto opportunity ranking for this exact ticker.")
    if "ranking_stale" in gates:
        actions.append(
            "Refresh crypto rankings for rows with fresh snapshots and forecasts."
        )
    if "ranking_before_forecast" in gates:
        actions.append("Refresh ranking because the forecast is newer than the ranking.")
    if "expired_crypto_window" in gates:
        actions.append(
            "Drop this expired crypto window from active refresh candidates and "
            "focus on current open windows."
        )
    if "snapshot_missing" in gates or "snapshot_stale" in gates:
        actions.append("Refresh exact-ticker crypto market snapshots for these active rows.")
    if "forecast_missing" in gates or "forecast_stale" in gates:
        actions.append("Refresh crypto_v2 forecasts after snapshot data is current.")
    if "spread_block" in gates:
        actions.append("Spread must tighten below the configured threshold.")
    if "liquidity_block" in gates:
        actions.append("Liquidity score must rise above the executable threshold.")
    if "executable_book_missing" in gates:
        actions.append("Visible bid/ask depth must appear at executable best prices.")
    if "edge_below_threshold" in gates:
        actions.append("Model edge must rise above the configured threshold.")
    if "score_below_threshold" in gates:
        actions.append("Opportunity score must rise above the configured threshold.")
    if "confidence_below_threshold" in gates:
        actions.append("Model confidence must rise above the executable threshold.")
    if risk_state == "MISSING" and row.get("readiness_status") == "PAPER_READY_CANDIDATE":
        actions.append("Run Phase 3M/3N paper-only risk checks before any paper-ready action.")
    if not actions:
        actions.append("Keep refreshing active crypto prices/orderbooks until gates improve.")
    return _unique(actions)


def _latest_risk_decisions_by_ticker(
    session: Session,
    tickers: list[str],
) -> dict[str, dict[str, Any]]:
    if not tickers:
        return {}
    seen: dict[str, dict[str, Any]] = {}
    rows = session.scalars(
        select(AdvancedRiskDecisionLog)
        .where(AdvancedRiskDecisionLog.ticker.in_(tickers))
        .order_by(
            desc(AdvancedRiskDecisionLog.decision_timestamp),
            desc(AdvancedRiskDecisionLog.id),
        )
    )
    for row in rows:
        if row.ticker in seen:
            continue
        seen[row.ticker] = {
            "id": row.id,
            "decision_timestamp": row.decision_timestamp.isoformat(),
            "mode": row.mode,
            "action": row.action,
            "phase_3m_tier": row.phase_3m_tier,
            "phase_3m_proposed_contracts": row.phase_3m_proposed_contracts,
            "live_candidate_contracts": row.live_candidate_contracts,
            "executed_contracts": row.executed_contracts,
            "reason_codes": _decode_list(row.reason_codes_json),
            "hard_blocks": _decode_list(row.hard_blocks_json),
            "limiting_factors": _decode_list(row.limiting_factors_json),
        }
    return seen


def _expected_value(row: dict[str, Any]) -> Decimal | None:
    value = to_decimal(row.get("expected_value"))
    if value is not None:
        return value
    side_probability = _side_probability(row)
    best_price = to_decimal(row.get("best_price"))
    if side_probability is None or best_price is None:
        return None
    return side_probability - best_price


def _side_probability(row: dict[str, Any]) -> Decimal | None:
    yes_probability = to_decimal(row.get("model_probability"))
    if yes_probability is None:
        yes_probability = to_decimal(row.get("forecast_probability"))
    side = row.get("best_side")
    if side == BUY_YES:
        return yes_probability
    if side == BUY_NO and yes_probability is not None:
        return Decimal("1") - yes_probability
    return None


def _price_improvement_needed(
    best_price: Decimal | None,
    side_probability: Decimal | None,
) -> str | None:
    if best_price is None or side_probability is None:
        return None
    needed = best_price - side_probability
    if needed <= 0:
        return "0.0"
    return decimal_to_str((needed * Decimal("100")).quantize(Decimal("0.1")))


def _cents(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return decimal_to_str((value * Decimal("100")).quantize(Decimal("0.1")))


def _age_minutes(value: Any, *, now: Any) -> str | None:
    if value is None:
        return None
    return decimal_to_str(Decimal(str((now - value).total_seconds() / 60)).quantize(Decimal("0.1")))


def _diagnostic_sort_key(row: dict[str, Any]) -> tuple[Decimal, Decimal, Decimal]:
    return (
        to_decimal(row.get("expected_value")) or Decimal("-999"),
        to_decimal(row.get("opportunity_score")) or Decimal("0"),
        to_decimal(row.get("estimated_edge")) or Decimal("0"),
    )


def _primary_gap(
    blocker_counts: Counter[str],
    risk_counts: Counter[str],
    rows: list[dict[str, Any]],
    *,
    expired_window_rows: int = 0,
    total_rows: int | None = None,
) -> str:
    if not rows:
        if expired_window_rows and (total_rows or 0) > 0:
            return "EXPIRED_CRYPTO_WINDOWS_ONLY"
        return "NO_ACTIVE_PURE_CRYPTO_ROWS"
    for category in (
        SNAPSHOT_STALE,
        SNAPSHOT_MISSING,
        FORECAST_STALE,
        FORECAST_MISSING,
        RANKING_MISSING,
        RANKING_STALE,
        RANKING_BEFORE_FORECAST,
        EV_NOT_POSITIVE,
        LIQUIDITY_BLOCKED,
        SPREAD_BLOCKED,
    ):
        if blocker_counts.get(category, 0) > 0:
            return category
    if risk_counts.get("MISSING", 0) > 0:
        return "PHASE3N_RISK_NOT_REACHED"
    return "NO_DOMINANT_BLOCKER"


def _primary_gap_examples(
    primary_gap: str,
    *,
    current_window_diagnostics: list[dict[str, Any]],
    expired_window_diagnostics: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if primary_gap == "EXPIRED_CRYPTO_WINDOWS_ONLY":
        return expired_window_diagnostics[:50]
    if primary_gap == "PHASE3N_RISK_NOT_REACHED":
        return [
            row
            for row in current_window_diagnostics
            if row["phase3n_risk_state"] == "MISSING"
        ][:50]
    return [
        row
        for row in current_window_diagnostics
        if primary_gap in row["blocker_categories"]
        or primary_gap == row["freshness_issue"]
    ][:50]


def _recommended_next_action(summary: dict[str, Any]) -> str:
    if summary["paper_ready_candidates"] and summary["missing_phase3n_for_paper_ready_rows"]:
        return (
            "Run paper-only Phase 3M/3N risk checks for paper-ready crypto rows; keep "
            "execution blocked."
        )
    if summary.get("primary_gap") == "EXPIRED_CRYPTO_WINDOWS_ONLY":
        return (
            "Expired crypto windows dominate the stale set; prune them from active "
            "decision queues and keep the watcher focused on current open windows."
        )
    if summary.get("snapshot_stale_rows") or summary.get("snapshot_missing_rows"):
        return "Refresh exact-ticker crypto snapshots before treating rankings as stale."
    if summary.get("forecast_stale_rows") or summary.get("forecast_missing_rows"):
        return "Refresh crypto_v2 forecasts after snapshots are current."
    if summary["missing_or_stale_ranking_rows"]:
        return "Rerun Phase 3BC-R3 crypto refresh/ranking on the 15-minute cadence."
    if summary["no_positive_ev_rows"]:
        return (
            "No code repair is needed for most rows; keep refreshing until price/model "
            "movement creates positive EV."
        )
    if summary["spread_or_liquidity_blocked_rows"]:
        return (
            "Wait for tighter spread or better orderbook depth before treating rows as "
            "paper-ready."
        )
    return "Continue 15-minute crypto refreshes and review any new paper-ready candidates manually."


def _next_commands(summary: dict[str, Any]) -> list[str]:
    commands = [
        (
            "kalshi-bot phase3bc-r3-active-crypto-refresh --refresh-open-markets "
            "--market-limit 100 --market-max-pages 1 --crypto-series-tickers "
            "KXBTC,KXETH,KXSOLE,KXXRP,KXDOGE --crypto-market-scan-limit 10000 "
            "--crypto-link-limit 1000 --forecast-limit 1000 --opportunity-limit 150 "
            "--phase3bc-limit 1000"
        ),
        "kalshi-bot phase3bc-r4-crypto-ev-risk-diagnostics --output-dir reports/phase3bc_r4",
    ]
    if summary.get("paper_ready_candidates"):
        commands.append("# Then inspect Phase 3M/3N paper-only risk readiness before any action.")
    return commands


def _has_clean_execution(row: dict[str, Any]) -> bool:
    gates = set(row.get("blocking_gates") or [])
    liquidity_score = to_decimal(row.get("liquidity_score"))
    spread = to_decimal(row.get("spread"))
    best_price = to_decimal(row.get("best_price"))
    if best_price is None or spread is None:
        return False
    if liquidity_score is None or liquidity_score <= 0:
        return False
    return not (
        "spread_block" in gates
        or "liquidity_block" in gates
        or "missing_executable_price" in gates
    )


def _render_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Phase 3BC-R4 Crypto EV + Risk Readiness Diagnostics",
        "",
        f"- Generated at: `{payload['generated_at']}`",
        f"- Mode: `{payload['mode']}`",
        "- PAPER ONLY: no live/demo execution, no paper orders, no risk reservations.",
        f"- Freshness window: `{payload['freshness_minutes']} minutes`",
        "",
        "## Summary",
        "",
    ]
    for key, value in payload["summary"].items():
        lines.append(f"- {key}: `{value}`")
    lines.extend(["", "## Blocking Gates", ""])
    for key, value in payload["blocking_gate_counts"].items():
        lines.append(f"- {key}: `{value}`")
    lines.extend(["", "## Phase 3N Risk", ""])
    for key, value in payload["phase3n_risk_counts"].items():
        lines.append(f"- {key}: `{value}`")
    primary_examples = payload.get("primary_gap_examples") or []
    lines.extend(
        [
            "",
            "## Primary Gap Examples",
            "",
            "| Ticker | Title | Freshness | Snapshot age | Forecast age | "
            "Ranking age | EV cents | Gates |",
            "|---|---|---|---:|---:|---:|---:|---|",
        ]
    )
    for row in primary_examples[:25]:
        lines.append(
            "| "
            f"{row['ticker']} | "
            f"{_cell(row.get('clean_title'))} | "
            f"{row['freshness_issue']} | "
            f"{row.get('snapshot_age_minutes') or ''} | "
            f"{row.get('forecast_age_minutes') or ''} | "
            f"{row.get('ranking_age_minutes') or ''} | "
            f"{row.get('expected_value_cents') or ''} | "
            f"{_cell(', '.join(row['blocking_gates']))} |"
        )
    if not primary_examples:
        lines.append("| _No primary-gap examples available._ |  |  |  |  |  |  |  |")
    lines.extend(
        [
            "",
            "## Top Active Pure Crypto Rows",
            "",
            "| Ticker | Status | EV cents | Needed cents | Score | Gates | Phase 3N |",
            "|---|---|---:|---:|---:|---|---|",
        ]
    )
    for row in payload["top_blocked_rows"][:25]:
        lines.append(
            "| "
            f"{row['ticker']} | "
            f"{row['readiness_status']} | "
            f"{row['expected_value_cents'] or ''} | "
            f"{row['price_improvement_needed_for_positive_ev'] or ''} | "
            f"{row['opportunity_score'] or ''} | "
            f"{_cell(', '.join(row['blocking_gates']))} | "
            f"{row['phase3n_risk_state']} |"
        )
    if not payload["top_blocked_rows"]:
        lines.append("| _No active pure crypto rows._ |  |  |  |  |  |  |")
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


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _decode_list(value: str | None) -> list[Any]:
    decoded = decode_json(value)
    if isinstance(decoded, list):
        return decoded
    if decoded in (None, ""):
        return []
    return [decoded]


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _cell(value: Any) -> str:
    return str(value or "").replace("|", "\\|").replace("\n", " ")
