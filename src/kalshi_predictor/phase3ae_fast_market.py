from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.data.schema import Market, MarketRanking
from kalshi_predictor.learning.targets import settlement_speed_score
from kalshi_predictor.paper.settlement_reconciliation import PAPER_ONLY_SAFETY
from kalshi_predictor.phase3ab import FAST_BUCKETS, _candidate_row, _eta_bucket, _latest_rankings
from kalshi_predictor.tournament.ranking import classify_market_category
from kalshi_predictor.utils.decimals import to_decimal
from kalshi_predictor.utils.time import parse_datetime, utc_now

PHASE_3AE_FAST_MARKET_VERSION = "phase3ae_fast_market_harvester_v1"


@dataclass(frozen=True)
class Phase3AEFastMarketArtifactSet:
    output_dir: Path
    json_path: Path
    markdown_path: Path


def build_fast_market_harvester(
    session: Session,
    *,
    settings: Settings | None = None,
    model_name: str = "ensemble_v2",
    ranking_limit: int = 500,
    market_limit: int = 500,
    horizon_hours: int = 24,
) -> dict[str, Any]:
    """Build a read-only route map for getting more 0-24h markets into learning.

    The harvester does not create paper trades. It checks whether the current ranked
    universe contains fast-settlement candidates and identifies open 0-24h markets
    that still need forecast/ranking coverage before Learning Mode can use them.
    """

    resolved = settings or get_settings()
    now = utc_now()
    horizon = now + timedelta(hours=max(horizon_hours, 1))
    session.flush()

    ranking_rows = _latest_rankings(
        session,
        model_name=model_name,
        limit=max(ranking_limit, 1),
    )
    routed_rows = [_candidate_row(row) for row in ranking_rows]
    fast_ranked = [row for row in routed_rows if row["route"] == "FAST_SETTLEMENT"]
    watch_ranked = [row for row in routed_rows if row["route"] == "WATCH"]
    slow_ranked = [row for row in routed_rows if row["route"] == "SLOW_SETTLEMENT_AVOID"]

    open_fast_markets = _open_fast_markets(
        session,
        now=now,
        horizon=horizon,
        limit=max(market_limit, 1),
    )
    ranked_tickers = {row.ticker for row in ranking_rows}
    unranked_fast = [
        _market_gap_row(market, now=now)
        for market in open_fast_markets
        if market.ticker not in ranked_tickers
    ]
    stale_or_missing_rankings = _stale_or_missing_ranking_rows(
        open_fast_markets,
        rankings_by_ticker={row.ticker: row for row in ranking_rows},
        now=now,
    )

    route_counts = _route_counts(routed_rows)
    bucket_counts = _bucket_counts(routed_rows)
    category_counts = _category_counts(routed_rows)
    open_fast_category_counts = _category_counts(unranked_fast)
    command_queue = _command_queue(
        fast_ranked=fast_ranked,
        unranked_fast=unranked_fast,
        stale_or_missing_rankings=stale_or_missing_rankings,
        model_name=model_name,
    )
    summary = {
        "rankings_scanned": len(ranking_rows),
        "ranked_fast_settlement_candidates": len(fast_ranked),
        "ranked_watch_candidates": len(watch_ranked),
        "ranked_slow_settlement_avoids": len(slow_ranked),
        "open_0_24h_markets_seen": len(open_fast_markets),
        "open_0_24h_markets_missing_current_ranking": len(unranked_fast),
        "open_0_24h_markets_stale_or_missing_ranking": len(stale_or_missing_rankings),
        "horizon_hours": max(horizon_hours, 1),
        "execution_enabled": bool(resolved.execution_enabled),
        "learning_mode": bool(resolved.learning_mode),
        "paper_trade_creation_allowed": False,
    }
    return {
        "generated_at": now.isoformat(),
        "phase": "3AE",
        "phase_title": "Fast Market Harvester",
        "phase_version": PHASE_3AE_FAST_MARKET_VERSION,
        "mode": "PAPER_ONLY_READ_ONLY_HARVESTER",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "live_or_demo_execution": False,
        "order_submission": False,
        "model_name": model_name,
        "summary": summary,
        "route_counts": route_counts,
        "eta_bucket_counts": bucket_counts,
        "category_counts": category_counts,
        "open_0_24h_missing_ranking_category_counts": open_fast_category_counts,
        "top_fast_ranked_candidates": fast_ranked[:25],
        "open_0_24h_markets_missing_current_ranking": unranked_fast[:50],
        "open_0_24h_markets_stale_or_missing_ranking": stale_or_missing_rankings[:50],
        "recommended_commands": command_queue,
        "recommended_next_action": _recommended_next_action(
            fast_ranked=fast_ranked,
            unranked_fast=unranked_fast,
            stale_or_missing_rankings=stale_or_missing_rankings,
        ),
        "safety": {
            "creates_paper_trades": False,
            "submits_exchange_orders": False,
            "enables_execution": False,
            "enables_demo": False,
            "requires_human_approval_before_trade_creation": True,
        },
    }


