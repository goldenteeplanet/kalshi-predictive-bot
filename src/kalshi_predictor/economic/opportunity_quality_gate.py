from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from kalshi_predictor.active_universe import is_active_market_status
from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.data.repositories import decode_json
from kalshi_predictor.data.schema import (
    AdvancedRiskDecisionLog,
    EconomicEvent,
    Forecast,
    Market,
    MarketRanking,
    MarketSnapshot,
)
from kalshi_predictor.economic.repository import get_latest_economic_link_for_ticker
from kalshi_predictor.learning.config import learning_paper_settings
from kalshi_predictor.paper.models import BUY_NO, BUY_YES, PaperDecision
from kalshi_predictor.paper.settlement_reconciliation import PAPER_ONLY_SAFETY
from kalshi_predictor.position_sizing.service import ensure_paper_decision_sized
from kalshi_predictor.utils.decimals import decimal_to_str, to_decimal
from kalshi_predictor.utils.time import parse_datetime, utc_now

PHASE3BD_R7_VERSION = "phase3bd_r7_economic_opportunity_quality_gate"
MODEL_NAME = "economic_v1"

FRESHNESS_OK = "FRESH"
MARKET_NOT_ACTIVE = "MARKET_NOT_ACTIVE"
SNAPSHOT_MISSING = "SNAPSHOT_MISSING"
SNAPSHOT_STALE = "SNAPSHOT_STALE"
FORECAST_MISSING = "FORECAST_MISSING"
FORECAST_STALE = "FORECAST_STALE"
RANKING_STALE = "RANKING_STALE"
RANKING_BEFORE_FORECAST = "RANKING_BEFORE_FORECAST"
MISSING_EXECUTABLE_PRICE = "MISSING_EXECUTABLE_PRICE"
MISSING_CONSENSUS_EVIDENCE = "MISSING_CONSENSUS_EVIDENCE"
MISSING_ACTUAL_CONSENSUS_EVIDENCE = "MISSING_ACTUAL_CONSENSUS_EVIDENCE"
EV_NOT_POSITIVE = "EV_NOT_POSITIVE"
EDGE_BELOW_THRESHOLD = "EDGE_BELOW_THRESHOLD"
SCORE_BELOW_THRESHOLD = "SCORE_BELOW_THRESHOLD"
LIQUIDITY_BLOCKED = "LIQUIDITY_BLOCKED"
SPREAD_BLOCKED = "SPREAD_BLOCKED"
RISK_BLOCKED = "RISK_BLOCKED"

BLOCKER_ORDER = (
    MARKET_NOT_ACTIVE,
    SNAPSHOT_MISSING,
    SNAPSHOT_STALE,
    FORECAST_MISSING,
    FORECAST_STALE,
    RANKING_BEFORE_FORECAST,
    RANKING_STALE,
    MISSING_CONSENSUS_EVIDENCE,
    MISSING_ACTUAL_CONSENSUS_EVIDENCE,
    MISSING_EXECUTABLE_PRICE,
    EV_NOT_POSITIVE,
    EDGE_BELOW_THRESHOLD,
    SCORE_BELOW_THRESHOLD,
    LIQUIDITY_BLOCKED,
    SPREAD_BLOCKED,
    RISK_BLOCKED,
)


@dataclass(frozen=True)
class Phase3BDR7Artifacts:
    json_path: Path
    markdown_path: Path
    rows_path: Path
    preflight_rows_path: Path
    payload: dict[str, Any]


