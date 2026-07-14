from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from kalshi_predictor.active_universe import is_inactive_market_status
from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.data.repositories import decode_json
from kalshi_predictor.data.schema import (
    ForecastSkipLog,
    LearningCycle,
    LearningOpportunity,
    LearningRejectionLog,
    LearningTradeTarget,
    Market,
    MarketRanking,
    MarketSnapshot,
    PaperPosition,
)
from kalshi_predictor.learning.config import learning_paper_settings
from kalshi_predictor.learning.duplicates import is_duplicate_candidate
from kalshi_predictor.learning.repository import recent_learning_rejections, row_to_dict
from kalshi_predictor.learning.safety import settled_paper_trade_count
from kalshi_predictor.learning.targets import (
    category_priority_score,
    learning_priority_score,
    settlement_speed_score,
)
from kalshi_predictor.phase3ak import (
    NOT_MULTILEG,
    build_multi_leg_component_provenance,
    phase3ak_learning_rejection_reason,
)
from kalshi_predictor.tournament.ranking import classify_market_category
from kalshi_predictor.utils.decimals import decimal_to_str, to_decimal
from kalshi_predictor.utils.time import utc_now


@dataclass(frozen=True)
class ThresholdAdvisor:
    current_min_edge: Decimal
    current_min_score: Decimal
    observed_top_score: Decimal | None
    opportunities_detected: int
    recommended_min_edge: Decimal
    recommended_min_score: Decimal
    expected_additional_paper_trades: int
    candidates_passing_current_thresholds: int
    candidates_passing_suggested_thresholds: int
    additional_candidates_available: int
    duplicate_blocked_additional_candidates: int
    position_blocked_additional_candidates: int
    safety_blocked_additional_candidates: int
    candidate_pool_size: int
    message: str
    next_action: str


def build_learning_diagnostics(
    session: Session,
    *,
    settings: Settings | None = None,
    rejection_limit: int = 100,
    scan_limit: int | None = None,
    suggest_thresholds: bool = True,
) -> dict[str, Any]:
    resolved_settings = settings or get_settings()
    learning_settings = learning_paper_settings(resolved_settings)
    resolved_scan_limit = scan_limit or learning_settings.learning_candidate_scan_limit
    latest_cycle = _latest_cycle(session)
    latest_summary = decode_json(latest_cycle.summary_json if latest_cycle else None)
    funnel = _funnel_summary(
        session,
        latest_cycle=latest_cycle,
        latest_summary=latest_summary,
    )
    candidates = _candidate_pool(
        session,
        settings=learning_settings,
        scan_limit=resolved_scan_limit,
    )
    rejections = recent_learning_rejections(
        session,
        limit=max(rejection_limit, resolved_scan_limit),
    )
    rejection_breakdown = _rejection_breakdown(rejections)
    bottleneck = _top_bottleneck(rejection_breakdown)
    advisor = threshold_advisor(
        session,
        settings=learning_settings,
        latest_cycle=latest_cycle,
        rejections=rejections,
        candidates=candidates,
        suggest_thresholds=suggest_thresholds,
    )
    categories = _category_breakdown(session, rejections)
    return {
        "generated_at": utc_now().isoformat(),
        "funnel": funnel,
        "funnel_help": _funnel_help(),
        "candidate_pool_size": len(candidates),
        "top_bottleneck": bottleneck,
        "bottleneck_banner": _bottleneck_banner(bottleneck),
        "bottleneck_next_action": _bottleneck_next_action(bottleneck),
        "rejection_breakdown": rejection_breakdown,
        "threshold_advisor": _advisor_to_dict(advisor),
        "category_breakdown": categories,
        "top_rejected_candidates": [_rejection_to_dict(row) for row in _top_rejections(rejections)],
        "top_usable_non_duplicate_candidates": _top_usable_candidates(
            candidates,
            settings=learning_settings,
            min_score=advisor.recommended_min_score,
            min_edge=advisor.recommended_min_edge,
            limit=20,
        ),
        "duplicate_cooldown": {
            "hours": learning_settings.learning_duplicate_cooldown_hours,
            "status": (
                "Active; ticker/model/side repeats are blocked inside the cooldown window."
            ),
        },
        "recommended_next_action": advisor.next_action,
        "current_thresholds": {
            "min_edge": decimal_to_str(advisor.current_min_edge),
            "min_score": decimal_to_str(advisor.current_min_score),
        },
    }


