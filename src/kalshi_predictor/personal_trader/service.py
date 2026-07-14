from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.data.repositories import decode_json
from kalshi_predictor.data.schema import (
    AdvancedRiskDecisionLog,
    Forecast,
    Market,
    MarketRanking,
    MarketSnapshot,
    PersonalTraderRecommendationMemory,
    PositionSizingDecisionLog,
    RlPolicyDecision,
    SyntheticMarketRun,
)
from kalshi_predictor.personal_trader.contracts import (
    API_SCHEMA_VERSION,
    BRIEF_SCHEMA_VERSION,
    CARD_SCHEMA_VERSION,
    EVENT_BRIEF_ISSUED,
    EVENT_BRIEF_REQUESTED,
    EVENT_CANDIDATE_EVALUATED,
    EVENT_CANDIDATE_RANKED,
    EVENT_CANDIDATE_REJECTED,
    EVENT_QUERY_NORMALIZED,
    EVENT_SNAPSHOT_CAPTURED,
    METRIC_CATALOG_VERSION,
    MODE_DISABLED,
    MODE_LIVE_ADVISORY,
    MODE_PAPER_ADVISORY,
    MODE_SHADOW,
    READ_ONLY_BOUNDARY,
    REJECTION_CATEGORIES,
    STATUS_ACTIONABLE,
    STATUS_REJECTED,
    STATUS_SYNTHETIC,
    STATUS_WATCHLIST,
    PersonalTraderConfig,
    PersonalTraderQuery,
    canonical_json,
    config_from_settings,
    decimal_string,
    event_id,
    scope_hash,
    stable_hash,
    stable_id,
)
from kalshi_predictor.ui.market_display import classify_market_category
from kalshi_predictor.utils.decimals import ONE, ZERO, to_decimal
from kalshi_predictor.utils.time import parse_datetime, utc_now


@dataclass
class CandidateEvaluation:
    ranking: MarketRanking
    market: Market | None
    snapshot: MarketSnapshot | None
    forecast: Forecast | None
    sizing: PositionSizingDecisionLog | None
    risk: AdvancedRiskDecisionLog | None
    phase_3s: RlPolicyDecision | None
    category: str
    candidate_id: str
    side: str
    price: Decimal | None
    model_probability: Decimal | None
    market_probability: Decimal | None
    raw_edge: Decimal | None
    spread: Decimal | None
    costs: Decimal | None
    net_ev: Decimal | None
    expected_roi: Decimal | None
    risk_adjusted_ev: Decimal | None
    risk_adjusted_ev_lcb: Decimal | None
    approved_quantity: int
    phase_3m_quantity: int
    phase_3n_decision: str
    rejection_codes: list[str] = field(default_factory=list)
    warning_codes: list[str] = field(default_factory=list)
    ranking_components: dict[str, Decimal] = field(default_factory=dict)
    standalone_rank: int | None = None
    slate_rank: int | None = None

    @property
    def actionable(self) -> bool:
        return not self.rejection_codes and self.approved_quantity > 0


def normalize_personal_trader_query(
    *,
    natural_language_query: str = "What should I trade today?",
    settings: Settings | None = None,
    requested_at: datetime | None = None,
    as_of: datetime | str | None = None,
    timezone: str | None = None,
    maximum_recommendations: int | None = None,
    category_include: list[str] | tuple[str, ...] | None = None,
    category_exclude: list[str] | tuple[str, ...] | None = None,
    market_include: list[str] | tuple[str, ...] | None = None,
    market_exclude: list[str] | tuple[str, ...] | None = None,
    principal_id: str = "local-user",
    account_scope: str = "paper-account",
    portfolio_scope: str = "paper-portfolio",
    execution_mode: str | None = None,
    include_watchlist: bool = True,
    include_synthetic_research: bool = True,
    response_detail_level: str = "concise",
    locale: str = "en-US",
) -> PersonalTraderQuery:
    resolved_settings = settings or get_settings()
    config = config_from_settings(resolved_settings)
    requested = requested_at or utc_now()
    requested_as_of = parse_datetime(as_of) or requested
    tz_name = timezone or config.default_timezone
    tz = _zoneinfo(tz_name)
    local_as_of = requested_as_of.astimezone(tz)
    day_start_local = local_as_of.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end_local = day_start_local + timedelta(days=1)
    capped_max = _cap_recommendations(maximum_recommendations, config=config)
    natural_query = natural_language_query.strip() or "What should I trade today?"
    return PersonalTraderQuery(
        query_id=stable_id(natural_query, requested.isoformat(), prefix="query"),
        requested_at=requested,
        principal_id=principal_id,
        account_scope=account_scope,
        portfolio_scope=portfolio_scope,
        timezone=tz_name,
        natural_language_query=natural_query,
        normalized_intent="RANK_TODAYS_OPPORTUNITIES",
        requested_as_of=requested_as_of,
        resolved_day_start=day_start_local.astimezone(UTC),
        resolved_day_end=day_end_local.astimezone(UTC),
        relative_time_expression="today" if "today" in natural_query.lower() else None,
        execution_mode=(execution_mode or config.mode).upper(),
        maximum_recommendations=capped_max,
        category_include=tuple(category_include or ()),
        category_exclude=tuple(category_exclude or ()),
        market_include=tuple(market_include or ()),
        market_exclude=tuple(market_exclude or ()),
        risk_preference_override="NONE",
        include_watchlist=include_watchlist,
        include_synthetic_research=include_synthetic_research,
        response_detail_level=response_detail_level,
        locale=locale,
        profile_version="profile-local-v1",
    )