def write_phase3bd_r7_economic_opportunity_quality_gate_report(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3bd_r7"),
    settings: Settings | None = None,
    limit: int = 500,
    freshness_minutes: int = 360,
    min_expected_value: Decimal = Decimal("0"),
    min_edge: Decimal = Decimal("0.01"),
    min_score: Decimal = Decimal("60"),
    min_liquidity_score: Decimal = Decimal("1"),
    max_spread: Decimal = Decimal("0.03"),
    require_actual_consensus: bool = True,
    max_preflight: int = 10,
    risk_preflight: bool = False,
) -> Phase3BDR7Artifacts:
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = build_phase3bd_r7_payload(
        session,
        settings=settings,
        limit=limit,
        freshness_minutes=freshness_minutes,
        min_expected_value=min_expected_value,
        min_edge=min_edge,
        min_score=min_score,
        min_liquidity_score=min_liquidity_score,
        max_spread=max_spread,
        require_actual_consensus=require_actual_consensus,
        max_preflight=max_preflight,
        risk_preflight=risk_preflight,
    )
    json_path = output_dir / "phase3bd_r7_economic_opportunity_quality_gate.json"
    markdown_path = output_dir / "phase3bd_r7_economic_opportunity_quality_gate.md"
    rows_path = output_dir / "phase3bd_r7_economic_opportunity_quality_rows.json"
    preflight_rows_path = output_dir / "phase3bd_r7_preflight_rows.json"
    json_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    rows_path.write_text(
        json.dumps(payload["rows"], indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    preflight_rows_path.write_text(
        json.dumps(
            payload["phase3m_phase3n_preflight_results"],
            indent=2,
            sort_keys=True,
            default=str,
        ),
        encoding="utf-8",
    )
    markdown_path.write_text(_render_markdown(payload), encoding="utf-8")
    return Phase3BDR7Artifacts(
        json_path=json_path,
        markdown_path=markdown_path,
        rows_path=rows_path,
        preflight_rows_path=preflight_rows_path,
        payload=payload,
    )


def build_phase3bd_r7_payload(
    session: Session,
    *,
    settings: Settings | None = None,
    limit: int = 500,
    freshness_minutes: int = 360,
    min_expected_value: Decimal = Decimal("0"),
    min_edge: Decimal = Decimal("0.01"),
    min_score: Decimal = Decimal("60"),
    min_liquidity_score: Decimal = Decimal("1"),
    max_spread: Decimal = Decimal("0.03"),
    require_actual_consensus: bool = True,
    max_preflight: int = 10,
    risk_preflight: bool = False,
    now: Any | None = None,
) -> dict[str, Any]:
    resolved_now = now or utc_now()
    resolved_settings = settings or get_settings()
    rankings = _latest_economic_rankings(session, limit=limit)
    tickers = [ranking.ticker for ranking in rankings]
    risk_by_ticker = _latest_risk_decisions_by_ticker(session, tickers)
    thresholds = {
        "freshness_minutes": freshness_minutes,
        "min_expected_value": decimal_to_str(min_expected_value),
        "min_edge": decimal_to_str(min_edge),
        "min_score": decimal_to_str(min_score),
        "min_liquidity_score": decimal_to_str(min_liquidity_score),
        "max_spread": decimal_to_str(max_spread),
        "require_actual_consensus": require_actual_consensus,
    }
    rows = [
        _diagnose_ranking(
            session,
            ranking,
            risk=risk_by_ticker.get(ranking.ticker),
            thresholds=thresholds,
            freshness_minutes=freshness_minutes,
            min_expected_value=min_expected_value,
            min_edge=min_edge,
            min_score=min_score,
            min_liquidity_score=min_liquidity_score,
            max_spread=max_spread,
            require_actual_consensus=require_actual_consensus,
            now=resolved_now,
        )
        for ranking in rankings
    ]
    rows.sort(key=_row_sort_key, reverse=True)
    preflight_candidates = [row for row in rows if row["preflight_ready"]]
    preflight_candidates = preflight_candidates[: max(0, max_preflight)]
    preflight_results = (
        _run_risk_preflight(
            session,
            preflight_candidates,
            settings=_preflight_settings(resolved_settings),
        )
        if risk_preflight
        else []
    )
    summary = _summary(
        rows=rows,
        preflight_candidates=preflight_candidates,
        preflight_results=preflight_results,
        risk_preflight=risk_preflight,
    )
    return {
        "phase": "3BD-R7",
        "phase_version": PHASE3BD_R7_VERSION,
        "generated_at": resolved_now.isoformat(),
        "mode": "PAPER_ONLY_ECONOMIC_OPPORTUNITY_QUALITY_GATE",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "live_or_demo_execution": False,
        "order_submission_cancel_replace": False,
        "risk_preflight_only": True,
        "model_name": MODEL_NAME,
        "thresholds": thresholds,
        "summary": summary,
        "blocker_counts": dict(sorted(Counter(_all_blockers(rows)).items())),
        "evidence_state_counts": dict(
            sorted(Counter(row["economic_evidence_state"] for row in rows).items())
        ),
        "freshness_issue_counts": dict(
            sorted(Counter(row["freshness_issue"] for row in rows).items())
        ),
        "risk_state_counts": dict(
            sorted(Counter(row["phase3n_risk_state"] for row in rows).items())
        ),
        "preflight_candidates": preflight_candidates,
        "phase3m_phase3n_preflight_results": preflight_results,
        "rows": rows,
        "recommended_next_action": _recommended_next_action(summary),
        "next_commands": _next_commands(summary),
    }


def _latest_economic_rankings(session: Session, *, limit: int) -> list[MarketRanking]:
    seen: set[str] = set()
    rankings: list[MarketRanking] = []
    scan_limit = max(limit * 5, limit, 100)
    rows = session.scalars(
        select(MarketRanking)
        .where(MarketRanking.forecast_model == MODEL_NAME)
        .order_by(desc(MarketRanking.ranked_at), desc(MarketRanking.id))
        .limit(scan_limit)
    )
    for row in rows:
        if row.ticker in seen:
            continue
        seen.add(row.ticker)
        rankings.append(row)
        if len(rankings) >= limit:
            break
    return rankings


def _diagnose_ranking(
    session: Session,
    ranking: MarketRanking,
    *,
    risk: dict[str, Any] | None,
    thresholds: dict[str, Any],
    freshness_minutes: int,
    min_expected_value: Decimal,
    min_edge: Decimal,
    min_score: Decimal,
    min_liquidity_score: Decimal,
    max_spread: Decimal,
    require_actual_consensus: bool,
    now: Any,
) -> dict[str, Any]:
    market = session.get(Market, ranking.ticker)
    snapshot = _latest_snapshot(session, ranking.ticker)
    forecast = _latest_forecast(session, ranking.ticker)
    evidence = _economic_evidence(session, ranking.ticker)
    market_status = (
        market.status
        if market is not None and market.status is not None
        else snapshot.status
        if snapshot is not None
        else ranking.status
    )
    snapshot_at = parse_datetime(snapshot.captured_at) if snapshot is not None else None
    forecast_at = parse_datetime(forecast.forecasted_at) if forecast is not None else None
    ranked_at = parse_datetime(ranking.ranked_at) or ranking.ranked_at
    freshness_issue = _freshness_issue(
        snapshot_at=snapshot_at,
        forecast_at=forecast_at,
        ranked_at=ranked_at,
        freshness_minutes=freshness_minutes,
        now=now,
    )
    side_probability = _side_probability(ranking)
    best_price = to_decimal(ranking.best_price)
    expected_value = (
        side_probability - best_price
        if side_probability is not None and best_price is not None
        else None
    )
    spread = to_decimal(ranking.spread)
    liquidity_score = to_decimal(ranking.liquidity_score)
    score = to_decimal(ranking.opportunity_score)
    edge = to_decimal(ranking.estimated_edge)
    risk_state, risk_reason = _risk_state(risk=risk, ranked_at=ranking.ranked_at, now=now)
    blockers = _blockers(
        market_status=market_status,
        freshness_issue=freshness_issue,
        evidence=evidence,
        require_actual_consensus=require_actual_consensus,
        best_price=best_price,
        expected_value=expected_value,
        min_expected_value=min_expected_value,
        edge=edge,
        min_edge=min_edge,
        score=score,
        min_score=min_score,
        liquidity_score=liquidity_score,
        min_liquidity_score=min_liquidity_score,
        spread=spread,
        max_spread=max_spread,
        risk_state=risk_state,
    )
    preflight_ready = not blockers
    return {
        "ticker": ranking.ticker,
        "title": ranking.title,
        "event_ticker": ranking.event_ticker,
        "series_ticker": ranking.series_ticker,
        "market_status": market_status,
        "active_market": is_active_market_status(market_status),
        "ranked_at": ranked_at.isoformat(),
        "latest_snapshot_at": snapshot_at.isoformat() if snapshot_at else None,
        "latest_forecast_at": forecast_at.isoformat() if forecast_at else None,
        "ranking_age_minutes": _age_minutes(ranked_at, now=now),
        "snapshot_age_minutes": _age_minutes(snapshot_at, now=now),
        "forecast_age_minutes": _age_minutes(forecast_at, now=now),
        "freshness_issue": freshness_issue,
        "best_side": ranking.best_side,
        "best_price": decimal_to_str(best_price),
        "side_probability": decimal_to_str(side_probability),
        "forecast_probability": decimal_to_str(to_decimal(ranking.forecast_probability)),
        "expected_value": decimal_to_str(expected_value),
        "expected_value_cents": _cents(expected_value),
        "estimated_edge": decimal_to_str(edge),
        "estimated_edge_cents": _cents(edge),
        "opportunity_score": decimal_to_str(score),
        "liquidity_score": decimal_to_str(liquidity_score),
        "spread": decimal_to_str(spread),
        "spread_cents": _cents(spread),
        "time_to_close_minutes": ranking.time_to_close_minutes,
        "economic_evidence_state": evidence["state"],
        "economic_evidence": evidence,
        "phase3n_risk_state": risk_state,
        "phase3n_risk_reason": risk_reason,
        "phase3n_latest": risk,
        "blockers": blockers,
        "preflight_ready": preflight_ready,
        "what_would_make_preflight_ready": _what_would_make_ready(blockers, thresholds),
    }


def _economic_evidence(session: Session, ticker: str) -> dict[str, Any]:
    link = get_latest_economic_link_for_ticker(session, ticker)
    if link is None:
        return {
            "state": "NO_ECONOMIC_LINK",
            "link_id": None,
            "event_key": None,
            "event_title": None,
            "event_time": None,
            "source": None,
            "source_url": None,
            "actual_value": None,
            "forecast_value": None,
            "previous_value": None,
            "verified_consensus": False,
            "actual_and_consensus": False,
        }
    event = _latest_event(session, link.event_key)
    if event is None:
        return {
            "state": "NO_ECONOMIC_EVENT",
            "link_id": link.id,
            "event_key": link.event_key,
            "event_title": None,
            "event_time": None,
            "source": None,
            "source_url": None,
            "actual_value": None,
            "forecast_value": None,
            "previous_value": None,
            "verified_consensus": False,
            "actual_and_consensus": False,
        }
    raw = decode_json(event.raw_json)
    source_url = _source_url(raw)
    has_actual = to_decimal(event.actual_value) is not None
    has_forecast = to_decimal(event.forecast_value) is not None
    has_previous = to_decimal(event.previous_value) is not None
    verified_consensus = has_forecast and bool(source_url)
    actual_and_consensus = has_actual and verified_consensus
    if actual_and_consensus:
        state = "ACTUAL_AND_CONSENSUS"
    elif verified_consensus:
        state = "CONSENSUS_ONLY"
    elif has_actual:
        state = "ACTUAL_ONLY"
    elif has_previous:
        state = "PREVIOUS_ONLY"
    else:
        state = "CALENDAR_ONLY"
    return {
        "state": state,
        "link_id": link.id,
        "link_confidence": link.confidence,
        "link_reason": link.reason,
        "event_key": event.event_key,
        "event_title": event.title,
        "event_time": event.event_time.isoformat(),
        "source": event.source,
        "source_url": source_url,
        "actual_value": event.actual_value,
        "forecast_value": event.forecast_value,
        "previous_value": event.previous_value,
        "verified_consensus": verified_consensus,
        "actual_and_consensus": actual_and_consensus,
    }


def _blockers(
    *,
    market_status: str | None,
    freshness_issue: str,
    evidence: dict[str, Any],
    require_actual_consensus: bool,
    best_price: Decimal | None,
    expected_value: Decimal | None,
    min_expected_value: Decimal,
    edge: Decimal | None,
    min_edge: Decimal,
    score: Decimal | None,
    min_score: Decimal,
    liquidity_score: Decimal | None,
    min_liquidity_score: Decimal,
    spread: Decimal | None,
    max_spread: Decimal,
    risk_state: str,
) -> list[str]:
    blockers: list[str] = []
    if not is_active_market_status(market_status):
        blockers.append(MARKET_NOT_ACTIVE)
    if freshness_issue != FRESHNESS_OK:
        blockers.append(freshness_issue)
    if not evidence.get("verified_consensus"):
        blockers.append(MISSING_CONSENSUS_EVIDENCE)
    elif require_actual_consensus and not evidence.get("actual_and_consensus"):
        blockers.append(MISSING_ACTUAL_CONSENSUS_EVIDENCE)
    if best_price is None:
        blockers.append(MISSING_EXECUTABLE_PRICE)
    if expected_value is None or expected_value <= min_expected_value:
        blockers.append(EV_NOT_POSITIVE)
    if edge is None or edge < min_edge:
        blockers.append(EDGE_BELOW_THRESHOLD)
    if score is None or score < min_score:
        blockers.append(SCORE_BELOW_THRESHOLD)
    if liquidity_score is None or liquidity_score < min_liquidity_score:
        blockers.append(LIQUIDITY_BLOCKED)
    if spread is None or spread > max_spread:
        blockers.append(SPREAD_BLOCKED)
    if risk_state == "BLOCKED":
        blockers.append(RISK_BLOCKED)
    return _unique(blockers)


def _summary(
    *,
    rows: list[dict[str, Any]],
    preflight_candidates: list[dict[str, Any]],
    preflight_results: list[dict[str, Any]],
    risk_preflight: bool,
) -> dict[str, Any]:
    blocker_counts = Counter(_all_blockers(rows))
    fresh_rows = [row for row in rows if row["freshness_issue"] == FRESHNESS_OK]
    source_ready_rows = [
        row for row in rows if row["economic_evidence"].get("actual_and_consensus")
    ]
    positive_ev_rows = [
        row for row in rows if (to_decimal(row.get("expected_value")) or Decimal("-1")) > 0
    ]
    clean_execution_rows = [
        row
        for row in rows
        if LIQUIDITY_BLOCKED not in row["blockers"]
        and SPREAD_BLOCKED not in row["blockers"]
        and MISSING_EXECUTABLE_PRICE not in row["blockers"]
    ]
    risk_ready_rows = [
        row for row in rows if row["phase3n_risk_state"] in {"AVAILABLE", "REDUCED"}
    ]
    summary = {
        "economic_rankings_scanned": len(rows),
        "fresh_rows": len(fresh_rows),
        "source_evidence_ready_rows": len(source_ready_rows),
        "positive_ev_rows": len(positive_ev_rows),
        "clean_execution_rows": len(clean_execution_rows),
        "risk_ready_rows": len(risk_ready_rows),
        "risk_missing_rows": sum(1 for row in rows if row["phase3n_risk_state"] == "MISSING"),
        "preflight_ready_rows": len(preflight_candidates),
        "risk_preflight_enabled": risk_preflight,
        "phase3m_phase3n_preflight_attempted": len(preflight_results),
        "phase3m_phase3n_preflight_recorded": sum(
            1 for row in preflight_results if row.get("preflight_status") == "RECORDED"
        ),
        "blocked_rows": sum(1 for row in rows if row["blockers"]),
        "primary_gap": _primary_gap(rows, blocker_counts),
        "latest_ranked_at": rows[0]["ranked_at"] if rows else None,
        "live_demo_execution": "blocked",
        "order_submission_cancel_replace": "blocked",
    }
    summary["status"] = _status(summary)
    return summary


def _run_risk_preflight(
    session: Session,
    candidates: list[dict[str, Any]],
    *,
    settings: Settings,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for candidate in candidates:
        decision = _paper_decision_for_candidate(session, candidate, settings=settings)
        if decision is None:
            results.append(
                {
                    **candidate,
                    "preflight_status": "SKIPPED",
                    "preflight_reason": "latest ranking fields were unavailable",
                }
            )
            continue
        sized = ensure_paper_decision_sized(session, decision, settings=settings)
        raw = sized.raw_decision_json
        sizing = raw.get("position_sizing_decision") or {}
        risk = raw.get("advanced_risk_decision") or {}
        results.append(
            {
                **candidate,
                "preflight_status": "RECORDED",
                "phase3m_decision_id": raw.get("position_sizing_decision_id"),
                "phase3m_tier": sizing.get("tier"),
                "phase3m_proposed_contracts": sizing.get("proposed_contracts"),
                "phase3m_executed_contracts": sizing.get("executed_contracts"),
                "phase3n_decision_id": raw.get("advanced_risk_decision_id"),
                "phase3n_action": risk.get("action"),
                "phase3n_mode": risk.get("mode"),
                "phase3n_reason_codes": risk.get("reason_codes", []),
                "phase3n_hard_blocks": risk.get("hard_blocks", []),
                "preflight_reason": sized.reason,
            }
        )
    return results


def _paper_decision_for_candidate(
    session: Session,
    candidate: dict[str, Any],
    *,
    settings: Settings,
) -> PaperDecision | None:
    ticker = str(candidate.get("ticker") or "")
    ranking = _latest_ranking(session, ticker)
    if ranking is None or ranking.best_side not in {BUY_YES, BUY_NO}:
        return None
    probability = to_decimal(ranking.forecast_probability)
    price = to_decimal(ranking.best_price)
    edge = to_decimal(ranking.estimated_edge)
    if probability is None or price is None or edge is None:
        return None
    forecast = _latest_forecast(session, ticker)
    reason = (
        "Phase 3BD-R7 paper-only economic quality-gate risk preflight. "
        "No order submission, cancellation, replacement, or live/demo execution."
    )
    return PaperDecision(
        ticker=ticker,
        forecast_id=forecast.id if forecast is not None else None,
        model_name=MODEL_NAME,
        side=ranking.best_side,
        probability=probability,
        market_price=price,
        limit_price=price,
        edge=edge,
        quantity=settings.paper_max_order_quantity,
        reason=reason,
        raw_decision_json={
            "source": PHASE3BD_R7_VERSION,
            "risk_preflight_only": True,
            "execution_enabled": False,
            "execution_dry_run": True,
            "ticker": ticker,
            "ranking_id": ranking.id,
            "forecast_id": forecast.id if forecast is not None else None,
            "ranked_at": ranking.ranked_at.isoformat(),
            "quality_gate_status": candidate.get("preflight_ready"),
            "expected_value": candidate.get("expected_value"),
            "opportunity_score": candidate.get("opportunity_score"),
            "economic_evidence_state": candidate.get("economic_evidence_state"),
            "reason": reason,
        },
    )


def _preflight_settings(settings: Settings) -> Settings:
    paper_settings = learning_paper_settings(settings)
    return paper_settings.model_copy(
        update={
            "execution_enabled": False,
            "execution_dry_run": True,
            "overnight_run_demo": False,
            "autopilot_dry_run": True,
            "dynamic_position_sizing_mode": "shadow",
            "advanced_risk_engine_mode": "shadow",
        }
    )


def _latest_ranking(session: Session, ticker: str) -> MarketRanking | None:
    return session.scalar(
        select(MarketRanking)
        .where(MarketRanking.ticker == ticker, MarketRanking.forecast_model == MODEL_NAME)
        .order_by(desc(MarketRanking.ranked_at), desc(MarketRanking.id))
        .limit(1)
    )


def _latest_forecast(session: Session, ticker: str) -> Forecast | None:
    return session.scalar(
        select(Forecast)
        .where(Forecast.ticker == ticker, Forecast.model_name == MODEL_NAME)
        .order_by(desc(Forecast.forecasted_at), desc(Forecast.id))
        .limit(1)
    )


def _latest_snapshot(session: Session, ticker: str) -> MarketSnapshot | None:
    return session.scalar(
        select(MarketSnapshot)
        .where(MarketSnapshot.ticker == ticker)
        .order_by(desc(MarketSnapshot.captured_at), desc(MarketSnapshot.id))
        .limit(1)
    )


def _latest_event(session: Session, event_key: str) -> EconomicEvent | None:
    return session.scalar(
        select(EconomicEvent)
        .where(EconomicEvent.event_key == event_key)
        .order_by(desc(EconomicEvent.event_time), desc(EconomicEvent.id))
        .limit(1)
    )


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


def _risk_state(
    *,
    risk: dict[str, Any] | None,
    ranked_at: Any,
    now: Any,
) -> tuple[str, str]:
    if risk is None:
        return "MISSING", "Phase 3M/3N has not evaluated this economic candidate yet."
    decision_at = parse_datetime(risk.get("decision_timestamp"))
    if decision_at is not None and ranked_at is not None and decision_at < ranked_at:
        return "STALE", "Phase 3N decision predates the latest economic ranking."
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
    if ranked_at < forecast_at:
        return RANKING_BEFORE_FORECAST
    if (now - ranked_at).total_seconds() / 60 > freshness_minutes:
        return RANKING_STALE
    return FRESHNESS_OK


def _side_probability(ranking: MarketRanking) -> Decimal | None:
    yes_probability = to_decimal(ranking.forecast_probability)
    if yes_probability is None:
        return None
    if ranking.best_side == BUY_YES:
        return yes_probability
    if ranking.best_side == BUY_NO:
        return Decimal("1") - yes_probability
    return None


def _primary_gap(rows: list[dict[str, Any]], blocker_counts: Counter[str]) -> str:
    if not rows:
        return "NO_ECONOMIC_RANKINGS"
    for blocker in BLOCKER_ORDER:
        if blocker_counts.get(blocker, 0) > 0:
            return blocker
    if any(row["phase3n_risk_state"] == "MISSING" for row in rows):
        return "PREFLIGHT_READY_RISK_MISSING"
    return "NO_DOMINANT_BLOCKER"


def _status(summary: dict[str, Any]) -> str:
    if summary["economic_rankings_scanned"] == 0:
        return "NO_ECONOMIC_RANKINGS"
    if summary["fresh_rows"] == 0:
        return "WAITING_FOR_FRESH_ECONOMIC_RANKINGS"
    if summary["source_evidence_ready_rows"] == 0:
        return "WAITING_FOR_ACTUAL_CONSENSUS_EVIDENCE"
    if summary["positive_ev_rows"] == 0:
        return "WAITING_FOR_POSITIVE_EV"
    if summary["clean_execution_rows"] == 0:
        return "WAITING_FOR_CLEAN_EXECUTION"
    if summary["preflight_ready_rows"] > 0 and summary["phase3m_phase3n_preflight_recorded"] > 0:
        return "PREFLIGHT_RECORDED"
    if summary["preflight_ready_rows"] > 0:
        return "PREFLIGHT_READY"
    return "WAITING_FOR_QUALITY_GATES"


def _recommended_next_action(summary: dict[str, Any]) -> str:
    status = str(summary.get("status") or "")
    if status == "PREFLIGHT_READY":
        return (
            "Run Phase 3BD-R7 with --risk-preflight for these clean economic rows; "
            "this records paper-only Phase 3M/3N evidence and keeps execution blocked."
        )
    if status == "PREFLIGHT_RECORDED":
        return "Review the paper-only Phase 3M/3N results before any further economic action."
    if status == "WAITING_FOR_ACTUAL_CONSENSUS_EVIDENCE":
        return (
            "Run Phase 3BD-R5 around the next release window with a verified consensus "
            "source so economic_v1 has actual-vs-consensus evidence."
        )
    if status == "WAITING_FOR_FRESH_ECONOMIC_RANKINGS":
        return "Refresh economic features/forecasts/rankings before evaluating preflight quality."
    if status == "WAITING_FOR_POSITIVE_EV":
        return (
            "No economic paper preflight yet; wait for price/model movement to "
            "create positive EV."
        )
    if status == "WAITING_FOR_CLEAN_EXECUTION":
        return "Wait for executable spread/liquidity before economic risk preflight."
    return "Keep the economic release-window watch active and rerun R7 after the next refresh."


def _next_commands(summary: dict[str, Any]) -> list[str]:
    commands = [
        "kalshi-bot phase3bd-r5-consensus-feed-watch --output-dir reports/phase3bd_r5 --cycles 1",
        "kalshi-bot phase3bd-r7-economic-opportunity-quality-gate --output-dir reports/phase3bd_r7",
    ]
    if summary.get("status") == "PREFLIGHT_READY":
        commands.append(
            "kalshi-bot phase3bd-r7-economic-opportunity-quality-gate "
            "--output-dir reports/phase3bd_r7 --risk-preflight"
        )
    return commands


def _what_would_make_ready(
    blockers: list[str],
    thresholds: dict[str, Any],
) -> list[str]:
    actions: list[str] = []
    if MARKET_NOT_ACTIVE in blockers:
        actions.append("Market status must be active/open.")
    if SNAPSHOT_MISSING in blockers or SNAPSHOT_STALE in blockers:
        actions.append("Refresh the exact economic market snapshot.")
    if FORECAST_MISSING in blockers or FORECAST_STALE in blockers:
        actions.append("Refresh economic_v1 forecasts after feature evidence is current.")
    if RANKING_BEFORE_FORECAST in blockers or RANKING_STALE in blockers:
        actions.append("Rerank economic_v1 after the latest forecast.")
    if MISSING_CONSENSUS_EVIDENCE in blockers:
        actions.append("Attach a verified consensus source URL and consensus value.")
    if MISSING_ACTUAL_CONSENSUS_EVIDENCE in blockers:
        actions.append("Wait for/reload the released actual value alongside consensus.")
    if MISSING_EXECUTABLE_PRICE in blockers:
        actions.append("Need a current executable best price.")
    if EV_NOT_POSITIVE in blockers:
        actions.append(
            f"Expected value must exceed {thresholds['min_expected_value']}."
        )
    if EDGE_BELOW_THRESHOLD in blockers:
        actions.append(f"Model edge must be at least {thresholds['min_edge']}.")
    if SCORE_BELOW_THRESHOLD in blockers:
        actions.append(f"Opportunity score must be at least {thresholds['min_score']}.")
    if LIQUIDITY_BLOCKED in blockers:
        actions.append(
            f"Liquidity score must be at least {thresholds['min_liquidity_score']}."
        )
    if SPREAD_BLOCKED in blockers:
        actions.append(f"Spread must be at or below {thresholds['max_spread']}.")
    if RISK_BLOCKED in blockers:
        actions.append("Phase 3N must allow or reduce rather than block the row.")
    if not actions:
        actions.append("Run paper-only Phase 3M/3N preflight; execution remains blocked.")
    return _unique(actions)


def _source_url(raw: dict[str, Any]) -> str | None:
    for key in ("source_url", "url", "request_url"):
        value = raw.get(key)
        if value:
            return str(value)
    raw_row = raw.get("raw_row")
    if isinstance(raw_row, dict):
        for key in ("source_url", "url"):
            value = raw_row.get(key)
            if value:
                return str(value)
    return None


def _all_blockers(rows: list[dict[str, Any]]) -> list[str]:
    return [blocker for row in rows for blocker in row["blockers"]]


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _decode_list(value: str | None) -> list[Any]:
    decoded = json.loads(value or "[]")
    return decoded if isinstance(decoded, list) else []


def _age_minutes(value: Any, *, now: Any) -> str | None:
    if value is None:
        return None
    return decimal_to_str(
        Decimal(str((now - value).total_seconds() / 60)).quantize(Decimal("0.1"))
    )


def _cents(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return decimal_to_str((value * Decimal("100")).quantize(Decimal("0.1")))


def _row_sort_key(row: dict[str, Any]) -> tuple[Decimal, Decimal, Decimal]:
    return (
        to_decimal(row.get("expected_value")) or Decimal("-999"),
        to_decimal(row.get("opportunity_score")) or Decimal("0"),
        to_decimal(row.get("estimated_edge")) or Decimal("0"),
    )


def _render_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Phase 3BD-R7 Economic Opportunity Quality Gate",
        "",
        f"- Generated at: `{payload['generated_at']}`",
        f"- Mode: `{payload['mode']}`",
        "- PAPER ONLY: no live/demo execution and no order submission/cancel/replace.",
        "",
        "## Summary",
        "",
    ]
    for key, value in payload["summary"].items():
        lines.append(f"- {key}: `{value}`")
    lines.extend(["", "## Blockers", ""])
    for key, value in payload["blocker_counts"].items():
        lines.append(f"- {key}: `{value}`")
    lines.extend(["", "## Preflight Candidates", ""])
    for row in payload["preflight_candidates"][:10]:
        lines.append(
            f"- `{row['ticker']}` {row.get('title') or ''} "
            f"EV `{row.get('expected_value_cents')}` cents, "
            f"score `{row.get('opportunity_score')}`"
        )
    if not payload["preflight_candidates"]:
        lines.append("- none")
    lines.extend(
        [
            "",
            "## Recommended Next Action",
            "",
            payload["recommended_next_action"],
            "",
            "## Next Commands",
            "",
        ]
    )
    for command in payload["next_commands"]:
        lines.append(f"- `{command}`")
    return "\n".join(lines) + "\n"