def threshold_advisor(
    session: Session,
    *,
    settings: Settings,
    latest_cycle: LearningCycle | None = None,
    rejections: list[LearningRejectionLog] | None = None,
    candidates: list[dict[str, Any]] | None = None,
    suggest_thresholds: bool = True,
) -> ThresholdAdvisor:
    current_min_edge = settings.learning_min_edge
    current_min_score = settings.learning_min_opportunity_score
    observed_top_score = _latest_top_score(session)
    opportunities_detected = latest_cycle.opportunities_found if latest_cycle else _count(
        session,
        LearningOpportunity,
    )
    recommended_score = current_min_score
    message = "Learning thresholds look usable for the latest stored rankings."
    candidate_rows = candidates or _candidate_pool(
        session,
        settings=settings,
        scan_limit=settings.learning_candidate_scan_limit,
    )
    if (
        suggest_thresholds
        and
        opportunities_detected == 0
        and current_min_score > Decimal("25")
        and (
            (
                observed_top_score is not None
                and observed_top_score < current_min_score
            )
            or _has_candidates_at_score(
                candidate_rows,
                settings=settings,
                min_score=Decimal("25"),
                min_edge=current_min_edge,
            )
        )
    ):
        recommended_score = _recommended_score_floor(observed_top_score)
        message = (
            "Learning mode is too strict. Lower min score from "
            f"{decimal_to_str(current_min_score)} to {decimal_to_str(recommended_score)}."
        )
    replay = _threshold_replay(
        candidate_rows,
        settings=settings,
        recommended_score=recommended_score,
        current_score=current_min_score,
        min_edge=current_min_edge,
    )
    expected = replay["expected_additional_paper_trades"]
    duplicate_rejections = sum(
        1 for row in (rejections or recent_learning_rejections(session, limit=500))
        if row.reason == "duplicate_trade"
    )
    if (
        recommended_score < current_min_score
        and expected == 0
        and replay["duplicate_blocked_additional_candidates"] > 0
    ):
        message = (
            "Lowering score will not help because duplicate protection is the current "
            "bottleneck."
        )
    elif (
        recommended_score < current_min_score
        and expected == 0
        and replay["safety_blocked_additional_candidates"] > 0
    ):
        message = (
            "Lowering score will not help because active-market or provenance safety "
            "gates block the threshold-eligible candidates."
        )
    elif duplicate_rejections and duplicate_rejections >= max(1, len(candidate_rows) // 10):
        message = (
            "Duplicate protection is limiting Learning Mode rotation. Scan deeper "
            "candidate pools and let cooldowns expire before lowering thresholds further."
        )
    if recommended_score < current_min_score:
        if expected:
            next_action = (
                "Run `LEARNING_MODE=true "
                f"LEARNING_MIN_OPPORTUNITY_SCORE={decimal_to_str(recommended_score)} "
                "LEARNING_CANDIDATE_SCAN_LIMIT=500 kalshi-bot learning-once`."
            )
        elif replay["safety_blocked_additional_candidates"] > 0:
            next_action = (
                "Run active-universe and Phase 3AK provenance diagnostics before lowering "
                "paper-learning thresholds further."
            )
        else:
            next_action = (
                "Scan deeper candidate pool and rotate into fresh markets before lowering "
                "thresholds."
            )
    else:
        next_action = "Scan deeper candidate pool and collect more settled outcomes."
    return ThresholdAdvisor(
        current_min_edge=current_min_edge,
        current_min_score=current_min_score,
        observed_top_score=observed_top_score,
        opportunities_detected=opportunities_detected,
        recommended_min_edge=current_min_edge,
        recommended_min_score=recommended_score,
        expected_additional_paper_trades=expected,
        candidates_passing_current_thresholds=replay["current_pass_count"],
        candidates_passing_suggested_thresholds=replay["suggested_pass_count"],
        additional_candidates_available=replay["additional_candidates_available"],
        duplicate_blocked_additional_candidates=replay["duplicate_blocked_additional_candidates"],
        position_blocked_additional_candidates=replay["position_blocked_additional_candidates"],
        safety_blocked_additional_candidates=replay["safety_blocked_additional_candidates"],
        candidate_pool_size=len(candidate_rows),
        message=message,
        next_action=next_action,
    )


def generate_learning_diagnostics_report(
    session: Session,
    *,
    output_path: str | Path = "reports/learning_diagnostics.md",
    settings: Settings | None = None,
    scan_limit: int | None = None,
    suggest_thresholds: bool = True,
) -> Path:
    diagnostics = build_learning_diagnostics(
        session,
        settings=settings,
        scan_limit=scan_limit,
        suggest_thresholds=suggest_thresholds,
    )
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_learning_diagnostics_report(diagnostics), encoding="utf-8")
    return output


