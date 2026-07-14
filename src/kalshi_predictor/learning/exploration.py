from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from kalshi_predictor.active_universe import is_ticker_eligible_for_new_forecasts
from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.data.repositories import encode_json
from kalshi_predictor.data.schema import (
    Forecast,
    LearningCycle,
    LearningRun,
    MarketRanking,
    MarketSnapshot,
    PaperOrder,
)
from kalshi_predictor.lanes.repository import insert_learning_trade_for_order
from kalshi_predictor.learning.config import learning_paper_settings
from kalshi_predictor.learning.config import learning_config_payload
from kalshi_predictor.learning.duplicates import is_duplicate_candidate
from kalshi_predictor.learning.safety import settled_paper_trade_count
from kalshi_predictor.paper.ledger import (
    create_paper_order,
    get_position,
    open_order_count,
)
from kalshi_predictor.paper.models import BUY_NO, BUY_YES, PaperDecision
from kalshi_predictor.paper.simulator import simulate_immediate_fill
from kalshi_predictor.tournament.ranking import classify_market_category
from kalshi_predictor.utils.decimals import ONE_DOLLAR, decimal_to_str, to_decimal
from kalshi_predictor.utils.time import utc_now


DEFAULT_OUTPUT_DIR = Path("reports/phase3_overnight")


@dataclass(frozen=True)
class ExplorationSeedResult:
    generated_at: str
    mode: str
    model_name: str
    apply: bool
    candidates_scanned: int
    candidates_found: int
    paper_orders_created: int
    fills_created: int
    learning_paper_trades_inserted: int
    settled_paper_trades_total: int
    reason_counts: dict[str, int]
    selected_candidates: list[dict[str, Any]]
    created_orders: list[dict[str, Any]]
    safety: dict[str, Any]
    thresholds: dict[str, Any]
    json_path: str | None = None
    markdown_path: str | None = None


@dataclass(frozen=True)
class _Candidate:
    ranking: MarketRanking
    forecast: Forecast
    snapshot: MarketSnapshot
    decision: PaperDecision
    score: Decimal
    spread: Decimal | None
    category: str
    reason: str
    ranking_age_minutes: float | None

    def payload(self) -> dict[str, Any]:
        return {
            "ticker": self.decision.ticker,
            "title": self.ranking.title,
            "category": self.category,
            "side": self.decision.side,
            "edge": decimal_to_str(self.decision.edge),
            "market_price": decimal_to_str(self.decision.market_price),
            "score": decimal_to_str(self.score),
            "spread": decimal_to_str(self.spread),
            "ranked_at": _iso_or_none(self.ranking.ranked_at),
            "ranking_age_minutes": self.ranking_age_minutes,
            "forecasted_at": _iso_or_none(self.forecast.forecasted_at),
            "snapshot_captured_at": _iso_or_none(self.snapshot.captured_at),
            "reason": self.reason,
            "model_name": self.decision.model_name,
        }


@dataclass
class _ScanState:
    selected_keys: set[tuple[str, str, str]] = field(default_factory=set)
    reason_counts: dict[str, int] = field(default_factory=dict)
    candidates: list[_Candidate] = field(default_factory=list)
    scanned: int = 0

    def reject(self, reason: str) -> None:
        self.reason_counts[reason] = self.reason_counts.get(reason, 0) + 1