def build_personal_trade_brief(
    session: Session,
    *,
    settings: Settings | None = None,
    natural_language_query: str = "What should I trade today?",
    as_of: datetime | str | None = None,
    timezone: str | None = None,
    maximum_recommendations: int | None = None,
    category_include: list[str] | tuple[str, ...] | None = None,
    category_exclude: list[str] | tuple[str, ...] | None = None,
    market_include: list[str] | tuple[str, ...] | None = None,
    market_exclude: list[str] | tuple[str, ...] | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    resolved_settings = settings or get_settings()
    config = config_from_settings(resolved_settings)
    generated_at = utc_now()
    query = normalize_personal_trader_query(
        natural_language_query=natural_language_query,
        settings=resolved_settings,
        requested_at=generated_at,
        as_of=as_of,
        timezone=timezone,
        maximum_recommendations=maximum_recommendations,
        category_include=category_include,
        category_exclude=category_exclude,
        market_include=market_include,
        market_exclude=market_exclude,
        execution_mode=config.mode,
    )
    source_health = _source_health(session, config=config, generated_at=generated_at)
    public_source_health = _public_source_health(source_health)
    snapshot_id = stable_id(
        query.query_id,
        query.requested_as_of.isoformat(),
        _source_signature(source_health),
        prefix="snapshot",
    )
    evaluations = _evaluate_candidates(
        session,
        config=config,
        query=query,
        source_health=source_health,
        snapshot_id=snapshot_id,
        generated_at=generated_at,
    )
    selected = _select_slate(evaluations, query=query)
    cards = [
        _recommendation_card(
            item,
            config=config,
            query=query,
            snapshot_id=snapshot_id,
            generated_at=generated_at,
        )
        for item in selected
    ]
    watchlist = _watchlist(evaluations, selected=selected, query=query)
    rejection_summary = _rejection_summary(evaluations)
    no_trade = _no_trade_payload(config=config, evaluations=evaluations, recommendations=cards)
    brief_id = stable_id(
        query.query_id,
        snapshot_id,
        canonical_json([card["recommendation_id"] for card in cards]),
        canonical_json(rejection_summary),
        prefix="brief",
    )
    brief = {
        "schema_version": BRIEF_SCHEMA_VERSION,
        "api_schema_version": API_SCHEMA_VERSION,
        "brief_id": brief_id,
        "query": _query_payload(query),
        "generated_at": generated_at.isoformat(),
        "as_of": query.requested_as_of.isoformat(),
        "timezone": query.timezone,
        "environment": _environment(resolved_settings),
        "execution_mode": _execution_mode(config),
        "profile_version": query.profile_version,
        "account_scope": query.account_scope,
        "portfolio_scope": query.portfolio_scope,
        "snapshot": {
            "snapshot_id": snapshot_id,
            "consistency_grade": _consistency_grade(source_health),
            "maximum_source_skew_ms": _maximum_source_skew_ms(source_health),
            "source_watermarks": {
                row["source"]: row["as_of"] for row in public_source_health if row["as_of"]
            }
            or {"sources": "UNAVAILABLE"},
        },
        "ranking_policy_version": config.ranking_policy_version,
        "summary": {
            "markets_scanned": _count_markets(session),
            "candidates_considered": len(evaluations),
            "eligible_count": len([item for item in evaluations if item.actionable]),
            "recommended_count": len(cards),
            "message": _summary_message(cards, no_trade),
        },
        "portfolio_risk": _portfolio_risk_payload(evaluations),
        "source_health": public_source_health,
        "recommendations": cards,
        "watchlist": watchlist,
        "rejection_summary": rejection_summary,
        "no_trade": no_trade,
        "next_recheck_at": (generated_at + timedelta(seconds=config.max_advisory_lifetime_seconds))
        .isoformat(),
        "disclosures": [
            "This is an advisory snapshot, not an order.",
            "Phase 3U cannot create, submit, cancel, replace, or route orders.",
            "Prices, liquidity, forecasts, and Phase 3N risk headroom can change.",
            "Paper, live, replay, synthetic, and research evidence remain separate.",
        ],
        "lineage": {
            "phase_3m_policy_version": _latest_sizing_version(evaluations),
            "phase_3n_policy_version": _latest_risk_version(evaluations),
            "phase_3s_policy_version": _latest_phase_3s_version(evaluations),
            "phase_3q_feature_registry_version": "phase_3q_registry_read_only",
            "metric_catalog_version": METRIC_CATALOG_VERSION,
            "explanation_policy_version": config.explanation_policy_version,
        },
        "illustrative": False,
    }
    if persist and _should_persist(config):
        persist_recommendation_memory(
            session,
            brief=brief,
            query=query,
            evaluations=evaluations,
        )
    return brief


def conversational_response(brief: dict[str, Any]) -> str:
    lines = [
        "Today's Trade Brief",
        f"As of: {brief['as_of']} {brief['timezone']}",
        f"Mode: {brief['execution_mode']}",
        (
            "Coverage: "
            f"{brief['summary']['eligible_count']} eligible of "
            f"{brief['summary']['candidates_considered']} candidates"
        ),
        f"Portfolio risk: {brief['portfolio_risk']['status']}",
        f"Data quality: {brief['snapshot']['consistency_grade']}",
        "",
    ]
    recommendations = brief.get("recommendations") or []
    if recommendations:
        for card in recommendations:
            lines.extend(
                [
                    (
                        f"{card['slate_rank']}. {card['market']['title']} - "
                        f"BUY {card['market']['side']} at "
                        f"{card['price_probability']['executable_price']}"
                    ),
                    f"   Approved size: {card['economics']['approved_quantity']} contract(s)",
                    f"   Expected net EV: {card['economics']['expected_net_ev_total']}",
                    (
                        "   Risk-adjusted EV lower bound: "
                        f"{card['economics']['risk_adjusted_ev_lcb_total']}"
                    ),
                    f"   Why it ranks here: {card['explanation']['why_ranked']}",
                    f"   Key risk: {card['explanation']['material_risks'][0]}",
                    f"   Invalid if: {card['explanation']['what_would_invalidate'][0]}",
                    f"   Expires: {card['timing']['recommendation_expires_at']}",
                    "",
                ]
            )
        lines.append("Trade nothing else from this snapshot.")
    else:
        lines.extend(
            [
                "Trade nothing right now.",
                f"Reason: {brief['no_trade']['message']}",
                f"Rejected candidates: {sum(brief['rejection_summary'].values())}",
            ]
        )
    lines.extend(
        [
            "",
            f"Snapshot: {brief['snapshot']['snapshot_id']}",
            "This is an advisory snapshot, not an order.",
        ]
    )
    return "\n".join(lines)


def persist_recommendation_memory(
    session: Session,
    *,
    brief: dict[str, Any],
    query: PersonalTraderQuery,
    evaluations: list[CandidateEvaluation],
) -> None:
    base_payload = {
        "brief_id": brief["brief_id"],
        "query_id": query.query_id,
        "snapshot_id": brief["snapshot"]["snapshot_id"],
        "schema_version": brief["schema_version"],
        "ranking_policy_version": brief["ranking_policy_version"],
    }
    _append_event(
        session,
        event_type=EVENT_BRIEF_REQUESTED,
        brief=brief,
        query=query,
        payload=base_payload,
    )
    _append_event(
        session,
        event_type=EVENT_QUERY_NORMALIZED,
        brief=brief,
        query=query,
        payload={"query": brief["query"]},
    )
    _append_event(
        session,
        event_type=EVENT_SNAPSHOT_CAPTURED,
        brief=brief,
        query=query,
        payload={"snapshot": brief["snapshot"], "source_health": brief["source_health"]},
    )
    for evaluation in evaluations:
        payload = _candidate_audit_payload(evaluation)
        _append_event(
            session,
            event_type=EVENT_CANDIDATE_EVALUATED,
            brief=brief,
            query=query,
            payload=payload,
            candidate_id=evaluation.candidate_id,
        )
        if evaluation.actionable:
            _append_event(
                session,
                event_type=EVENT_CANDIDATE_RANKED,
                brief=brief,
                query=query,
                payload=payload,
                candidate_id=evaluation.candidate_id,
            )
        else:
            _append_event(
                session,
                event_type=EVENT_CANDIDATE_REJECTED,
                brief=brief,
                query=query,
                payload=payload,
                candidate_id=evaluation.candidate_id,
            )
    _append_event(
        session,
        event_type=EVENT_BRIEF_ISSUED,
        brief=brief,
        query=query,
        payload=brief,
    )


def latest_brief(session: Session) -> dict[str, Any] | None:
    row = session.scalar(
        select(PersonalTraderRecommendationMemory)
        .where(PersonalTraderRecommendationMemory.event_type == EVENT_BRIEF_ISSUED)
        .order_by(
            desc(PersonalTraderRecommendationMemory.created_at),
            desc(PersonalTraderRecommendationMemory.id),
        )
        .limit(1)
    )
    return _memory_payload(row)


def brief_by_id(session: Session, brief_id: str) -> dict[str, Any] | None:
    row = session.scalar(
        select(PersonalTraderRecommendationMemory)
        .where(
            PersonalTraderRecommendationMemory.event_type == EVENT_BRIEF_ISSUED,
            PersonalTraderRecommendationMemory.brief_id == brief_id,
        )
        .order_by(
            desc(PersonalTraderRecommendationMemory.created_at),
            desc(PersonalTraderRecommendationMemory.id),
        )
        .limit(1)
    )
    return _memory_payload(row)


def recommendation_by_id(session: Session, recommendation_id: str) -> dict[str, Any] | None:
    rows = session.scalars(
        select(PersonalTraderRecommendationMemory)
        .where(PersonalTraderRecommendationMemory.event_type == EVENT_BRIEF_ISSUED)
        .order_by(
            desc(PersonalTraderRecommendationMemory.created_at),
            desc(PersonalTraderRecommendationMemory.id),
        )
    )
    for memory in rows:
        payload = _memory_payload(memory)
        found = _recommendation_from_brief(payload, recommendation_id)
        if found:
            return found
    return None


def recommendation_audit_events(
    session: Session,
    *,
    brief_id: str | None = None,
    recommendation_id: str | None = None,
) -> list[dict[str, Any]]:
    statement = select(PersonalTraderRecommendationMemory).order_by(
        PersonalTraderRecommendationMemory.created_at,
        PersonalTraderRecommendationMemory.id,
    )
    if brief_id:
        statement = statement.where(PersonalTraderRecommendationMemory.brief_id == brief_id)
    if recommendation_id:
        statement = statement.where(
            PersonalTraderRecommendationMemory.recommendation_id == recommendation_id
        )
    return [
        {
            "event_id": row.event_id,
            "event_type": row.event_type,
            "brief_id": row.brief_id,
            "recommendation_id": row.recommendation_id,
            "candidate_id": row.candidate_id,
            "created_at": row.created_at.isoformat(),
            "execution_mode": row.execution_mode,
            "payload": decode_json(row.payload_json),
        }
        for row in session.scalars(statement)
    ]


def personal_trader_status(session: Session, *, settings: Settings | None = None) -> dict[str, Any]:
    resolved_settings = settings or get_settings()
    config = config_from_settings(resolved_settings)
    issued = int(
        session.scalar(
            select(func.count())
            .select_from(PersonalTraderRecommendationMemory)
            .where(PersonalTraderRecommendationMemory.event_type == EVENT_BRIEF_ISSUED)
        )
        or 0
    )
    latest = latest_brief(session)
    return {
        "enabled": config.enabled,
        "mode": config.mode,
        "brief_count": issued,
        "latest_brief_id": latest["brief_id"] if latest else None,
        "latest_no_trade": bool(latest and latest["no_trade"]["active"]),
        "ranking_policy_version": config.ranking_policy_version,
        "eligibility_policy_version": config.eligibility_policy_version,
        "explanation_policy_version": config.explanation_policy_version,
        "read_only_boundary": READ_ONLY_BOUNDARY,
    }


def _evaluate_candidates(
    session: Session,
    *,
    config: PersonalTraderConfig,
    query: PersonalTraderQuery,
    source_health: list[dict[str, Any]],
    snapshot_id: str,
    generated_at: datetime,
) -> list[CandidateEvaluation]:
    latest_rankings = _latest_rankings(session, limit=config.candidate_limit)
    evaluations = [
        _evaluate_ranking(
            session,
            ranking=ranking,
            config=config,
            query=query,
            snapshot_id=snapshot_id,
            generated_at=generated_at,
        )
        for ranking in latest_rankings
    ]
    if config.mode == MODE_DISABLED:
        for evaluation in evaluations:
            _reject_once(evaluation, "PHASE_3U_DISABLED")
    if _required_decision_sources_unavailable(source_health):
        for evaluation in evaluations:
            _reject_once(evaluation, "QUOTE_STALE")
    return evaluations


def _evaluate_ranking(
    session: Session,
    *,
    ranking: MarketRanking,
    config: PersonalTraderConfig,
    query: PersonalTraderQuery,
    snapshot_id: str,
    generated_at: datetime,
) -> CandidateEvaluation:
    market = session.get(Market, ranking.ticker)
    snapshot = _latest_snapshot(session, ranking.ticker)
    forecast = _latest_forecast(session, ranking.ticker, ranking.forecast_model)
    sizing = _latest_sizing(session, ranking.ticker, ranking.forecast_model)
    risk = _latest_risk(session, ranking.ticker, ranking.forecast_model)
    phase_3s = _latest_phase_3s(session, ranking)
    category = classify_market_category(ranking.title or (market.title if market else ""))
    side = _side(ranking.best_side)
    price = _price_for_side(ranking, snapshot, side)
    model_probability = _model_probability_for_side(ranking, forecast, side)
    market_probability = _market_probability(price)
    raw_edge = _edge(ranking, model_probability, price)
    spread = to_decimal(ranking.spread) or to_decimal(snapshot.spread if snapshot else None)
    costs = _costs(spread)
    net_ev = raw_edge - costs if raw_edge is not None and costs is not None else None
    expected_roi = _roi(net_ev, price)
    risk_adjusted_ev = net_ev - Decimal("0.005") if net_ev is not None else None
    risk_adjusted_ev_lcb = (
        risk_adjusted_ev - Decimal("0.005") if risk_adjusted_ev is not None else None
    )
    phase_3m_quantity = sizing.proposed_contracts if sizing else 0
    approved_quantity = _approved_quantity(sizing, risk)
    evaluation = CandidateEvaluation(
        ranking=ranking,
        market=market,
        snapshot=snapshot,
        forecast=forecast,
        sizing=sizing,
        risk=risk,
        phase_3s=phase_3s,
        category=category,
        candidate_id=stable_id(snapshot_id, ranking.id, ranking.ticker, prefix="cand"),
        side=side,
        price=price,
        model_probability=model_probability,
        market_probability=market_probability,
        raw_edge=raw_edge,
        spread=spread,
        costs=costs,
        net_ev=net_ev,
        expected_roi=expected_roi,
        risk_adjusted_ev=risk_adjusted_ev,
        risk_adjusted_ev_lcb=risk_adjusted_ev_lcb,
        approved_quantity=approved_quantity,
        phase_3m_quantity=phase_3m_quantity,
        phase_3n_decision=_phase_3n_decision(risk, sizing),
    )
    _apply_gates(evaluation, config=config, query=query, generated_at=generated_at)
    evaluation.ranking_components = _ranking_components(evaluation)
    return evaluation


def _apply_gates(
    evaluation: CandidateEvaluation,
    *,
    config: PersonalTraderConfig,
    query: PersonalTraderQuery,
    generated_at: datetime,
) -> None:
    market = evaluation.market
    status = (market.status if market else evaluation.ranking.status or "").upper()
    if status not in {"OPEN", "ACTIVE"}:
        _reject_once(evaluation, "MARKET_NOT_OPEN")
    if market and market.close_time is None:
        _reject_once(evaluation, "MARKET_CLOSE_MISSING")
    if market and _age_seconds(market.close_time, generated_at) > 0:
        _reject_once(evaluation, "MARKET_ALREADY_CLOSED")
    if market and not (market.rules_primary or market.rules_secondary):
        _reject_once(evaluation, "SETTLEMENT_TERMS_MISSING")
    if evaluation.price is None:
        _reject_once(evaluation, "QUOTE_MISSING")
    quote_age = (
        float("inf")
        if evaluation.snapshot is None
        else _age_seconds(evaluation.snapshot.captured_at, generated_at)
    )
    if quote_age > config.max_quote_age_seconds:
        _reject_once(evaluation, "QUOTE_STALE")
    if evaluation.forecast is None or evaluation.model_probability is None:
        _reject_once(evaluation, "FORECAST_MISSING")
    elif _age_seconds(evaluation.forecast.forecasted_at, generated_at) > (
        config.max_forecast_age_seconds
    ):
        _reject_once(evaluation, "FORECAST_STALE")
    opportunity_age = _age_seconds(evaluation.ranking.ranked_at, generated_at)
    if opportunity_age > config.max_opportunity_age_seconds:
        _reject_once(evaluation, "OPPORTUNITY_STALE")
    if evaluation.spread is not None and evaluation.spread > config.max_spread:
        _reject_once(evaluation, "SPREAD_LIMIT_EXCEEDED")
    if evaluation.raw_edge is None:
        _reject_once(evaluation, "MISSING_EDGE")
    if evaluation.net_ev is None or evaluation.net_ev < config.min_net_ev_per_contract:
        _reject_once(evaluation, "NET_EV_BELOW_MINIMUM")
    if evaluation.expected_roi is None or evaluation.expected_roi < config.min_expected_roi:
        _reject_once(evaluation, "ROI_BELOW_MINIMUM")
    if (
        evaluation.risk_adjusted_ev_lcb is None
        or evaluation.risk_adjusted_ev_lcb < config.min_risk_adjusted_ev_lcb_per_contract
    ):
        _reject_once(evaluation, "RISK_ADJUSTED_EV_BELOW_MINIMUM")
    _apply_phase_3s_gate(evaluation, config=config)
    if evaluation.sizing is None:
        _reject_once(evaluation, "PHASE_3M_SIZE_MISSING")
    elif evaluation.phase_3m_quantity <= 0:
        _reject_once(evaluation, "PHASE_3M_ZERO_SIZE")
    if evaluation.risk is None:
        _reject_once(evaluation, "PHASE_3N_DECISION_MISSING")
    else:
        risk_age = _age_seconds(evaluation.risk.decision_timestamp, generated_at)
        if risk_age > config.max_risk_age_seconds:
            _reject_once(evaluation, "PHASE_3N_STALE")
        if evaluation.risk.action.upper() == "BLOCK":
            _reject_once(evaluation, "PHASE_3N_BLOCK")
        if evaluation.approved_quantity <= 0:
            _reject_once(evaluation, "PHASE_3N_ZERO_QUANTITY")
    if not _passes_user_filters(evaluation, query=query):
        _reject_once(evaluation, "USER_FILTERED")


def _apply_phase_3s_gate(
    evaluation: CandidateEvaluation,
    *,
    config: PersonalTraderConfig,
) -> None:
    decision = evaluation.phase_3s
    if decision is None:
        if config.allow_phase_3s_fallback:
            evaluation.warning_codes.append("PHASE_3S_BASELINE_FALLBACK")
        else:
            _reject_once(evaluation, "PHASE_3S_UNSUPPORTED")
        return
    action = decision.recommended_action.upper()
    support = decode_json(decision.support_json)
    ood_status = str(support.get("ood_status") or support.get("ood") or "IN_DISTRIBUTION").upper()
    if action == "SKIP":
        _reject_once(evaluation, "PHASE_3S_SKIP")
    if ood_status not in {"IN_DISTRIBUTION", "SUPPORTED", ""}:
        _reject_once(evaluation, "PHASE_3S_OOD")


def _select_slate(
    evaluations: list[CandidateEvaluation],
    *,
    query: PersonalTraderQuery,
) -> list[CandidateEvaluation]:
    eligible = [item for item in evaluations if item.actionable]
    ranked = sorted(
        eligible,
        key=lambda item: (
            -item.ranking_components["incremental_portfolio_utility_lcb"],
            -item.ranking_components["risk_adjusted_ev_lcb_total"],
            -item.ranking_components["expected_net_ev_total"],
            -item.ranking_components["execution_quality"],
            -item.ranking_components["diversification_contribution"],
            -item.ranking_components["model_support"],
            item.candidate_id,
        ),
    )
    for index, item in enumerate(ranked, start=1):
        item.standalone_rank = index
    selected: list[CandidateEvaluation] = []
    category_counts: Counter[str] = Counter()
    event_counts: Counter[str] = Counter()
    for item in ranked:
        event = item.ranking.event_ticker or item.ranking.ticker
        if category_counts[item.category] >= 2 or event_counts[event] >= 1:
            _reject_once(item, "REDUNDANT_WITH_HIGHER_RANK")
            continue
        selected.append(item)
        item.slate_rank = len(selected)
        category_counts[item.category] += 1
        event_counts[event] += 1
        if len(selected) >= query.maximum_recommendations:
            break
    return selected


def _recommendation_card(
    evaluation: CandidateEvaluation,
    *,
    config: PersonalTraderConfig,
    query: PersonalTraderQuery,
    snapshot_id: str,
    generated_at: datetime,
) -> dict[str, Any]:
    market = evaluation.market
    ranking = evaluation.ranking
    close_time = parse_datetime(market.close_time if market else None) or (
        generated_at + timedelta(hours=1)
    )
    expires_at = min(
        generated_at + timedelta(seconds=config.max_advisory_lifetime_seconds),
        close_time,
    )
    recommendation_id = stable_id(snapshot_id, evaluation.candidate_id, "rec", prefix="rec")
    expected_total = (evaluation.net_ev or ZERO) * Decimal(evaluation.approved_quantity)
    lcb_total = (evaluation.risk_adjusted_ev_lcb or ZERO) * Decimal(evaluation.approved_quantity)
    max_loss = (evaluation.price or ZERO) * Decimal(evaluation.approved_quantity)
    phase_3n_codes = _reason_codes(evaluation.risk.reason_codes_json if evaluation.risk else None)
    phase_3n_decision = evaluation.phase_3n_decision
    return {
        "schema_version": CARD_SCHEMA_VERSION,
        "recommendation_id": recommendation_id,
        "snapshot_id": snapshot_id,
        "candidate_id": evaluation.candidate_id,
        "rank": evaluation.slate_rank or 1,
        "standalone_rank": evaluation.standalone_rank or evaluation.slate_rank or 1,
        "slate_rank": evaluation.slate_rank or 1,
        "status": STATUS_ACTIONABLE,
        "market": {
            "market_id": ranking.ticker,
            "market_ticker": ranking.ticker,
            "event_id": ranking.event_ticker or "UNKNOWN_EVENT",
            "series_id": (
                ranking.series_ticker
                or (market.series_ticker if market else None)
                or "UNKNOWN_SERIES"
            ),
            "category_id": evaluation.category,
            "title": _safe_title(ranking.title or (market.title if market else ranking.ticker)),
            "side": evaluation.side,
            "market_status": "OPEN",
            "settlement_terms_version": "market_rules_as_observed_v1",
            "settlement_source_ids": ["market_rules_primary"],
            "synthetic": False,
        },
        "timing": {
            "as_of": query.requested_as_of.isoformat(),
            "quote_observed_at": (
                evaluation.snapshot.captured_at if evaluation.snapshot else generated_at
            ).isoformat(),
            "market_close_at": close_time.isoformat(),
            "expected_settlement_at": (
                parse_datetime(market.settlement_ts if market else None).isoformat()
                if market and market.settlement_ts
                else None
            ),
            "recommendation_expires_at": expires_at.isoformat(),
            "freshness_status": "FRESH",
        },
        "price_probability": {
            "currency": "USD",
            "best_bid": _best_bid(evaluation),
            "best_ask": _best_ask(evaluation),
            "executable_price": decimal_string(evaluation.price),
            "price_basis": "TOP_OF_BOOK",
            "model_probability": _prob_string(evaluation.model_probability),
            "model_probability_low": _prob_string(_prob_low(evaluation.model_probability)),
            "model_probability_high": _prob_string(_prob_high(evaluation.model_probability)),
            "market_implied_probability": _prob_string(evaluation.market_probability),
            "raw_edge": decimal_string(evaluation.raw_edge),
        },
        "economics": {
            "gross_ev_per_contract": decimal_string(evaluation.raw_edge),
            "expected_costs_per_contract": decimal_string(evaluation.costs),
            "net_ev_per_contract": decimal_string(evaluation.net_ev),
            "expected_roi": decimal_string(evaluation.expected_roi),
            "risk_adjusted_ev_per_contract": decimal_string(evaluation.risk_adjusted_ev),
            "risk_adjusted_ev_lcb_per_contract": decimal_string(evaluation.risk_adjusted_ev_lcb),
            "approved_quantity": evaluation.approved_quantity,
            "expected_net_ev_total": decimal_string(expected_total),
            "risk_adjusted_ev_lcb_total": decimal_string(lcb_total),
            "maximum_loss_total": decimal_string(max_loss),
            "capital_required": decimal_string(max_loss),
            "reward_definition_version": "reward-net-ev-v1",
        },
        "model_policy": {
            "forecast_id": str(evaluation.forecast.id if evaluation.forecast else ranking.id),
            "model_id": ranking.forecast_model,
            "model_version": ranking.forecast_model,
            "confidence_score": _score_ratio(ranking.model_confidence_score),
            "confidence_tier": _confidence_tier(ranking.model_confidence_score),
            "opportunity_score": _score_ratio(ranking.opportunity_score),
            "phase_3s_action": "PROCEED",
            "phase_3s_policy_version": _phase_3s_version(evaluation),
            "phase_3s_support_status": _phase_3s_support_status(evaluation),
            "phase_3s_ood_status": "IN_DISTRIBUTION",
            "phase_3m_proposed_quantity": evaluation.phase_3m_quantity,
            "phase_3m_policy_version": (
                evaluation.sizing.version if evaluation.sizing else "UNKNOWN"
            ),
            "phase_3n_decision": phase_3n_decision,
            "phase_3n_approved_quantity": evaluation.approved_quantity,
            "phase_3n_policy_version": evaluation.risk.version if evaluation.risk else "UNKNOWN",
            "phase_3n_reason_codes": phase_3n_codes,
        },
        "market_quality": {
            "spread": decimal_string(evaluation.spread),
            "available_depth_at_price": _depth(evaluation),
            "depth_for_approved_quantity": max(1, evaluation.approved_quantity),
            "estimated_slippage": "0",
            "liquidity_tier": _liquidity_tier(ranking.liquidity_score),
            "orderbook_sequence_status": "SYNCHRONIZED",
        },
        "portfolio_effect": {
            "incremental_category_exposure": decimal_string(max_loss),
            "incremental_model_exposure": decimal_string(max_loss),
            "incremental_correlation_exposure": "0",
            "incremental_settlement_dependency_exposure": "0",
            "incremental_max_loss": decimal_string(max_loss),
            "incremental_risk_budget_usage": decimal_string(_min(ONE, max_loss / Decimal("100"))),
            "remaining_headroom": decimal_string(_max(ZERO, Decimal("100") - max_loss)),
            "incremental_portfolio_utility_lcb": decimal_string(
                evaluation.ranking_components["incremental_portfolio_utility_lcb"]
            ),
        },
        "explanation": _explanation(evaluation),
        "lineage": {
            "opportunity_id": f"market_ranking:{ranking.id}",
            "quote_snapshot_id": (
                f"market_snapshot:{evaluation.snapshot.id if evaluation.snapshot else 'UNKNOWN'}"
            ),
            "portfolio_snapshot_id": (
                f"advanced_risk:{evaluation.risk.id}" if evaluation.risk else "UNKNOWN"
            ),
            "feature_view_id": "phase_3q_approved_features_read_only",
            "ranking_policy_version": config.ranking_policy_version,
            "explanation_policy_version": config.explanation_policy_version,
            "metric_catalog_version": METRIC_CATALOG_VERSION,
        },
    }


def _explanation(evaluation: CandidateEvaluation) -> dict[str, Any]:
    size_note = (
        f"Phase 3N reduced size from {evaluation.phase_3m_quantity} to "
        f"{evaluation.approved_quantity} contract(s)."
        if evaluation.phase_3m_quantity > evaluation.approved_quantity
        else f"Phase 3N approved {evaluation.approved_quantity} contract(s)."
    )
    return {
        "why_ranked": (
            "Positive lower-bound portfolio utility after costs, Phase 3M sizing, "
            "and Phase 3N risk gates."
        ),
        "key_positive_factors": [
            (
                "Model probability is above the executable price by "
                f"{decimal_string(evaluation.raw_edge)}."
            ),
            f"Expected net EV per contract is {decimal_string(evaluation.net_ev)}.",
            size_note,
        ],
        "material_risks": [
            (
                "The recommendation depends on current quote freshness, spread, "
                "and Phase 3N headroom."
            )
        ],
        "what_would_invalidate": [
            (
                f"Spread above {decimal_string(evaluation.spread)} worsening materially, "
                "stale forecast, market status change, or a new Phase 3N block."
            )
        ],
        "why_not_higher": (
            "Slate rank reflects portfolio concentration and deterministic tie-breakers."
        ),
        "assumptions": [
            "Advisory-only output; no order is created.",
            "Displayed quantity is Phase 3N-approved and cannot exceed Phase 3M.",
        ],
        "evidence_ids": [
            f"market_ranking:{evaluation.ranking.id}",
            f"forecast:{evaluation.forecast.id if evaluation.forecast else 'UNKNOWN'}",
            f"advanced_risk:{evaluation.risk.id if evaluation.risk else 'UNKNOWN'}",
        ],
        "renderer": "DETERMINISTIC_TEMPLATE",
    }


def _watchlist(
    evaluations: list[CandidateEvaluation],
    *,
    selected: list[CandidateEvaluation],
    query: PersonalTraderQuery,
) -> list[dict[str, Any]]:
    if not query.include_watchlist:
        return []
    selected_ids = {item.candidate_id for item in selected}
    rows = []
    for item in evaluations:
        if item.candidate_id in selected_ids:
            continue
        status = STATUS_REJECTED if item.rejection_codes else STATUS_WATCHLIST
        rows.append(
            {
                "candidate_id": item.candidate_id,
                "market_id": item.ranking.ticker,
                "title": _safe_title(item.ranking.title or item.ranking.ticker),
                "side": item.side if item.side in {"YES", "NO"} else "NONE",
                "status": status,
                "reason_codes": sorted(
                    set(item.rejection_codes or item.warning_codes or ["NOT_SELECTED"])
                ),
            }
        )
    synthetic = _synthetic_watchlist_item(evaluations)
    if query.include_synthetic_research and synthetic:
        rows.append(synthetic)
    return rows[:25]


def _synthetic_watchlist_item(evaluations: list[CandidateEvaluation]) -> dict[str, Any] | None:
    if evaluations:
        return None
    return {
        "candidate_id": "synthetic-research-placeholder",
        "market_id": "synthetic-research",
        "title": "Synthetic market research is internal, synthetic, and non-tradable.",
        "side": "NONE",
        "status": STATUS_SYNTHETIC,
        "reason_codes": ["SYNTHETIC_INTERNAL_NON_TRADABLE"],
    }


def _no_trade_payload(
    *,
    config: PersonalTraderConfig,
    evaluations: list[CandidateEvaluation],
    recommendations: list[dict[str, Any]],
) -> dict[str, Any]:
    if recommendations:
        return {
            "active": False,
            "reason_codes": [],
            "message": (
                f"{len(recommendations)} advisory recommendation(s) are available; "
                "trade nothing else from this snapshot."
            ),
        }
    reasons = sorted({code for item in evaluations for code in item.rejection_codes})
    if config.mode == MODE_DISABLED:
        reasons = ["PHASE_3U_DISABLED"]
    if not reasons:
        reasons = ["NO_ELIGIBLE_CANDIDATES"]
    return {
        "active": True,
        "reason_codes": reasons,
        "message": _no_trade_message(reasons),
    }


def _append_event(
    session: Session,
    *,
    event_type: str,
    brief: dict[str, Any],
    query: PersonalTraderQuery,
    payload: dict[str, Any],
    candidate_id: str | None = None,
    recommendation_id: str | None = None,
) -> None:
    payload_text = canonical_json(payload)
    created_at = utc_now()
    event = PersonalTraderRecommendationMemory(
        event_id=event_id(
            brief["brief_id"],
            event_type,
            candidate_id,
            created_at.isoformat(),
            payload_text,
        ),
        event_type=event_type,
        brief_id=brief["brief_id"],
        recommendation_id=recommendation_id,
        candidate_id=candidate_id,
        created_at=created_at,
        execution_mode=brief["execution_mode"],
        account_scope_hash=scope_hash(query.account_scope),
        portfolio_scope_hash=scope_hash(query.portfolio_scope),
        schema_version=brief["schema_version"],
        ranking_policy_version=brief["ranking_policy_version"],
        source_ids_json=canonical_json(_source_ids(brief)),
        payload_json=payload_text,
        raw_json=canonical_json(
            {
                "event_type": event_type,
                "brief_id": brief["brief_id"],
                "candidate_id": candidate_id,
                "payload_hash": stable_hash(payload),
            }
        ),
    )
    session.add(event)


def _candidate_audit_payload(evaluation: CandidateEvaluation) -> dict[str, Any]:
    return {
        "candidate_id": evaluation.candidate_id,
        "ticker": evaluation.ranking.ticker,
        "model": evaluation.ranking.forecast_model,
        "actionable": evaluation.actionable,
        "rejection_codes": evaluation.rejection_codes,
        "warning_codes": evaluation.warning_codes,
        "phase_3m_quantity": evaluation.phase_3m_quantity,
        "phase_3n_approved_quantity": evaluation.approved_quantity,
        "phase_3n_decision": evaluation.phase_3n_decision,
        "net_ev": decimal_string(evaluation.net_ev, fallback="n/a"),
        "risk_adjusted_ev_lcb": decimal_string(evaluation.risk_adjusted_ev_lcb, fallback="n/a"),
        "ranking_components": evaluation.ranking_components,
    }


def _latest_rankings(session: Session, *, limit: int) -> list[MarketRanking]:
    rows = session.scalars(
        select(MarketRanking)
        .order_by(
            desc(MarketRanking.ranked_at),
            desc(MarketRanking.opportunity_score),
            desc(MarketRanking.id),
        )
        .limit(limit * 4)
    )
    latest: dict[str, MarketRanking] = {}
    for row in rows:
        if row.ticker not in latest:
            latest[row.ticker] = row
        if len(latest) >= limit:
            break
    return list(latest.values())


def _latest_snapshot(session: Session, ticker: str) -> MarketSnapshot | None:
    return session.scalar(
        select(MarketSnapshot)
        .where(MarketSnapshot.ticker == ticker)
        .order_by(desc(MarketSnapshot.captured_at), desc(MarketSnapshot.id))
        .limit(1)
    )


def _latest_forecast(session: Session, ticker: str, model_name: str) -> Forecast | None:
    return session.scalar(
        select(Forecast)
        .where(Forecast.ticker == ticker, Forecast.model_name == model_name)
        .order_by(desc(Forecast.forecasted_at), desc(Forecast.id))
        .limit(1)
    )


def _latest_sizing(
    session: Session,
    ticker: str,
    model_name: str,
) -> PositionSizingDecisionLog | None:
    return session.scalar(
        select(PositionSizingDecisionLog)
        .where(
            PositionSizingDecisionLog.ticker == ticker,
            PositionSizingDecisionLog.model_name == model_name,
        )
        .order_by(
            desc(PositionSizingDecisionLog.decision_timestamp),
            desc(PositionSizingDecisionLog.id),
        )
        .limit(1)
    )


def _latest_risk(
    session: Session,
    ticker: str,
    model_name: str,
) -> AdvancedRiskDecisionLog | None:
    return session.scalar(
        select(AdvancedRiskDecisionLog)
        .where(
            AdvancedRiskDecisionLog.ticker == ticker,
            AdvancedRiskDecisionLog.model_id == model_name,
        )
        .order_by(
            desc(AdvancedRiskDecisionLog.decision_timestamp),
            desc(AdvancedRiskDecisionLog.id),
        )
        .limit(1)
    )


def _latest_phase_3s(session: Session, ranking: MarketRanking) -> RlPolicyDecision | None:
    opportunity_ids = [f"market_ranking:{ranking.id}", ranking.ticker, str(ranking.id)]
    return session.scalar(
        select(RlPolicyDecision)
        .where(RlPolicyDecision.opportunity_id.in_(opportunity_ids))
        .order_by(desc(RlPolicyDecision.decision_at), desc(RlPolicyDecision.policy_decision_id))
        .limit(1)
    )


def _source_health(
    session: Session,
    *,
    config: PersonalTraderConfig,
    generated_at: datetime,
) -> list[dict[str, Any]]:
    source_specs = (
        ("market_catalog", Market, Market.last_seen_at, True),
        ("market_data", MarketSnapshot, MarketSnapshot.captured_at, True),
        ("forecast_pipeline", Forecast, Forecast.forecasted_at, True),
        ("opportunity_rankings", MarketRanking, MarketRanking.ranked_at, True),
        ("phase_3m_sizing", PositionSizingDecisionLog, PositionSizingDecisionLog.created_at, True),
        ("phase_3n_risk", AdvancedRiskDecisionLog, AdvancedRiskDecisionLog.created_at, True),
        ("phase_3s_policy", RlPolicyDecision, RlPolicyDecision.decision_at, False),
        ("phase_3r_synthetic", SyntheticMarketRun, SyntheticMarketRun.completed_at, False),
    )
    rows = []
    for source, model, column, required in source_specs:
        latest = parse_datetime(session.scalar(select(func.max(column))))
        count = int(session.scalar(select(func.count()).select_from(model)) or 0)
        age_ms = int(max(0, (generated_at - latest).total_seconds() * 1000)) if latest else 0
        rows.append(
            {
                "source": source,
                "status": _source_status(
                    source=source,
                    latest=latest,
                    count=count,
                    required=required,
                    config=config,
                    generated_at=generated_at,
                ),
                "as_of": latest.isoformat() if latest else generated_at.isoformat(),
                "lag_ms": age_ms,
                "required": required,
                "row_count": count,
            }
        )
    return rows


def _public_source_health(source_health: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "source": row["source"],
            "status": row["status"],
            "as_of": row["as_of"],
            "lag_ms": row["lag_ms"],
        }
        for row in source_health
    ]