def render_learning_diagnostics_report(diagnostics: dict[str, Any]) -> str:
    funnel = diagnostics["funnel"]
    advisor = diagnostics["threshold_advisor"]
    lines = [
        "# Learning Funnel Diagnostics",
        "",
        f"- Generated at: {diagnostics['generated_at']}",
        "- Mode: PAPER ONLY",
        "",
        "## Funnel Summary",
        "",
        f"- Markets scanned: {funnel['markets_scanned']}",
        f"- Snapshots available: {funnel['snapshots_available']}",
        f"- Forecasts generated: {funnel['forecasts_generated']}",
        f"- Forecasts skipped: {funnel['forecasts_skipped']}",
        f"- Rankings inserted: {funnel['rankings_inserted']}",
        f"- Opportunities detected: {funnel['opportunities_detected']}",
        f"- Learning candidates: {funnel['learning_candidates']}",
        f"- Paper trades created: {funnel['paper_trades_created']}",
        f"- Settled paper trades: {funnel['settled_paper_trades']}",
        f"- Candidate pool size: {diagnostics['candidate_pool_size']}",
        "",
        "## Top Bottleneck",
        "",
        f"- Main bottleneck: {diagnostics['top_bottleneck']['reason']}",
        f"- Count: {diagnostics['top_bottleneck']['count']}",
        f"- Recommended action: {diagnostics['bottleneck_next_action']}",
        "",
        "## Duplicate Cooldown",
        "",
        f"- Cooldown hours: {diagnostics['duplicate_cooldown']['hours']}",
        f"- Status: {diagnostics['duplicate_cooldown']['status']}",
        "",
        "## Rejection Breakdown",
        "",
        "| Reason | Count | Percent | Example ticker | Suggested fix |",
        "|---|---:|---:|---|---|",
    ]
    if diagnostics["rejection_breakdown"]:
        for row in diagnostics["rejection_breakdown"]:
            lines.append(
                "| "
                f"{row['reason']} | {row['count']} | {row['percent']} | "
                f"{row['example_ticker'] or ''} | {row['suggested_fix']} |"
            )
    else:
        lines.append("| _No learning rejections logged yet_ | 0 | 0% |  |  |")
    lines.extend(
        [
            "",
            "## Threshold Advisor",
            "",
            f"- Current min edge: {advisor['current_min_edge']}",
            f"- Current min score: {advisor['current_min_score']}",
            f"- Observed top score: {advisor['observed_top_score'] or 'n/a'}",
            f"- Opportunities detected: {advisor['opportunities_detected']}",
            (
                "- Candidates passing current thresholds: "
                f"{advisor['candidates_passing_current_thresholds']}"
            ),
            (
                "- Candidates passing suggested thresholds: "
                f"{advisor['candidates_passing_suggested_thresholds']}"
            ),
            (
                "- Additional candidates available: "
                f"{advisor['additional_candidates_available']}"
            ),
            (
                "- Duplicate-blocked additional candidates: "
                f"{advisor['duplicate_blocked_additional_candidates']}"
            ),
            (
                "- Position-blocked additional candidates: "
                f"{advisor['position_blocked_additional_candidates']}"
            ),
            (
                "- Safety-blocked additional candidates: "
                f"{advisor['safety_blocked_additional_candidates']}"
            ),
            f"- Recommended min edge: {advisor['recommended_min_edge']}",
            f"- Recommended min score: {advisor['recommended_min_score']}",
            (
                "- Expected additional paper trades: "
                f"{advisor['expected_additional_paper_trades']}"
            ),
            f"- Recommendation: {advisor['message']}",
            "",
            "## Category Breakdown",
            "",
            "| Category | Rejections |",
            "|---|---:|",
        ]
    )
    if diagnostics["category_breakdown"]:
        for row in diagnostics["category_breakdown"]:
            lines.append(f"| {row['category']} | {row['count']} |")
    else:
        lines.append("| _No category data yet_ | 0 |")
    lines.extend(
        [
            "",
            "## Top Rejected Candidates",
            "",
            "| Ticker | Model | Reason | Edge | Score | Spread | Liquidity | ETA Hours |",
            "|---|---|---|---:|---:|---:|---:|---:|",
        ]
    )
    if diagnostics["top_rejected_candidates"]:
        for row in diagnostics["top_rejected_candidates"]:
            lines.append(
                "| "
                f"{row['ticker']} | {row['model_name']} | {row['reason']} | "
                f"{row['edge'] or ''} | {row['opportunity_score'] or ''} | "
                f"{row['spread'] or ''} | {row['liquidity'] or ''} | "
                f"{row['settlement_eta_hours'] or ''} |"
            )
    else:
        lines.append("| _No rejected candidates yet_ |  |  |  |  |  |  |  |")
    lines.extend(
        [
            "",
            "## Top 20 Usable Non-Duplicate Candidates",
            "",
            "| Ticker | Model | Side | Edge | Score | Priority | ETA Hours | Category |",
            "|---|---|---|---:|---:|---:|---:|---|",
        ]
    )
    if diagnostics["top_usable_non_duplicate_candidates"]:
        for row in diagnostics["top_usable_non_duplicate_candidates"]:
            lines.append(
                "| "
                f"{row['ticker']} | {row['model_name']} | {row['side'] or ''} | "
                f"{row['edge'] or ''} | {row['opportunity_score'] or ''} | "
                f"{row['learning_priority_score'] or ''} | "
                f"{row['settlement_eta_hours'] or ''} | {row['category']} |"
            )
    else:
        lines.append("| _No currently usable non-duplicate candidates_ |  |  |  |  |  |  |  |")
    lines.extend(
        [
            "",
            "## Recommended Next Action",
            "",
            diagnostics["recommended_next_action"],
            "",
        ]
    )
    return "\n".join(lines)