def seed_exploratory_paper_trades(
    session: Session,
    *,
    settings: Settings | None = None,
    model_name: str = "crypto_v2",
    apply: bool = False,
    max_trades: int = 3,
    min_edge: Decimal = Decimal("-0.005"),
    min_score: Decimal = Decimal("25"),
    max_spread: Decimal | None = None,
    scan_limit: int = 120,
    ranking_fetch_limit: int = 1000,
    max_ranking_age_minutes: int = 30,
    refresh_metrics: bool = False,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
) -> ExplorationSeedResult:
    base_settings = settings or get_settings()
    bounded_max_trades = max(0, min(int(max_trades), 10))
    exploration_settings = learning_paper_settings(
        base_settings.model_copy(
            update={
                "learning_mode": True,
                "learning_model_name": model_name,
                "learning_min_edge": min_edge,
                "learning_min_opportunity_score": min_score,
                "learning_min_trades_per_cycle": 1,
                "learning_target_trades_per_cycle": max(1, bounded_max_trades or 1),
                "learning_max_paper_order_qty": 1,
                "paper_max_order_quantity": 1,
                "execution_enabled": False,
                "execution_dry_run": True,
                "overnight_run_demo": False,
                "autopilot_dry_run": True,
                "learning_block_demo_execution": True,
                "learning_block_live_execution": True,
            }
        )
    )
    resolved_max_spread = max_spread or exploration_settings.learning_max_spread
    now = utc_now()
    state = _scan_candidates(
        session,
        settings=exploration_settings,
        model_name=model_name,
        min_edge=min_edge,
        min_score=min_score,
        max_spread=resolved_max_spread,
        max_trades=bounded_max_trades,
        scan_limit=max(1, scan_limit),
        ranking_fetch_limit=max(1, ranking_fetch_limit),
        max_ranking_age_minutes=max_ranking_age_minutes,
        now=now,
    )

    created_orders: list[dict[str, Any]] = []
    fills_created = 0
    learning_rows = 0
    if apply and bounded_max_trades > 0:
        for candidate in state.candidates[:bounded_max_trades]:
            order = create_paper_order(session, candidate.decision, settings=exploration_settings)
            if order is None:
                state.reject("duplicate_or_position_blocked_at_insert")
                continue
            fill = simulate_immediate_fill(session, order, settings=exploration_settings)
            if fill is not None:
                fills_created += 1
            insert_learning_trade_for_order(
                session,
                order,
                source="overnight-exploratory-paper-seed",
            )
            learning_rows += 1
            created_orders.append(_order_payload(order))
        _record_learning_cycle(
            session,
            settings=exploration_settings,
            model_name=model_name,
            candidates_scanned=state.scanned,
            paper_orders_created=len(created_orders),
            fills_created=fills_created,
            thresholds={
                "min_edge": str(min_edge),
                "min_score": str(min_score),
                "max_spread": str(resolved_max_spread),
                "scan_limit": scan_limit,
            },
            selected_candidates=[candidate.payload() for candidate in state.candidates],
        )
        if refresh_metrics:
            from kalshi_predictor.lanes.metrics import refresh_learning_metrics

            refresh_learning_metrics(session, settings=exploration_settings)

    payload = ExplorationSeedResult(
        generated_at=now.isoformat(),
        mode=(
            "PAPER_ONLY_EXPLORATORY_SEED_APPLY"
            if apply
            else "PAPER_ONLY_EXPLORATORY_SEED_DRY_RUN"
        ),
        model_name=model_name,
        apply=apply,
        candidates_scanned=state.scanned,
        candidates_found=len(state.candidates),
        paper_orders_created=len(created_orders),
        fills_created=fills_created,
        learning_paper_trades_inserted=learning_rows,
        settled_paper_trades_total=settled_paper_trade_count(session),
        reason_counts=dict(sorted(state.reason_counts.items(), key=lambda item: (-item[1], item[0]))),
        selected_candidates=[candidate.payload() for candidate in state.candidates[:20]],
        created_orders=created_orders,
        safety={
            "paper_only": True,
            "live_execution": "blocked",
            "demo_execution": "blocked",
            "exchange_order_submission": "blocked",
            "max_trades_per_run": bounded_max_trades,
            "default_mode": "dry_run",
        },
        thresholds={
            "min_edge": str(min_edge),
            "min_score": str(min_score),
            "max_spread": str(resolved_max_spread),
            "scan_limit": scan_limit,
            "ranking_fetch_limit": ranking_fetch_limit,
            "max_ranking_age_minutes": max_ranking_age_minutes,
            "refresh_metrics": refresh_metrics,
            "duplicate_cooldown_hours": exploration_settings.learning_duplicate_cooldown_hours,
            "paper_max_position_per_market": exploration_settings.paper_max_position_per_market,
        },
    )
    return _write_result(payload, output_dir=output_dir)