def _source_status(
    *,
    source: str,
    latest: datetime | None,
    count: int,
    required: bool,
    config: PersonalTraderConfig,
    generated_at: datetime,
) -> str:
    if count == 0:
        return "UNAVAILABLE" if required else "PARTIAL"
    if latest is None:
        return "UNAVAILABLE" if required else "PARTIAL"
    age = _age_seconds(latest, generated_at)
    threshold = {
        "market_data": config.max_quote_age_seconds,
        "forecast_pipeline": config.max_forecast_age_seconds,
        "opportunity_rankings": config.max_opportunity_age_seconds,
        "phase_3n_risk": config.max_risk_age_seconds,
    }.get(source, config.max_opportunity_age_seconds)
    if age > threshold:
        return "STALE"
    if age > threshold / 2:
        return "AGING"
    return "FRESH"


def _query_payload(query: PersonalTraderQuery) -> dict[str, Any]:
    return {
        "query_id": query.query_id,
        "original_query_redacted": _redact_query(query.natural_language_query),
        "normalized_intent": query.normalized_intent,
        "resolved_day_start": query.resolved_day_start.isoformat(),
        "resolved_day_end": query.resolved_day_end.isoformat(),
        "effective_filters": query.effective_filters(),
    }


def _ranking_components(evaluation: CandidateEvaluation) -> dict[str, Decimal]:
    quantity = Decimal(evaluation.approved_quantity)
    expected_total = (evaluation.net_ev or ZERO) * quantity
    lcb_total = (evaluation.risk_adjusted_ev_lcb or ZERO) * quantity
    spread_penalty = (evaluation.spread or ZERO) * Decimal("0.10")
    category_penalty = Decimal("0.002") if evaluation.category == "other" else ZERO
    execution_quality = _max(ZERO, ONE - (evaluation.spread or ZERO))
    diversification = Decimal("0.02") if evaluation.category not in {"other", "unknown"} else ZERO
    model_support = _score_decimal(evaluation.ranking.model_confidence_score)
    utility = lcb_total - spread_penalty - category_penalty
    return {
        "incremental_portfolio_utility_lcb": utility,
        "risk_adjusted_ev_lcb_total": lcb_total,
        "expected_net_ev_total": expected_total,
        "execution_quality": execution_quality,
        "diversification_contribution": diversification,
        "model_support": model_support,
    }