def _funnel_summary(
    session: Session,
    *,
    latest_cycle: LearningCycle | None,
    latest_summary: dict[str, Any],
) -> dict[str, int]:
    steps = latest_summary.get("steps") or {}
    collect_step = steps.get("collect_markets") or {}
    forecast_step = steps.get("forecast_all") or {}
    find_step = steps.get("find_opportunities") or {}
    paper_step = steps.get("paper_run") or {}
    latest_forecast_skips = _int(collect_step.get("skipped_forecasts")) + _int(
        forecast_step.get("skipped")
    )
    return {
        "markets_scanned": _int(
            (latest_cycle.markets_scanned if latest_cycle else 0)
            or collect_step.get("markets_seen")
            or find_step.get("markets_scanned")
        ),
        "snapshots_available": _int(collect_step.get("snapshots_inserted"))
        or _count(session, MarketSnapshot),
        "forecasts_generated": _int(
            (latest_cycle.forecasts_generated if latest_cycle else 0)
            or (
                _int(collect_step.get("forecasts_inserted"))
                + _int(forecast_step.get("forecasts_inserted"))
            )
        ),
        "forecasts_skipped": latest_forecast_skips
        if latest_cycle is not None or latest_forecast_skips
        else _count(session, ForecastSkipLog),
        "rankings_inserted": _int(find_step.get("rankings_inserted"))
        or _count(session, MarketRanking),
        "opportunities_detected": _int(
            (latest_cycle.opportunities_found if latest_cycle else 0)
            or find_step.get("opportunities_detected")
        ),
        "learning_candidates": _int(paper_step.get("learning_candidates_scanned"))
        or _int(find_step.get("learning_opportunities_inserted"))
        or _int((steps.get("generate_targets") or {}).get("targets_inserted"))
        or _count(session, LearningTradeTarget),
        "paper_trades_created": _int(
            (latest_cycle.paper_trades_created if latest_cycle else 0)
            or paper_step.get("orders_created")
        ),
        "settled_paper_trades": settled_paper_trade_count(session),
    }


def _latest_cycle(session: Session) -> LearningCycle | None:
    return session.scalar(
        select(LearningCycle)
        .order_by(desc(LearningCycle.started_at), desc(LearningCycle.id))
        .limit(1)
    )


def _latest_top_score(session: Session) -> Decimal | None:
    latest_ranked_at = session.scalar(
        select(MarketRanking.ranked_at)
        .order_by(desc(MarketRanking.ranked_at), desc(MarketRanking.id))
        .limit(1)
    )
    if latest_ranked_at is None:
        return None
    scores = session.scalars(
        select(MarketRanking.opportunity_score).where(
            MarketRanking.ranked_at == latest_ranked_at
        )
    )
    decimal_scores = [
        score for score in (to_decimal(value) for value in scores) if score is not None
    ]
    return max(decimal_scores, default=None)


def _recommended_score_floor(observed_top_score: Decimal) -> Decimal:
    del observed_top_score
    return Decimal("25")


def _expected_additional_trades(
    rejections: list[LearningRejectionLog],
    *,
    recommended_score: Decimal,
    current_score: Decimal,
    min_edge: Decimal,
) -> int:
    if recommended_score >= current_score:
        return 0
    total = 0
    for row in rejections:
        score = to_decimal(row.opportunity_score) or Decimal("0")
        edge = to_decimal(row.edge) or Decimal("0")
        if row.reason == "low_score" and recommended_score <= score < current_score:
            total += 1
        elif row.reason == "low_edge" and edge >= min_edge:
            total += 1
    return total


