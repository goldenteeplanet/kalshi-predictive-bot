from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.data.schema import MarketRanking
from kalshi_predictor.learning.targets import settlement_speed_score
from kalshi_predictor.paper.settlement_reconciliation import PAPER_ONLY_SAFETY
from kalshi_predictor.tournament.ranking import classify_market_category
from kalshi_predictor.utils.decimals import to_decimal
from kalshi_predictor.utils.time import utc_now


@dataclass(frozen=True)
class Phase3ABArtifactSet:
    output_dir: Path
    json_path: Path
    markdown_path: Path


FAST_BUCKETS = {"overdue", "0-6h", "6-24h"}
SLOW_BUCKETS = {"3-7d", "7d+", "unknown"}


def phase3ab_fast_settlement_settings(
    settings: Settings,
    *,
    max_days_to_settlement: int = 1,
    scan_limit: int = 500,
) -> Settings:
    return settings.model_copy(
        update={
            "learning_mode": True,
            "learning_prioritize_fast_settlement": True,
            "learning_max_days_to_settlement": max_days_to_settlement,
            "learning_candidate_scan_limit": scan_limit,
            "learning_block_demo_execution": True,
            "learning_block_live_execution": True,
            "execution_enabled": False,
            "execution_dry_run": True,
        }
    )


def build_learning_governor(
    session: Session,
    *,
    settings: Settings | None = None,
    model_name: str = "ensemble_v2",
    limit: int = 500,
) -> dict[str, Any]:
    resolved = settings or get_settings()
    session.flush()
    rows = _latest_rankings(session, model_name=model_name, limit=limit)
    candidates = [_candidate_row(row) for row in rows]
    fast = [row for row in candidates if row["route"] == "FAST_SETTLEMENT"]
    watch = [row for row in candidates if row["route"] == "WATCH"]
    avoid = [row for row in candidates if row["route"] == "SLOW_SETTLEMENT_AVOID"]
    governor_settings = phase3ab_fast_settlement_settings(resolved)
    return {
        "generated_at": utc_now().isoformat(),
        "phase": "3AB",
        "mode": "PAPER_ONLY_LEARNING_GOVERNOR",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "model_name": model_name,
        "summary": {
            "rankings_scanned": len(rows),
            "fast_settlement_candidates": len(fast),
            "watch_candidates": len(watch),
            "slow_settlement_avoids": len(avoid),
            "recommended_max_days_to_settlement": governor_settings.learning_max_days_to_settlement,
            "candidate_scan_limit": governor_settings.learning_candidate_scan_limit,
        },
        "route_counts": _route_counts(candidates),
        "category_counts": _category_counts(candidates),
        "top_fast_candidates": fast[:25],
        "watch_candidates": watch[:25],
        "slow_settlement_avoids": avoid[:25],
        "recommended_env": {
            "LEARNING_MODE": "true",
            "EXECUTION_ENABLED": "false",
            "LEARNING_PRIORITIZE_FAST_SETTLEMENT": "true",
            "LEARNING_MAX_DAYS_TO_SETTLEMENT": str(
                governor_settings.learning_max_days_to_settlement
            ),
            "LEARNING_CANDIDATE_SCAN_LIMIT": str(governor_settings.learning_candidate_scan_limit),
            "LEARNING_BLOCK_DEMO_EXECUTION": "true",
            "LEARNING_BLOCK_LIVE_EXECUTION": "true",
        },
        "recommended_next_action": _next_action(fast, avoid),
    }