def _scan_candidates(
    session: Session,
    *,
    settings: Settings,
    model_name: str,
    min_edge: Decimal,
    min_score: Decimal,
    max_spread: Decimal,
    max_trades: int,
    scan_limit: int,
    ranking_fetch_limit: int,
    max_ranking_age_minutes: int,
    now: datetime,
) -> _ScanState:
    state = _ScanState()
    open_orders = open_order_count(session)
    daily_trades = _daily_paper_trade_count(session, now=now)
    rankings = _latest_rankings(
        session,
        model_name=model_name,
        fetch_limit=ranking_fetch_limit,
        scan_limit=scan_limit,
    )
    for ranking in rankings:
        if max_trades and len(state.candidates) >= max_trades:
            break
        state.scanned += 1
        reason = _ranking_precheck_reason(
            ranking,
            max_ranking_age_minutes=max_ranking_age_minutes,
            now=now,
        )
        if reason is not None:
            state.reject(reason)
            continue
        if not is_ticker_eligible_for_new_forecasts(session, ranking.ticker):
            state.reject("inactive_market")
            continue
        forecast = _latest_forecast(session, ticker=ranking.ticker, model_name=model_name)
        if forecast is None:
            state.reject("missing_latest_forecast")
            continue
        snapshot = _latest_snapshot(session, ticker=ranking.ticker)
        if snapshot is None:
            state.reject("missing_market_snapshot")
            continue
        decision = _best_exploratory_decision(
            forecast,
            snapshot,
            ranking,
            settings=settings,
            min_edge=min_edge,
        )
        if decision is None:
            state.reject("below_exploration_edge")
            continue
        score = to_decimal(ranking.opportunity_score) or Decimal("0")
        if score < min_score:
            state.reject("low_score")
            continue
        spread = _first_decimal(ranking.spread, snapshot.spread)
        if spread is None:
            state.reject("missing_spread")
            continue
        if spread > max_spread:
            state.reject("wide_spread")
            continue
        if decision.market_price <= Decimal("0"):
            state.reject("missing_executable_price")
            continue
        if is_duplicate_candidate(
            session,
            ticker=decision.ticker,
            model_name=decision.model_name,
            side=decision.side,
            cooldown_hours=settings.learning_duplicate_cooldown_hours,
            pending_keys=state.selected_keys,
        ):
            state.reject("duplicate_trade_recent")
            continue
        if open_orders + len(state.candidates) >= settings.paper_max_open_orders:
            state.reject("paper_open_order_cap")
            continue
        if daily_trades + len(state.candidates) >= settings.learning_max_daily_paper_trades:
            state.reject("daily_cap")
            continue
        if _position_limit_exceeded(session, decision, settings=settings):
            state.reject("position_limit")
            continue
        category = classify_market_category(ranking.title or "") or "unknown"
        state.selected_keys.add((decision.ticker, decision.model_name, decision.side))
        state.candidates.append(
            _Candidate(
                ranking=ranking,
                forecast=forecast,
                snapshot=snapshot,
                decision=decision,
                score=score,
                spread=spread,
                category=category,
                reason="exploratory_near_miss_paper_sample",
                ranking_age_minutes=_age_minutes(ranking.ranked_at, now=now),
            )
        )
    return state


def _latest_rankings(
    session: Session,
    *,
    model_name: str,
    fetch_limit: int,
    scan_limit: int,
) -> list[MarketRanking]:
    rows = list(
        session.scalars(
            select(MarketRanking)
            .where(MarketRanking.forecast_model == model_name)
            .order_by(desc(MarketRanking.ranked_at), desc(MarketRanking.id))
            .limit(fetch_limit)
        )
    )
    latest: dict[str, MarketRanking] = {}
    for row in rows:
        latest.setdefault(row.ticker, row)
    return sorted(
        latest.values(),
        key=lambda row: (
            to_decimal(row.opportunity_score) or Decimal("0"),
            row.ranked_at,
            row.id or 0,
        ),
        reverse=True,
    )[:scan_limit]


def _latest_forecast(
    session: Session,
    *,
    ticker: str,
    model_name: str,
) -> Forecast | None:
    return session.scalar(
        select(Forecast)
        .where(Forecast.ticker == ticker, Forecast.model_name == model_name)
        .order_by(desc(Forecast.forecasted_at), desc(Forecast.id))
        .limit(1)
    )


def _latest_snapshot(session: Session, *, ticker: str) -> MarketSnapshot | None:
    return session.scalar(
        select(MarketSnapshot)
        .where(MarketSnapshot.ticker == ticker)
        .order_by(desc(MarketSnapshot.captured_at), desc(MarketSnapshot.id))
        .limit(1)
    )


def _ranking_precheck_reason(
    ranking: MarketRanking,
    *,
    max_ranking_age_minutes: int,
    now: datetime,
) -> str | None:
    age = _age_minutes(ranking.ranked_at, now=now)
    if age is None or age > max_ranking_age_minutes:
        return "stale_ranking"
    minutes_to_close = to_decimal(ranking.time_to_close_minutes)
    if minutes_to_close is not None and minutes_to_close <= 0:
        return "expired_window"
    status = str(ranking.status or "").lower()
    if status and status not in {"active", "open"}:
        return "inactive_market"
    return None