def _candidate_pool(
    session: Session,
    *,
    settings: Settings,
    scan_limit: int,
) -> list[dict[str, Any]]:
    rankings = _latest_candidate_rankings(session, scan_limit=scan_limit)
    market_statuses = _market_status_by_ticker(session, [ranking.ticker for ranking in rankings])
    phase3ak_gates = _phase3ak_gates_for_rankings(session, rankings)
    candidates = [
        _candidate_from_ranking(
            session,
            ranking,
            settings=settings,
            market_status=market_statuses.get(ranking.ticker),
            phase3ak_gate=phase3ak_gates.get(ranking.ticker),
        )
        for ranking in rankings
    ]
    candidates.sort(
        key=lambda row: to_decimal(row["learning_priority_score"]) or Decimal("0"),
        reverse=True,
    )
    return candidates[:scan_limit]


def _latest_candidate_rankings(session: Session, *, scan_limit: int) -> list[MarketRanking]:
    rows = session.scalars(
        select(MarketRanking)
        .order_by(desc(MarketRanking.ranked_at), desc(MarketRanking.id))
        .limit(max(scan_limit * 5, scan_limit))
    )
    latest: dict[tuple[str, str], MarketRanking] = {}
    for ranking in rows:
        key = (ranking.ticker, ranking.forecast_model)
        if key not in latest:
            latest[key] = ranking
    return list(latest.values())


def _candidate_from_ranking(
    session: Session,
    ranking: MarketRanking,
    *,
    settings: Settings,
    market_status: str | None = None,
    phase3ak_gate: dict[str, Any] | None = None,
) -> dict[str, Any]:
    text = " ".join(
        part for part in (ranking.title, ranking.series_ticker, ranking.event_ticker) if part
    )
    category = classify_market_category(text or ranking.ticker)
    minutes = to_decimal(ranking.time_to_close_minutes)
    speed = settlement_speed_score(minutes)
    priority = learning_priority_score(
        edge=to_decimal(ranking.estimated_edge),
        opportunity_score=to_decimal(ranking.opportunity_score),
        confidence_score=to_decimal(ranking.model_confidence_score),
        liquidity_score=to_decimal(ranking.liquidity_score),
        speed_score=speed,
        category_score=category_priority_score(
            category=category,
            market_text=text,
            minutes_to_close=minutes,
        ),
    )
    side = ranking.best_side
    duplicate = (
        bool(side)
        and is_duplicate_candidate(
            session,
            ticker=ranking.ticker,
            model_name=ranking.forecast_model,
            side=side,
            cooldown_hours=settings.learning_duplicate_cooldown_hours,
        )
    )
    position_limited = _position_limited(
        session,
        ticker=ranking.ticker,
        side=side,
        settings=settings,
    )
    resolved_market_status = market_status or ranking.status
    inactive_market = is_inactive_market_status(resolved_market_status)
    phase3ak_blocked = (
        phase3ak_gate is not None
        and phase3ak_gate.get("status") != NOT_MULTILEG
        and not bool(phase3ak_gate.get("eligible"))
    )
    return {
        "ticker": ranking.ticker,
        "model_name": ranking.forecast_model,
        "side": side,
        "price": ranking.best_price,
        "edge_decimal": to_decimal(ranking.estimated_edge),
        "score_decimal": to_decimal(ranking.opportunity_score),
        "spread_decimal": to_decimal(ranking.spread),
        "liquidity_decimal": to_decimal(ranking.liquidity) or Decimal("0"),
        "settlement_eta_hours_decimal": (
            minutes / Decimal("60") if minutes is not None else None
        ),
        "learning_priority_decimal": priority,
        "edge": decimal_to_str(to_decimal(ranking.estimated_edge)),
        "opportunity_score": decimal_to_str(to_decimal(ranking.opportunity_score)),
        "spread": decimal_to_str(to_decimal(ranking.spread)),
        "liquidity": decimal_to_str(to_decimal(ranking.liquidity)),
        "settlement_eta_hours": decimal_to_str(
            minutes / Decimal("60") if minutes is not None else None
        ),
        "learning_priority_score": decimal_to_str(priority),
        "category": category,
        "duplicate_trade": duplicate,
        "position_limited": position_limited,
        "inactive_market": inactive_market,
        "market_status": resolved_market_status,
        "phase3ak_blocked": phase3ak_blocked,
        "phase3ak_reason": (
            phase3ak_learning_rejection_reason(phase3ak_gate or {})
            if phase3ak_blocked
            else None
        ),
        "ranking_id": ranking.id,
    }