def write_phase3ab_report(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3ab"),
    settings: Settings | None = None,
    model_name: str = "ensemble_v2",
    limit: int = 500,
) -> Phase3ABArtifactSet:
    payload = build_learning_governor(
        session,
        settings=settings,
        model_name=model_name,
        limit=limit,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "phase3ab_learning_governor.json"
    markdown_path = output_dir / "phase3ab_learning_governor.md"
    json_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    markdown_path.write_text(_render_markdown(payload), encoding="utf-8")
    return Phase3ABArtifactSet(output_dir, json_path, markdown_path)


def _latest_rankings(
    session: Session,
    *,
    model_name: str,
    limit: int,
) -> list[MarketRanking]:
    rows = list(
        session.scalars(
            select(MarketRanking)
            .where(MarketRanking.forecast_model == model_name)
            .order_by(desc(MarketRanking.ranked_at), desc(MarketRanking.id))
            .limit(max(limit * 2, limit))
        )
    )
    seen: set[str] = set()
    unique: list[MarketRanking] = []
    for row in rows:
        if row.ticker in seen:
            continue
        seen.add(row.ticker)
        unique.append(row)
        if len(unique) >= limit:
            break
    return unique


def _candidate_row(row: MarketRanking) -> dict[str, Any]:
    minutes = to_decimal(row.time_to_close_minutes)
    category = classify_market_category(
        " ".join(part for part in (row.title, row.series_ticker, row.event_ticker) if part)
    )
    bucket = _eta_bucket(minutes)
    route = _route(category, bucket, row)
    speed = settlement_speed_score(minutes)
    opportunity_score = to_decimal(row.opportunity_score) or Decimal("0")
    edge = to_decimal(row.estimated_edge) or Decimal("0")
    governor_score = (
        speed * Decimal("0.45")
        + opportunity_score * Decimal("0.35")
        + min(Decimal("100"), max(Decimal("0"), edge * Decimal("1000"))) * Decimal("0.20")
    ).quantize(Decimal("0.0001"))
    return {
        "ticker": row.ticker,
        "title": row.title,
        "category": category,
        "eta_bucket": bucket,
        "time_to_close_minutes": row.time_to_close_minutes,
        "opportunity_score": row.opportunity_score,
        "estimated_edge": row.estimated_edge,
        "governor_score": str(governor_score),
        "route": route,
        "reason": _route_reason(category, bucket, route),
    }


def _eta_bucket(minutes: Decimal | None) -> str:
    if minutes is None:
        return "unknown"
    if minutes <= 0:
        return "overdue"
    hours = minutes / Decimal("60")
    if hours <= 6:
        return "0-6h"
    if hours <= 24:
        return "6-24h"
    if hours <= 48:
        return "1-2d"
    if hours <= 72:
        return "2-3d"
    if hours <= 168:
        return "3-7d"
    return "7d+"


def _route(category: str, bucket: str, row: MarketRanking) -> str:
    text = " ".join(str(part or "") for part in (row.title, row.series_ticker, row.event_ticker))
    lowered = text.lower()
    if bucket in SLOW_BUCKETS:
        return "SLOW_SETTLEMENT_AVOID"
    if category == "sports" and any(token in lowered for token in ("multi", "series", "games")):
        return "SLOW_SETTLEMENT_AVOID"
    if bucket in FAST_BUCKETS:
        return "FAST_SETTLEMENT"
    return "WATCH"


def _route_reason(category: str, bucket: str, route: str) -> str:
    if route == "FAST_SETTLEMENT":
        return f"{category} market closes in {bucket}; prioritize for settled learning data."
    if route == "WATCH":
        return f"{category} market is not slow, but is outside the best overnight bucket."
    return f"{category} market is {bucket}; avoid for fast Learning Mode settlement capture."


def _route_counts(candidates: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in candidates:
        counts[row["route"]] = counts.get(row["route"], 0) + 1
    return dict(sorted(counts.items()))


def _category_counts(candidates: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in candidates:
        counts[row["category"]] = counts.get(row["category"], 0) + 1
    return dict(sorted(counts.items()))


def _next_action(fast: list[dict[str, Any]], avoid: list[dict[str, Any]]) -> str:
    if fast:
        return "Resume paper-only Learning Mode with the recommended fast-settlement env."
    if avoid:
        return "Collect more markets before learning; current candidates are too slow."
    return "Generate fresh rankings, then rerun phase3ab-learning-governor."


def _render_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Phase 3AB Learning Governor / Fast Settlement Router",
        "",
        f"- Generated at: {payload['generated_at']}",
        f"- Mode: {payload['mode']}",
        f"- Safety: {payload['paper_only_safety']}",
        "",
        "## Summary",
        "",
    ]
    for key, value in payload["summary"].items():
        lines.append(f"- {key}: {value}")
    lines.extend(["", "## Recommended Environment", "", "```bash"])
    for key, value in payload["recommended_env"].items():
        lines.append(f"export {key}={value}")
    lines.extend(
        [
            "```",
            "",
            "## Top Fast Candidates",
            "",
            "| Ticker | Category | ETA | Score | Reason |",
            "| --- | --- | --- | ---: | --- |",
        ]
    )
    if payload["top_fast_candidates"]:
        for row in payload["top_fast_candidates"][:20]:
            lines.append(
                f"| {row['ticker']} | {row['category']} | {row['eta_bucket']} | "
                f"{row['governor_score']} | {row['reason']} |"
            )
    else:
        lines.append("| None |  |  |  | No fast-settlement candidates found. |")
    lines.extend(
        [
            "",
            "## Slow Settlement Avoids",
            "",
            "| Ticker | Category | ETA | Reason |",
            "| --- | --- | --- | --- |",
        ]
    )
    if payload["slow_settlement_avoids"]:
        for row in payload["slow_settlement_avoids"][:20]:
            lines.append(
                f"| {row['ticker']} | {row['category']} | {row['eta_bucket']} | "
                f"{row['reason']} |"
            )
    else:
        lines.append("| None |  |  | No slow-settlement candidates found. |")
    lines.extend(["", "## Recommended Next Action", "", payload["recommended_next_action"], ""])
    return "\n".join(lines)