def _best_exploratory_decision(
    forecast: Forecast,
    snapshot: MarketSnapshot,
    ranking: MarketRanking,
    *,
    settings: Settings,
    min_edge: Decimal,
) -> PaperDecision | None:
    yes_probability = to_decimal(forecast.yes_probability)
    if yes_probability is None:
        return None
    choices: list[PaperDecision] = []
    yes_ask = _first_decimal(forecast.best_yes_ask, snapshot.best_yes_ask)
    if yes_ask is not None:
        yes_edge = yes_probability - yes_ask
        if yes_edge >= min_edge:
            choices.append(
                _decision(
                    forecast=forecast,
                    ranking=ranking,
                    side=BUY_YES,
                    probability=yes_probability,
                    market_price=yes_ask,
                    edge=yes_edge,
                    quantity=settings.paper_max_order_quantity,
                    min_edge=min_edge,
                )
            )
    no_ask = _first_decimal(snapshot.best_no_ask)
    if settings.paper_allow_buy_no and no_ask is not None:
        no_probability = ONE_DOLLAR - yes_probability
        no_edge = no_probability - no_ask
        if no_edge >= min_edge:
            choices.append(
                _decision(
                    forecast=forecast,
                    ranking=ranking,
                    side=BUY_NO,
                    probability=yes_probability,
                    market_price=no_ask,
                    edge=no_edge,
                    quantity=settings.paper_max_order_quantity,
                    min_edge=min_edge,
                )
            )
    if not choices:
        return None
    return max(choices, key=lambda item: item.edge)


def _decision(
    *,
    forecast: Forecast,
    ranking: MarketRanking,
    side: str,
    probability: Decimal,
    market_price: Decimal,
    edge: Decimal,
    quantity: int,
    min_edge: Decimal,
) -> PaperDecision:
    reason = (
        f"{side} exploratory paper-only near-miss sample; edge {edge} "
        f"meets exploration floor {min_edge}; no live/demo/exchange order writes."
    )
    raw = {
        "forecast_id": forecast.id,
        "ranking_id": ranking.id,
        "forecasted_at": forecast.forecasted_at.isoformat(),
        "ranked_at": ranking.ranked_at.isoformat(),
        "model_name": forecast.model_name,
        "side": side,
        "probability": str(probability),
        "market_price": str(market_price),
        "limit_price": str(market_price),
        "edge": str(edge),
        "quantity": quantity,
        "reason": reason,
        "strategy": "paper_exploratory_near_miss_v1",
        "safety": {
            "paper_only": True,
            "live_execution": "blocked",
            "demo_execution": "blocked",
            "exchange_order_submission": "blocked",
        },
    }
    return PaperDecision(
        ticker=forecast.ticker,
        forecast_id=forecast.id,
        model_name=forecast.model_name,
        side=side,
        probability=probability,
        market_price=market_price,
        limit_price=market_price,
        edge=edge,
        quantity=quantity,
        reason=reason,
        raw_decision_json=raw,
    )


def _position_limit_exceeded(
    session: Session,
    decision: PaperDecision,
    *,
    settings: Settings,
) -> bool:
    position = get_position(session, decision.ticker)
    yes_contracts = position.yes_contracts if position is not None else 0
    no_contracts = position.no_contracts if position is not None else 0
    if decision.side == BUY_YES:
        return yes_contracts + decision.quantity > settings.paper_max_position_per_market
    return no_contracts + decision.quantity > settings.paper_max_position_per_market


def _daily_paper_trade_count(session: Session, *, now: datetime) -> int:
    today = now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return int(
        session.scalar(
            select(func.count()).select_from(PaperOrder).where(PaperOrder.created_at >= today)
        )
        or 0
    )