def _market_status_by_ticker(session: Session, tickers: list[str]) -> dict[str, str | None]:
    if not tickers:
        return {}
    rows = session.execute(
        select(Market.ticker, Market.status).where(Market.ticker.in_(set(tickers)))
    )
    return {ticker: status for ticker, status in rows}


def _phase3ak_gates_for_rankings(
    session: Session,
    rankings: list[MarketRanking],
) -> dict[str, dict[str, Any]]:
    tickers = list(dict.fromkeys(ranking.ticker for ranking in rankings))
    if not tickers:
        return {}
    payload = build_multi_leg_component_provenance(
        session,
        tickers=tickers,
        include_single_leg=True,
    )
    gates: dict[str, dict[str, Any]] = {}
    for row in payload.get("rows", []):
        ticker = str(row.get("ticker") or "")
        if not ticker:
            continue
        if not row.get("is_multi_leg"):
            gates[ticker] = {
                "ticker": ticker,
                "status": NOT_MULTILEG,
                "eligible": True,
                "reason": "single_leg_or_non_sports_market",
            }
            continue
        gates[ticker] = {
            "ticker": ticker,
            "status": row.get("learning_eligibility"),
            "eligible": bool(row.get("learning_eligible")),
            "reason": row.get("blocking_reason"),
            "component_status_counts": row.get("component_status_counts", {}),
            "snapshot_status": row.get("snapshot_status", {}),
        }
    for ticker in tickers:
        gates.setdefault(
            ticker,
            {
                "ticker": ticker,
                "status": NOT_MULTILEG,
                "eligible": True,
                "reason": "no_multi_leg_sports_components",
            },
        )
    return gates


def _threshold_replay(
    candidates: list[dict[str, Any]],
    *,
    settings: Settings,
    recommended_score: Decimal,
    current_score: Decimal,
    min_edge: Decimal,
) -> dict[str, int]:
    current_pass = [
        candidate
        for candidate in candidates
        if _candidate_passes_thresholds(
            candidate,
            min_score=current_score,
            min_edge=min_edge,
            settings=settings,
        )
    ]
    suggested_pass = [
        candidate
        for candidate in candidates
        if _candidate_passes_thresholds(
            candidate,
            min_score=recommended_score,
            min_edge=min_edge,
            settings=settings,
        )
    ]
    suggested_threshold_only = [
        candidate
        for candidate in candidates
        if _candidate_meets_market_thresholds(
            candidate,
            min_score=recommended_score,
            min_edge=min_edge,
            settings=settings,
        )
    ]
    current_keys = {
        (candidate["ticker"], candidate["model_name"], candidate.get("side"))
        for candidate in current_pass
    }
    additional = [
        candidate
        for candidate in suggested_pass
        if (candidate["ticker"], candidate["model_name"], candidate.get("side"))
        not in current_keys
    ]
    threshold_only_additional = [
        candidate
        for candidate in suggested_threshold_only
        if (candidate["ticker"], candidate["model_name"], candidate.get("side"))
        not in current_keys
    ]
    duplicate_blocked = sum(1 for candidate in additional if candidate["duplicate_trade"])
    position_blocked = sum(1 for candidate in additional if candidate["position_limited"])
    safety_blocked = sum(
        1
        for candidate in threshold_only_additional
        if candidate.get("inactive_market") or candidate.get("phase3ak_blocked")
    )
    expected = sum(
        1
        for candidate in additional
        if not candidate["duplicate_trade"] and not candidate["position_limited"]
    )
    return {
        "current_pass_count": len(current_pass),
        "suggested_pass_count": len(suggested_pass),
        "additional_candidates_available": len(additional),
        "duplicate_blocked_additional_candidates": duplicate_blocked,
        "position_blocked_additional_candidates": position_blocked,
        "safety_blocked_additional_candidates": safety_blocked,
        "expected_additional_paper_trades": min(
            expected,
            max(1, settings.learning_target_trades_per_cycle),
        ),
    }


def _has_candidates_at_score(
    candidates: list[dict[str, Any]],
    *,
    settings: Settings,
    min_score: Decimal,
    min_edge: Decimal,
) -> bool:
    return any(
        _candidate_passes_thresholds(
            candidate,
            min_score=min_score,
            min_edge=min_edge,
            settings=settings,
        )
        for candidate in candidates
    )