def _rejection_summary(evaluations: list[CandidateEvaluation]) -> dict[str, int]:
    summary = {
        "risk": 0,
        "economics": 0,
        "market_quality": 0,
        "model_quality": 0,
        "user_filtered": 0,
        "other": 0,
    }
    for evaluation in evaluations:
        seen_categories = set()
        for code in evaluation.rejection_codes:
            category = REJECTION_CATEGORIES.get(code, "other")
            seen_categories.add(category)
        for category in seen_categories:
            summary[category] += 1
    return summary


def _portfolio_risk_payload(evaluations: list[CandidateEvaluation]) -> dict[str, Any]:
    blocked = any("PHASE_3N_BLOCK" in item.rejection_codes for item in evaluations)
    restricted = any(item.risk and item.risk.action.upper() == "REDUCE" for item in evaluations)
    return {
        "status": "BLOCKED" if blocked else "RESTRICTED" if restricted else "NORMAL",
        "daily_loss_status": "WITHIN_LIMIT",
        "drawdown_status": "WITHIN_LIMIT",
        "new_risk_allowed": not blocked,
        "policy_version": _latest_risk_version(evaluations),
    }


def _brief_warnings(
    config: PersonalTraderConfig,
    source_health: list[dict[str, Any]],
    evaluations: list[CandidateEvaluation],
) -> list[str]:
    warnings = []
    if config.mode == MODE_DISABLED:
        warnings.append("Phase 3U is disabled; brief is non-actionable.")
    has_bad_required_source = any(
        row["status"] in {"STALE", "UNAVAILABLE", "CONFLICTED"}
        for row in source_health
        if row["required"]
    )
    if has_bad_required_source:
        warnings.append("One or more required advisory sources are stale or unavailable.")
    if any("PHASE_3S_BASELINE_FALLBACK" in item.warning_codes for item in evaluations):
        warnings.append(
            "Phase 3S serving policy is unavailable; baseline fallback label is visible."
        )
    return warnings