def _record_learning_cycle(
    session: Session,
    *,
    settings: Settings,
    model_name: str,
    candidates_scanned: int,
    paper_orders_created: int,
    fills_created: int,
    thresholds: dict[str, Any],
    selected_candidates: list[dict[str, Any]],
) -> None:
    now = utc_now()
    run = LearningRun(
        started_at=now,
        completed_at=now,
        status="COMPLETED",
        cycles_completed=1,
        paper_trades_created=paper_orders_created,
        settlements_synced=0,
        starting_settled_trades=settled_paper_trade_count(session),
        ending_settled_trades=settled_paper_trade_count(session),
        target_settled_trades=settings.learning_target_settled_trades,
        config_json=encode_json(dict(learning_config_payload(settings))),
        summary_json=encode_json(
            {
                "source": "overnight-exploratory-paper-seed",
                "model_name": model_name,
                "paper_orders_created": paper_orders_created,
                "fills_created": fills_created,
                "thresholds": thresholds,
            }
        ),
        notes="Paper-only exploratory seed run.",
    )
    session.add(run)
    session.flush()
    cycle = LearningCycle(
        learning_run_id=run.id,
        cycle_number=1,
        started_at=now,
        completed_at=now,
        status="COMPLETED",
        markets_scanned=candidates_scanned,
        forecasts_generated=0,
        opportunities_found=len(selected_candidates),
        paper_trades_created=paper_orders_created,
        settlements_synced=0,
        settled_paper_trades_total=settled_paper_trade_count(session),
        errors_json=encode_json([]),
        summary_json=encode_json(
            {
                "mode": "PAPER_ONLY_EXPLORATORY_SEED_APPLY",
                "model_name": model_name,
                "paper_orders_created": paper_orders_created,
                "fills_created": fills_created,
                "thresholds": thresholds,
                "selected_candidates": selected_candidates[:20],
            }
        ),
    )
    session.add(cycle)


def _order_payload(order: PaperOrder) -> dict[str, Any]:
    return {
        "paper_order_id": order.id,
        "ticker": order.ticker,
        "model_name": order.model_name,
        "side": order.side,
        "edge": order.edge,
        "market_price": order.market_price,
        "quantity": order.quantity,
        "status": order.status,
        "created_at": _iso_or_none(order.created_at),
    }


def _write_result(
    result: ExplorationSeedResult,
    *,
    output_dir: Path,
) -> ExplorationSeedResult:
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = "exploratory_paper_seed_latest"
    json_path = output_dir / f"{stem}.json"
    markdown_path = output_dir / f"{stem}.md"
    data = {
        key: value
        for key, value in result.__dict__.items()
        if key not in {"json_path", "markdown_path"}
    }
    json_path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    markdown_path.write_text(_render_markdown(data), encoding="utf-8")
    return ExplorationSeedResult(
        **{
            **data,
            "json_path": str(json_path),
            "markdown_path": str(markdown_path),
        }
    )


def _render_markdown(payload: dict[str, Any]) -> str:
    candidates = payload.get("selected_candidates") or []
    orders = payload.get("created_orders") or []
    reasons = payload.get("reason_counts") or {}
    candidate_lines = [
        (
            f"- {row['ticker']} {row['side']} edge={row['edge']} "
            f"score={row['score']} price={row['market_price']}"
        )
        for row in candidates[:10]
    ] or ["- none"]
    order_lines = [
        (
            f"- order={row['paper_order_id']} {row['ticker']} {row['side']} "
            f"status={row['status']} edge={row['edge']}"
        )
        for row in orders[:10]
    ] or ["- none"]
    reason_lines = [f"- {reason}: {count}" for reason, count in reasons.items()] or ["- none"]
    safety = payload.get("safety") or {}
    return "\n".join(
        [
            "# Exploratory Paper Seed",
            "",
            f"Generated: {payload.get('generated_at')}",
            f"Mode: {payload.get('mode')}",
            f"Model: {payload.get('model_name')}",
            "",
            "Safety:",
            f"- Paper only: {safety.get('paper_only')}",
            f"- Live execution: {safety.get('live_execution')}",
            f"- Demo execution: {safety.get('demo_execution')}",
            f"- Exchange order submission: {safety.get('exchange_order_submission')}",
            f"- Max trades per run: {safety.get('max_trades_per_run')}",
            "",
            "Results:",
            f"- Candidates scanned: {payload.get('candidates_scanned')}",
            f"- Candidates found: {payload.get('candidates_found')}",
            f"- Paper orders created: {payload.get('paper_orders_created')}",
            f"- Fills created: {payload.get('fills_created')}",
            f"- Learning paper rows inserted: {payload.get('learning_paper_trades_inserted')}",
            f"- Settled paper trades total: {payload.get('settled_paper_trades_total')}",
            "",
            "Selected candidates:",
            *candidate_lines,
            "",
            "Created orders:",
            *order_lines,
            "",
            "Top rejection reasons:",
            *reason_lines[:12],
            "",
        ]
    )


def _first_decimal(*values: Any) -> Decimal | None:
    for value in values:
        parsed = to_decimal(value)
        if parsed is not None:
            return parsed
    return None


def _age_minutes(value: datetime | None, *, now: datetime) -> float | None:
    if value is None:
        return None
    parsed = value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return round((now - parsed.astimezone(timezone.utc)).total_seconds() / 60, 3)


def _iso_or_none(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    return str(value)