def _candidate_passes_thresholds(
    candidate: dict[str, Any],
    *,
    min_score: Decimal,
    min_edge: Decimal,
    settings: Settings,
) -> bool:
    return (
        _candidate_meets_market_thresholds(
            candidate,
            min_score=min_score,
            min_edge=min_edge,
            settings=settings,
        )
        and not candidate.get("inactive_market")
        and not candidate.get("phase3ak_blocked")
    )


def _candidate_meets_market_thresholds(
    candidate: dict[str, Any],
    *,
    min_score: Decimal,
    min_edge: Decimal,
    settings: Settings,
) -> bool:
    edge = candidate.get("edge_decimal") or Decimal("0")
    score = candidate.get("score_decimal") or Decimal("0")
    spread = candidate.get("spread_decimal")
    liquidity = candidate.get("liquidity_decimal") or Decimal("0")
    return (
        bool(candidate.get("side"))
        and bool(candidate.get("price"))
        and edge >= min_edge
        and score >= min_score
        and (spread is None or spread <= settings.learning_max_spread)
        and liquidity >= settings.learning_min_liquidity
    )


def _position_limited(
    session: Session,
    *,
    ticker: str,
    side: str | None,
    settings: Settings,
) -> bool:
    if not side:
        return False
    position = session.get(PaperPosition, ticker)
    if position is None:
        return False
    current = position.yes_contracts if side == "BUY_YES" else position.no_contracts
    return current + settings.paper_max_order_quantity > settings.paper_max_position_per_market


def _top_usable_candidates(
    candidates: list[dict[str, Any]],
    *,
    settings: Settings,
    min_score: Decimal,
    min_edge: Decimal,
    limit: int,
) -> list[dict[str, Any]]:
    rows = [
        candidate
        for candidate in candidates
        if _candidate_passes_thresholds(
            candidate,
            min_score=min_score,
            min_edge=min_edge,
            settings=settings,
        )
        and not candidate["duplicate_trade"]
        and not candidate["position_limited"]
    ]
    return rows[:limit]


def _top_bottleneck(breakdown: list[dict[str, Any]]) -> dict[str, Any]:
    if not breakdown:
        return {"reason": "none", "count": 0, "suggested_fix": "Collect another cycle."}
    return breakdown[0]


def _bottleneck_banner(bottleneck: dict[str, Any]) -> str:
    reason = bottleneck.get("reason")
    if reason == "duplicate_trade":
        return (
            "Main bottleneck: duplicate_trade. The bot is finding candidates but "
            "repeatedly hitting markets it already paper-traded."
        )
    if reason == "low_score":
        return "Main bottleneck: low_score. The learning opportunity score gate is too strict."
    if reason == "low_edge":
        return "Main bottleneck: low_edge. Forecast edges are below the learning threshold."
    if reason and reason != "none":
        return f"Main bottleneck: {reason}."
    return "No dominant learning bottleneck has been detected yet."


def _bottleneck_next_action(bottleneck: dict[str, Any]) -> str:
    reason = bottleneck.get("reason")
    if reason == "duplicate_trade":
        return "Scan deeper candidate pool and rotate into fresh markets."
    if reason == "low_score":
        return "Use suggested thresholds only for paper-learning cycles, then compare outcomes."
    if reason == "low_edge":
        return "Collect fresher forecasts and review model calibration before lowering edge."
    return bottleneck.get("suggested_fix") or "Run another paper-only learning cycle."


def _suggested_fix(reason: str) -> str:
    suggestions = {
        "low_edge": "Collect fresh forecasts or lower edge only in paper-learning mode.",
        "low_score": "Use the threshold advisor and lower score only for paper learning.",
        "wide_spread": "Prefer tighter markets or wait for spreads to improve.",
        "low_liquidity": "Prefer markets with more available liquidity.",
        "stale_data": "Refresh market snapshots before running learning.",
        "duplicate_trade": (
            "Increase candidate scan depth, wait for cooldown, or reduce duplicate cooldown "
            "only in paper-learning mode."
        ),
        "position_limit": "Wait for settlement or raise paper-only per-market caps carefully.",
        "daily_cap": "Wait until tomorrow or raise the paper-only daily learning cap.",
        "confidence_too_low": "Collect more settled paper outcomes for model confidence.",
        "settlement_too_slow": (
            "Prioritize crypto, weather, economic, and short-dated general markets."
        ),
        "multi_leg_component_not_verified": (
            "Keep multi-leg sports markets out of learning until every component leg "
            "has verified or derived provenance."
        ),
        "multi_leg_component_ambiguous": (
            "Add sports aliases or verified schedule/team provenance before learning."
        ),
        "multi_leg_missing_market_snapshot": "Run snapshot-coverage-repair for this market.",
        "multi_leg_missing_price": "Collect orderbooks before allowing this multi-leg market.",
        "multi_leg_missing_spread": "Repair snapshot spread data before learning.",
        "multi_leg_missing_liquidity": "Repair or skip markets with no usable liquidity.",
        "missing_price": "Collect orderbooks so YES/NO ask prices are available.",
        "missing_forecast": "Run forecast --model all before learning.",
    }
    return suggestions.get(reason, "Inspect the candidate and latest learning cycle logs.")