def _memory_payload(row: PersonalTraderRecommendationMemory | None) -> dict[str, Any] | None:
    if row is None:
        return None
    payload = decode_json(row.payload_json)
    return payload if isinstance(payload, dict) else None


def _recommendation_from_brief(
    brief: dict[str, Any] | None,
    recommendation_id: str,
) -> dict[str, Any] | None:
    if not brief:
        return None
    for row in brief.get("recommendations") or []:
        if row.get("recommendation_id") == recommendation_id:
            return row
    return None


def _should_persist(config: PersonalTraderConfig) -> bool:
    return config.enabled and config.mode in {MODE_SHADOW, MODE_PAPER_ADVISORY, MODE_LIVE_ADVISORY}


def _required_decision_sources_unavailable(source_health: list[dict[str, Any]]) -> bool:
    hard_sources = {"market_data", "forecast_pipeline", "opportunity_rankings"}
    return any(
        row["source"] in hard_sources and row["status"] in {"UNAVAILABLE", "CONFLICTED"}
        for row in source_health
    )


def _passes_user_filters(evaluation: CandidateEvaluation, *, query: PersonalTraderQuery) -> bool:
    ticker = evaluation.ranking.ticker
    category = evaluation.category.lower()
    include_categories = {item.lower() for item in query.category_include}
    exclude_categories = {item.lower() for item in query.category_exclude}
    include_markets = {item.upper() for item in query.market_include}
    exclude_markets = {item.upper() for item in query.market_exclude}
    if include_categories and category not in include_categories:
        return False
    if exclude_categories and category in exclude_categories:
        return False
    if include_markets and ticker.upper() not in include_markets:
        return False
    return not (exclude_markets and ticker.upper() in exclude_markets)


