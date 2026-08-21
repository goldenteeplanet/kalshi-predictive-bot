from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from kalshi_predictor.data.schema import (
    CryptoMarketLink,
    EconomicMarketLink,
    Forecast,
    Market,
    MarketLeg,
    MarketRanking,
    MarketSnapshot,
    NewsMarketLink,
    SportsMarketLink,
    WeatherMarketLink,
)
from kalshi_predictor.phase_gh2 import CRYPTO_TICKER_PREFIXES
from kalshi_predictor.utils.time import utc_now
from kalshi_predictor.weather.linker import WEATHER_TICKER_PREFIXES

CATEGORIES = (
    "crypto",
    "weather",
    "economic",
    "sports",
    "news",
    "cross_category",
    "general",
    "unknown",
)
STAGES = (
    "active_catalog",
    "parsed",
    "semantically_supported",
    "authoritative_link",
    "fresh_snapshot",
    "forecast",
    "ranking",
    "candidate_manifest",
    "positive_gross_ev",
    "paper_ready",
)


@dataclass(frozen=True)
class CategoryCoverageGapArtifacts:
    json_path: Path
    markdown_path: Path


def write_category_coverage_gap_audit(
    session: Session,
    *,
    gh1_manifest_path: Path,
    gh2_report_path: Path,
    output_dir: Path,
    freshness_minutes: int = 15,
) -> CategoryCoverageGapArtifacts:
    payload = build_category_coverage_gap_audit(
        session,
        manifest=_read_object(gh1_manifest_path),
        gh2=_read_object(gh2_report_path),
        freshness_minutes=freshness_minutes,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "category_coverage_gap_audit.json"
    markdown_path = output_dir / "category_coverage_gap_audit.md"
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    markdown_path.write_text(_markdown(payload), encoding="utf-8")
    return CategoryCoverageGapArtifacts(json_path, markdown_path)


def build_category_coverage_gap_audit(
    session: Session,
    *,
    manifest: dict[str, Any],
    gh2: dict[str, Any],
    freshness_minutes: int,
) -> dict[str, Any]:
    now = utc_now()
    fresh_after = now - timedelta(minutes=freshness_minutes)
    leg_categories: dict[str, set[str]] = defaultdict(set)
    for ticker, category in session.execute(select(MarketLeg.ticker, MarketLeg.category)):
        leg_categories[str(ticker)].add(str(category or "").strip().lower())

    links = _link_sets(session)
    snapshots = _latest(session, MarketSnapshot.ticker, MarketSnapshot.captured_at)
    forecasts = _latest(session, Forecast.ticker, Forecast.forecasted_at)
    rankings = _latest_rankings(session)
    manifest_tickers = _tickers(manifest)
    paper_ready_tickers = _paper_ready_tickers(gh2)

    rows: list[dict[str, Any]] = []
    for market in session.scalars(select(Market)):
        category = _category(market.ticker, leg_categories.get(market.ticker, set()))
        if category not in CATEGORIES:
            category = "unknown"
        active = _active(market.status, market.close_time, now)
        parsed = market.ticker in leg_categories
        unsupported_composite = category == "sports" and _unsupported_sports_composite(
            market, leg_categories.get(market.ticker, set())
        )
        supported = parsed and category in {
            "crypto", "weather", "economic", "sports", "news"
        } and not unsupported_composite
        linked = market.ticker in links.get(category, set())
        ranking = rankings.get(market.ticker)
        gross_ev = _gross_ev(ranking)
        ranking_raw = _ranking_raw(ranking)
        row = {
            "ticker": market.ticker,
            "category": category,
            "active_catalog": active,
            "parsed": parsed,
            "semantically_supported": supported,
            "authoritative_link": linked,
            "fresh_snapshot": _fresh(snapshots.get(market.ticker), fresh_after),
            "forecast": _fresh(forecasts.get(market.ticker), fresh_after),
            "ranking": _fresh(ranking.ranked_at if ranking else None, fresh_after),
            "candidate_manifest": market.ticker in manifest_tickers,
            "positive_gross_ev": gross_ev is not None and gross_ev > 0,
            "paper_ready": market.ticker in paper_ready_tickers,
            "gross_expected_value": str(gross_ev) if gross_ev is not None else None,
            "liquidity_score": ranking.liquidity_score if ranking else None,
            "executable_book": ranking_raw.get("executable_book"),
            "unsupported_composite": unsupported_composite,
        }
        row["first_blocker"] = _first_blocker(row)
        row["limitation_class"] = _limitation_class(str(row["first_blocker"]), row)
        rows.append(row)

    categories = {}
    for category in CATEGORIES:
        active_rows = [row for row in rows if row["category"] == category and row["active_catalog"]]
        surviving = active_rows
        counts = {"active_catalog": len(active_rows)}
        for stage in STAGES[1:]:
            surviving = [row for row in surviving if row[stage]]
            counts[stage] = len(surviving)
        blockers = Counter(str(row["first_blocker"]) for row in active_rows)
        categories[category] = {
            "counts": counts,
            "first_blocker": _dominant_blocker(blockers, active_rows),
            "first_blocker_counts": dict(sorted(blockers.items())),
            "limitation_classes": dict(
                sorted(Counter(str(row["limitation_class"]) for row in active_rows).items())
            ),
        }
    return {
        "audit_version": "category_coverage_gap_audit_v1",
        "generated_at": now.isoformat(),
        "mode": "READ_ONLY_CATEGORY_COVERAGE_DIAGNOSTIC",
        "safety": {
            "database_open_mode": "sqlite_mode_ro_query_only",
            "database_writes": 0,
            "gh2_triggered": False,
            "order_creation": False,
            "thresholds_changed": False,
            "safety_settings_changed": False,
        },
        "freshness_minutes": freshness_minutes,
        "categories": categories,
        "active_rows": [row for row in rows if row["active_catalog"]],
    }


def _link_sets(session: Session) -> dict[str, set[str]]:
    def values(model: Any) -> set[str]:
        return {str(value) for value in session.scalars(select(model.ticker).distinct())}

    return {
        "crypto": values(CryptoMarketLink),
        "weather": values(WeatherMarketLink),
        "economic": values(EconomicMarketLink),
        "sports": values(SportsMarketLink),
        "news": values(NewsMarketLink),
    }


def _category(ticker: str, categories: set[str]) -> str:
    if ticker.startswith(CRYPTO_TICKER_PREFIXES):
        return "crypto"
    if ticker.startswith(WEATHER_TICKER_PREFIXES):
        return "weather"
    normalized = {value for value in categories if value in CATEGORIES}
    if len(normalized) == 1:
        return next(iter(normalized))
    if len(normalized) > 1:
        return "cross_category"
    return "unknown"


def _unsupported_sports_composite(market: Market, categories: set[str]) -> bool:
    identity = " ".join(
        str(value or "") for value in (market.ticker, market.series_ticker, market.event_ticker)
    ).upper()
    return "KXMVE" in identity and (len(categories) > 1 or "MULTI" in identity)


def _first_blocker(row: dict[str, Any]) -> str:
    for stage in STAGES:
        if not row[stage]:
            return {
                "active_catalog": "NO_ACTIVE_OPPORTUNITY",
                "parsed": "MISSING_INGESTION",
                "semantically_supported": "UNSUPPORTED_SEMANTICS",
                "authoritative_link": "MISSING_AUTHORITATIVE_LINK",
                "fresh_snapshot": "MISSING_OR_STALE_SNAPSHOT",
                "forecast": "MISSING_MODEL_FORECAST",
                "ranking": "MISSING_OR_STALE_RANKING",
                "candidate_manifest": "NOT_SELECTED_IN_CANDIDATE_MANIFEST",
                "positive_gross_ev": "PROFITABILITY_REJECTION",
                "paper_ready": "PAPER_READINESS_GATE",
            }[stage]
    return "PAPER_READY"


def _limitation_class(blocker: str, row: dict[str, Any]) -> str:
    if blocker == "PAPER_READINESS_GATE":
        liquidity_score = _decimal(row.get("liquidity_score"))
        if liquidity_score is None or liquidity_score <= 0:
            return "liquidity_limitation"
        if row.get("executable_book") is False:
            return "liquidity_limitation"
        return "paper_readiness_gate"
    return {
        "NO_ACTIVE_OPPORTUNITY": "absent_active_opportunity",
        "MISSING_INGESTION": "missing_ingestion",
        "UNSUPPORTED_SEMANTICS": "unsupported_semantics",
        "MISSING_AUTHORITATIVE_LINK": "missing_authoritative_linking",
        "MISSING_OR_STALE_SNAPSHOT": "freshness_or_scheduling",
        "MISSING_MODEL_FORECAST": "missing_model_support",
        "MISSING_OR_STALE_RANKING": "freshness_or_scheduling",
        "NOT_SELECTED_IN_CANDIDATE_MANIFEST": "scheduling_or_selection",
        "PROFITABILITY_REJECTION": "profitability_rejection",
        "PAPER_READY": "none",
    }[blocker]


def _dominant_blocker(blockers: Counter[str], rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "NO_ACTIVE_OPPORTUNITY"
    return sorted(blockers.items(), key=lambda item: (-item[1], item[0]))[0][0]


def _latest(session: Session, ticker: Any, timestamp: Any) -> dict[str, datetime]:
    return {
        str(key): value
        for key, value in session.execute(select(ticker, func.max(timestamp)).group_by(ticker))
        if value is not None
    }


def _latest_rankings(session: Session) -> dict[str, MarketRanking]:
    ids: dict[str, int] = {
        str(ticker): int(ranking_id)
        for ticker, ranking_id in session.execute(
            select(MarketRanking.ticker, func.max(MarketRanking.id)).group_by(MarketRanking.ticker)
        )
        if ranking_id is not None
    }
    if not ids:
        return {}
    return {
        row.ticker: row
        for row in session.scalars(select(MarketRanking).where(MarketRanking.id.in_(ids.values())))
    }


def _gross_ev(ranking: MarketRanking | None) -> Decimal | None:
    if ranking is None:
        return None
    try:
        raw = json.loads(ranking.raw_json or "{}")
        value = raw.get("gross_expected_value", ranking.estimated_edge)
        return Decimal(str(value)) if value not in (None, "") else None
    except (json.JSONDecodeError, InvalidOperation, TypeError, ValueError):
        return None


def _ranking_raw(ranking: MarketRanking | None) -> dict[str, Any]:
    if ranking is None:
        return {}
    try:
        value = json.loads(ranking.raw_json or "{}")
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _active(status: Any, close_time: datetime | None, now: datetime) -> bool:
    if str(status or "").lower() not in {"active", "open"}:
        return False
    return close_time is None or _aware(close_time) > now


def _fresh(value: datetime | None, cutoff: datetime) -> bool:
    return value is not None and _aware(value) >= cutoff


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=utc_now().tzinfo)


def _read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _tickers(payload: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(payload, dict):
        ticker = payload.get("ticker")
        if isinstance(ticker, str) and ticker:
            found.add(ticker)
        for value in payload.values():
            found.update(_tickers(value))
    elif isinstance(payload, list):
        for value in payload:
            found.update(_tickers(value))
    return found


def _paper_ready_tickers(payload: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(payload, dict):
        if payload.get("paper_ready") is True and isinstance(payload.get("ticker"), str):
            found.add(payload["ticker"])
        for value in payload.values():
            found.update(_paper_ready_tickers(value))
    elif isinstance(payload, list):
        for value in payload:
            found.update(_paper_ready_tickers(value))
    return found


def _markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Category Coverage Gap Audit",
        "",
        "- Mode: read only",
        "- Database: SQLite mode=ro; PRAGMA query_only=ON",
        "- Database writes: 0",
        "",
        "| Category | Active | Parsed | Supported | Linked | Snapshot | Forecast | "
        "Ranking | Manifest | +Gross EV | Paper ready | First blocker |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for category in CATEGORIES:
        item = payload["categories"][category]
        count = item["counts"]
        lines.append(
            f"| {category} | {count['active_catalog']} | {count['parsed']} | "
            f"{count['semantically_supported']} | {count['authoritative_link']} | "
            f"{count['fresh_snapshot']} | {count['forecast']} | {count['ranking']} | "
            f"{count['candidate_manifest']} | {count['positive_gross_ev']} | "
            f"{count['paper_ready']} | {item['first_blocker']} |"
        )
    return "\n".join(lines) + "\n"