def _percent(count: int, total: int) -> str:
    if total <= 0:
        return "0%"
    return f"{(count / total) * 100:.1f}%"


def _funnel_help() -> dict[str, str]:
    return {
        "markets_scanned": "Markets evaluated in the latest learning diagnostic window.",
        "snapshots_available": "Fresh snapshots inserted or available for the current scan.",
        "forecasts_generated": "Forecasts newly generated or available for the current scan.",
        "forecasts_skipped": "Forecast attempts skipped in the latest cycle/window.",
        "rankings_inserted": "Market ranking rows created by the latest opportunity scan.",
        "opportunities_detected": "Candidates passing opportunity thresholds before paper filters.",
        "learning_candidates": "Candidates Learning Mode considered before final safety filters.",
        "paper_trades_created": "Actual new paper orders/fills created by Learning Mode.",
        "settled_paper_trades": "Filled paper trades with matching settlement outcomes.",
    }


def _rejection_breakdown(rejections: list[LearningRejectionLog]) -> list[dict[str, Any]]:
    counts = Counter(row.reason for row in rejections)
    examples: dict[str, str] = {}
    for row in rejections:
        examples.setdefault(row.reason, row.ticker)
    total = sum(counts.values())
    return [
        {
            "reason": reason,
            "count": count,
            "percent": _percent(count, total),
            "example_ticker": examples.get(reason),
            "suggested_fix": _suggested_fix(reason),
        }
        for reason, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    ]


def _category_breakdown(
    session: Session,
    rejections: list[LearningRejectionLog],
) -> list[dict[str, Any]]:
    titles = {
        row.ticker: " ".join(
            part for part in (row.title, row.series_ticker, row.event_ticker) if part
        )
        for row in session.scalars(select(MarketRanking))
    }
    counts: Counter[str] = Counter()
    for rejection in rejections:
        raw = decode_json(rejection.raw_json)
        category = raw.get("category")
        if not category:
            category = classify_market_category(titles.get(rejection.ticker, rejection.ticker))
        counts[str(category or "unknown")] += 1
    return [
        {"category": category, "count": count}
        for category, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    ]


def _top_rejections(rejections: list[LearningRejectionLog]) -> list[LearningRejectionLog]:
    return sorted(
        rejections,
        key=lambda row: (
            to_decimal(row.opportunity_score) or Decimal("0"),
            to_decimal(row.edge) or Decimal("0"),
        ),
        reverse=True,
    )[:10]


def _rejection_to_dict(row: LearningRejectionLog) -> dict[str, Any]:
    data = row_to_dict(row) or {}
    return data


def _advisor_to_dict(advisor: ThresholdAdvisor) -> dict[str, Any]:
    return {
        "current_min_edge": decimal_to_str(advisor.current_min_edge),
        "current_min_score": decimal_to_str(advisor.current_min_score),
        "observed_top_score": decimal_to_str(advisor.observed_top_score),
        "opportunities_detected": advisor.opportunities_detected,
        "recommended_min_edge": decimal_to_str(advisor.recommended_min_edge),
        "recommended_min_score": decimal_to_str(advisor.recommended_min_score),
        "expected_additional_paper_trades": advisor.expected_additional_paper_trades,
        "candidates_passing_current_thresholds": (
            advisor.candidates_passing_current_thresholds
        ),
        "candidates_passing_suggested_thresholds": (
            advisor.candidates_passing_suggested_thresholds
        ),
        "additional_candidates_available": advisor.additional_candidates_available,
        "duplicate_blocked_additional_candidates": (
            advisor.duplicate_blocked_additional_candidates
        ),
        "position_blocked_additional_candidates": (
            advisor.position_blocked_additional_candidates
        ),
        "safety_blocked_additional_candidates": (
            advisor.safety_blocked_additional_candidates
        ),
        "candidate_pool_size": advisor.candidate_pool_size,
        "message": advisor.message,
        "next_action": advisor.next_action,
    }


def _count(session: Session, model: type) -> int:
    return int(session.scalar(select(func.count()).select_from(model)) or 0)


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