def _side(value: str | None) -> str:
    normalized = (value or "BUY_YES").upper()
    if "NO" in normalized:
        return "NO"
    return "YES"


def _price_for_side(
    ranking: MarketRanking,
    snapshot: MarketSnapshot | None,
    side: str,
) -> Decimal | None:
    if ranking.best_price:
        return to_decimal(ranking.best_price)
    if snapshot is None:
        return None
    if side == "NO":
        return to_decimal(snapshot.best_no_ask or snapshot.no_ask_dollars)
    return to_decimal(snapshot.best_yes_ask or snapshot.yes_ask_dollars)


def _model_probability_for_side(
    ranking: MarketRanking,
    forecast: Forecast | None,
    side: str,
) -> Decimal | None:
    probability = to_decimal(ranking.forecast_probability)
    if probability is None and forecast is not None:
        probability = to_decimal(forecast.yes_probability)
    if probability is None:
        return None
    if side == "NO":
        return ONE - probability
    return probability


def _market_probability(price: Decimal | None) -> Decimal | None:
    return price


def _edge(
    ranking: MarketRanking,
    model_probability: Decimal | None,
    price: Decimal | None,
) -> Decimal | None:
    edge = to_decimal(ranking.estimated_edge)
    if edge is not None:
        return edge
    if model_probability is None or price is None:
        return None
    return model_probability - price