def write_phase3ae_fast_market_harvester_report(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3ae_fast_market"),
    settings: Settings | None = None,
    model_name: str = "ensemble_v2",
    ranking_limit: int = 500,
    market_limit: int = 500,
    horizon_hours: int = 24,
) -> Phase3AEFastMarketArtifactSet:
    payload = build_fast_market_harvester(
        session,
        settings=settings,
        model_name=model_name,
        ranking_limit=ranking_limit,
        market_limit=market_limit,
        horizon_hours=horizon_hours,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "phase3ae_fast_market_harvester.json"
    markdown_path = output_dir / "phase3ae_fast_market_harvester.md"
    json_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    markdown_path.write_text(_render_markdown(payload), encoding="utf-8")
    return Phase3AEFastMarketArtifactSet(output_dir, json_path, markdown_path)


def _open_fast_markets(
    session: Session,
    *,
    now: Any,
    horizon: Any,
    limit: int,
) -> list[Market]:
    rows = list(
        session.scalars(
            select(Market)
            .where(Market.status.in_(("open", "active")))
            .where(
                or_(
                    Market.close_time.between(now, horizon),
                    Market.expected_expiration_time.between(now, horizon),
                    Market.expiration_time.between(now, horizon),
                )
            )
            .order_by(
                Market.close_time.is_(None),
                Market.close_time,
                Market.expected_expiration_time,
                Market.expiration_time,
                Market.ticker,
            )
            .limit(limit)
        )
    )
    return rows


def _market_gap_row(market: Market, *, now: Any) -> dict[str, Any]:
    close_time = _market_close_time(market)
    minutes = _minutes_to_close(close_time, now=now)
    category = classify_market_category(
        " ".join(
            part
            for part in (market.title, market.subtitle, market.series_ticker, market.event_ticker)
            if part
        )
    )
    speed = settlement_speed_score(minutes)
    return {
        "ticker": market.ticker,
        "title": market.title,
        "status": market.status,
        "series_ticker": market.series_ticker,
        "event_ticker": market.event_ticker,
        "category": category,
        "close_time": close_time.isoformat() if close_time else None,
        "time_to_close_minutes": str(minutes) if minutes is not None else None,
        "eta_bucket": _eta_bucket(minutes),
        "settlement_speed_score": str(speed),
        "harvest_gap": "MISSING_CURRENT_RANKING",
        "next_action": (
            "Refresh forecast/ranking coverage before Learning Mode can route this market."
        ),
    }


def _stale_or_missing_ranking_rows(
    markets: list[Market],
    *,
    rankings_by_ticker: dict[str, MarketRanking],
    now: Any,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for market in markets:
        ranking = rankings_by_ticker.get(market.ticker)
        if ranking is None:
            rows.append(_market_gap_row(market, now=now))
            continue
        minutes = to_decimal(ranking.time_to_close_minutes)
        if _eta_bucket(minutes) not in FAST_BUCKETS:
            row = _market_gap_row(market, now=now)
            row["harvest_gap"] = "RANKING_NOT_FAST_SETTLEMENT"
            row["ranking_time_to_close_minutes"] = ranking.time_to_close_minutes
            row["ranking_eta_bucket"] = _eta_bucket(minutes)
            row["next_action"] = (
                "Regenerate rankings; market timestamp says 0-24h but ranking route is not fast."
            )
            rows.append(row)
    return rows


def _market_close_time(market: Market) -> Any | None:
    return (
        parse_datetime(market.close_time)
        or parse_datetime(market.expected_expiration_time)
        or parse_datetime(market.expiration_time)
    )


def _minutes_to_close(close_time: Any | None, *, now: Any) -> Decimal | None:
    if close_time is None:
        return None
    seconds = (close_time - now).total_seconds()
    return Decimal(str(seconds)) / Decimal("60")


def _route_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        key = str(row.get("route") or "UNKNOWN")
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def _bucket_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        key = str(row.get("eta_bucket") or "unknown")
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def _category_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        key = str(row.get("category") or "unknown")
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def _command_queue(
    *,
    fast_ranked: list[dict[str, Any]],
    unranked_fast: list[dict[str, Any]],
    stale_or_missing_rankings: list[dict[str, Any]],
    model_name: str,
) -> list[dict[str, Any]]:
    commands: list[dict[str, Any]] = []
    if unranked_fast or stale_or_missing_rankings:
        commands.extend(
            [
                {
                    "command": (
                        "kalshi-bot phase3ay-health-refresh --cycles 1 "
                        "--interval-seconds 0 --all-markets"
                    ),
                    "purpose": (
                        "Refresh paper/market health before ranking newly harvested fast markets."
                    ),
                    "writes_trades": False,
                },
                {
                    "command": f"kalshi-bot forecast --model {model_name}",
                    "purpose": (
                        "Create forecasts for open 0-24h markets that lack current rankings."
                    ),
                    "writes_trades": False,
                },
                {
                    "command": f"kalshi-bot find-opportunities --model-name {model_name}",
                    "purpose": (
                        "Regenerate ranked opportunity rows for the fast-settlement universe."
                    ),
                    "writes_trades": False,
                },
                {
                    "command": (
                        "kalshi-bot phase3ae-fast-market-harvester "
                        f"--model-name {model_name}"
                    ),
                    "purpose": (
                        "Confirm fast candidates exist before paper trade creation is considered."
                    ),
                    "writes_trades": False,
                },
            ]
        )
    commands.append(
        {
            "command": f"kalshi-bot phase3ab-learning-governor --model-name {model_name}",
            "purpose": "Route ranked 0-24h candidates into paper-only Learning Mode settings.",
            "writes_trades": False,
        }
    )
    if fast_ranked:
        commands.append(
            {
                "command": "kalshi-bot learning-status",
                "purpose": (
                    "Check paper-only learning capacity before any human-approved paper run."
                ),
                "writes_trades": False,
            }
        )
    return commands


def _recommended_next_action(
    *,
    fast_ranked: list[dict[str, Any]],
    unranked_fast: list[dict[str, Any]],
    stale_or_missing_rankings: list[dict[str, Any]],
) -> str:
    if fast_ranked:
        return (
            "Fast ranked candidates exist; run phase3ab-learning-governor before any "
            "human-approved paper run."
        )
    if unranked_fast or stale_or_missing_rankings:
        return "Refresh forecasts/rankings for open 0-24h markets, then rerun this harvester."
    return "Collect more open markets; no 0-24h ranked or rankable markets were found."


def _render_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Phase 3AE Fast Market Harvester",
        "",
        f"- Generated at: {payload['generated_at']}",
        f"- Mode: {payload['mode']}",
        f"- Safety: {payload['paper_only_safety']}",
        f"- Live/demo execution: {payload['live_or_demo_execution']}",
        f"- Order submission: {payload['order_submission']}",
        "",
        "## Summary",
        "",
    ]
    for key, value in payload["summary"].items():
        lines.append(f"- {key}: {value}")
    lines.extend(
        [
            "",
            "## Top Fast Ranked Candidates",
            "",
            "| Ticker | Category | ETA | Score | Reason |",
            "| --- | --- | --- | ---: | --- |",
        ]
    )
    if payload["top_fast_ranked_candidates"]:
        for row in payload["top_fast_ranked_candidates"][:20]:
            lines.append(
                f"| {row['ticker']} | {row['category']} | {row['eta_bucket']} | "
                f"{row['governor_score']} | {row['reason']} |"
            )
    else:
        lines.append("| None |  |  |  | No ranked fast-settlement candidates found. |")
    lines.extend(
        [
            "",
            "## Open 0-24h Markets Missing Current Ranking",
            "",
            "| Ticker | Category | ETA | Gap | Next action |",
            "| --- | --- | --- | --- | --- |",
        ]
    )
    if payload["open_0_24h_markets_missing_current_ranking"]:
        for row in payload["open_0_24h_markets_missing_current_ranking"][:20]:
            lines.append(
                f"| {row['ticker']} | {row['category']} | {row['eta_bucket']} | "
                f"{row['harvest_gap']} | {row['next_action']} |"
            )
    else:
        lines.append("| None |  |  | No missing-ranking 0-24h markets found. |")
    lines.extend(["", "## Recommended Command Queue", ""])
    for item in payload["recommended_commands"]:
        lines.append(f"- `{item['command']}` - {item['purpose']}")
    lines.extend(["", "## Recommended Next Action", "", payload["recommended_next_action"], ""])
    return "\n".join(lines)