def _costs(spread: Decimal | None) -> Decimal | None:
    if spread is None:
        return Decimal("0.01")
    return _max(Decimal("0.005"), spread / Decimal("2"))


def _roi(net_ev: Decimal | None, price: Decimal | None) -> Decimal | None:
    if net_ev is None or price is None or price <= ZERO:
        return None
    return net_ev / price


def _approved_quantity(
    sizing: PositionSizingDecisionLog | None,
    risk: AdvancedRiskDecisionLog | None,
) -> int:
    if sizing is None or risk is None:
        return 0
    return max(0, min(sizing.proposed_contracts, risk.executed_contracts))


def _phase_3n_decision(
    risk: AdvancedRiskDecisionLog | None,
    sizing: PositionSizingDecisionLog | None,
) -> str:
    if risk is None:
        return "BLOCK"
    if risk.action.upper() == "BLOCK":
        return "BLOCK"
    if sizing and risk.executed_contracts < sizing.proposed_contracts:
        return "REDUCE"
    return "ALLOW"


def _best_bid(evaluation: CandidateEvaluation) -> str:
    snapshot = evaluation.snapshot
    if snapshot is None:
        return decimal_string(evaluation.price)
    value = snapshot.best_no_bid if evaluation.side == "NO" else snapshot.best_yes_bid
    return decimal_string(value or evaluation.price)


def _best_ask(evaluation: CandidateEvaluation) -> str:
    snapshot = evaluation.snapshot
    if snapshot is None:
        return decimal_string(evaluation.price)
    value = snapshot.best_no_ask if evaluation.side == "NO" else snapshot.best_yes_ask
    return decimal_string(value or evaluation.price)


def _prob_low(value: Decimal | None) -> Decimal | None:
    return _max(ZERO, value - Decimal("0.05")) if value is not None else None


def _prob_high(value: Decimal | None) -> Decimal | None:
    return _min(ONE, value + Decimal("0.05")) if value is not None else None


def _prob_string(value: Decimal | None) -> str:
    if value is None:
        return "0"
    return decimal_string(_min(ONE, _max(ZERO, value)))


def _score_decimal(value: str | None) -> Decimal:
    score = to_decimal(value)
    if score is None:
        return ZERO
    if score > ONE:
        return score / Decimal("100")
    return score


def _score_ratio(value: str | None) -> str:
    return decimal_string(_score_decimal(value))


def _confidence_tier(value: str | None) -> str:
    score = _score_decimal(value)
    if score >= Decimal("0.75"):
        return "HIGH"
    if score >= Decimal("0.45"):
        return "MEDIUM"
    return "LOW"


def _liquidity_tier(value: str | None) -> str:
    score = _score_decimal(value)
    if score >= Decimal("0.75"):
        return "HIGH"
    if score >= Decimal("0.45"):
        return "MEDIUM"
    return "LOW"


def _depth(evaluation: CandidateEvaluation) -> int:
    if evaluation.approved_quantity > 0:
        return max(evaluation.approved_quantity, 1)
    return 0


def _reason_codes(value: str | None) -> list[str]:
    payload = decode_json(value)
    if isinstance(payload, list):
        return [str(item) for item in payload]
    if isinstance(payload, dict):
        raw = payload.get("reason_codes") or payload.get("reasons") or []
        return [str(item) for item in raw]
    return []


def _phase_3s_version(evaluation: CandidateEvaluation) -> str:
    if evaluation.phase_3s:
        return f"{evaluation.phase_3s.policy_id}:{evaluation.phase_3s.policy_version}"
    return "baseline-fallback-visible"


def _phase_3s_support_status(evaluation: CandidateEvaluation) -> str:
    if evaluation.phase_3s is None:
        return "BASELINE_FALLBACK"
    return "SUPPORTED_WITH_WARNING" if evaluation.phase_3s.baseline_action else "SUPPORTED"


def _latest_sizing_version(evaluations: list[CandidateEvaluation]) -> str:
    versions = [item.sizing.version for item in evaluations if item.sizing]
    return versions[0] if versions else "UNKNOWN"


def _latest_risk_version(evaluations: list[CandidateEvaluation]) -> str:
    versions = [item.risk.version for item in evaluations if item.risk]
    return versions[0] if versions else "UNKNOWN"


def _latest_phase_3s_version(evaluations: list[CandidateEvaluation]) -> str:
    versions = [_phase_3s_version(item) for item in evaluations if item.phase_3s]
    return versions[0] if versions else "baseline-fallback-visible"


def _source_ids(brief: dict[str, Any]) -> dict[str, Any]:
    return {
        "snapshot_id": brief["snapshot"]["snapshot_id"],
        "source_watermarks": brief["snapshot"]["source_watermarks"],
    }


def _environment(settings: Settings) -> str:
    env = settings.kalshi_env.upper()
    if env in {"PRODUCTION", "LIVE"}:
        return "PRODUCTION"
    if env == "PAPER":
        return "PAPER"
    if env == "TEST":
        return "TEST"
    return "DEMO"


def _execution_mode(config: PersonalTraderConfig) -> str:
    if config.mode == MODE_LIVE_ADVISORY:
        return "LIVE_ADVISORY"
    if config.mode == MODE_PAPER_ADVISORY:
        return "PAPER_ADVISORY"
    if config.mode == MODE_SHADOW:
        return "SHADOW"
    return "OFFLINE_REPLAY" if config.mode != MODE_DISABLED else "SHADOW"


def _count_markets(session: Session) -> int:
    return int(session.scalar(select(func.count()).select_from(Market)) or 0)


def _summary_message(
    recommendations: list[dict[str, Any]],
    no_trade: dict[str, Any],
) -> str:
    if recommendations:
        return f"{len(recommendations)} advisory recommendation(s) qualify."
    return no_trade["message"]


def _no_trade_message(reason_codes: list[str]) -> str:
    if "PHASE_3U_DISABLED" in reason_codes:
        return "Phase 3U is disabled."
    if "PHASE_3N_BLOCK" in reason_codes:
        return "Phase 3N blocks new risk."
    if "QUOTE_STALE" in reason_codes or "FORECAST_STALE" in reason_codes:
        return "Required market or forecast data is stale."
    if "PHASE_3M_SIZE_MISSING" in reason_codes or "PHASE_3N_DECISION_MISSING" in reason_codes:
        return "Sizing or risk approval data is unavailable."
    if "NET_EV_BELOW_MINIMUM" in reason_codes:
        return "Candidates do not clear net economic value thresholds."
    return "No candidate survived the eligibility waterfall."


def _consistency_grade(source_health: list[dict[str, Any]]) -> str:
    required = [row for row in source_health if row["required"]]
    if any(row["status"] == "CONFLICTED" for row in required):
        return "CONFLICTED"
    if any(row["status"] == "UNAVAILABLE" for row in required):
        return "UNAVAILABLE"
    if any(row["status"] == "STALE" for row in required):
        return "PARTIAL"
    if any(row["status"] == "AGING" for row in required):
        return "BOUNDED"
    return "STRONG"


def _maximum_source_skew_ms(source_health: list[dict[str, Any]]) -> int:
    times = [parse_datetime(row["as_of"]) for row in source_health if row.get("as_of")]
    present = [time for time in times if time is not None]
    if len(present) < 2:
        return 0
    return int((max(present) - min(present)).total_seconds() * 1000)


def _source_signature(source_health: list[dict[str, Any]]) -> str:
    return canonical_json(
        {
            row["source"]: {
                "status": row["status"],
                "as_of": row["as_of"],
                "row_count": row["row_count"],
            }
            for row in source_health
        }
    )


def _redact_query(value: str) -> str:
    return value.replace("\n", " ")[:240]


def _safe_title(value: str | None) -> str:
    text = (value or "Untitled market").replace("<", "&lt;").replace(">", "&gt;")
    return text[:500]


def _reject_once(evaluation: CandidateEvaluation, reason: str) -> None:
    if reason not in evaluation.rejection_codes:
        evaluation.rejection_codes.append(reason)


def _age_seconds(value: datetime | None, generated_at: datetime) -> float:
    parsed = parse_datetime(value)
    if parsed is None:
        return float("inf")
    return (generated_at - parsed).total_seconds()


def _zoneinfo(timezone: str) -> ZoneInfo:
    try:
        return ZoneInfo(timezone)
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def _cap_recommendations(
    maximum_recommendations: int | None,
    *,
    config: PersonalTraderConfig,
) -> int:
    requested = maximum_recommendations or config.default_maximum_recommendations
    return max(1, min(int(requested), config.absolute_maximum_recommendations))


def _min(left: Decimal, right: Decimal) -> Decimal:
    return left if left <= right else right


def _max(left: Decimal, right: Decimal) -> Decimal:
    return left if left >= right else right
