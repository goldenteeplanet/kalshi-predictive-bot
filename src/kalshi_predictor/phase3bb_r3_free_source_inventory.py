from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.data.schema import (
    CryptoFeature,
    CryptoMarketLink,
    EconomicEvent,
    EconomicMarketLink,
    Forecast,
    Market,
    MarketLeg,
    MarketRanking,
    NewsItem,
    NewsMarketLink,
    SportsGame,
    SportsMarketLink,
    WeatherForecast,
    WeatherMarketLink,
)
from kalshi_predictor.phase3bb_acceleration import (
    _metadata,
    _metadata_lines,
    _read_json,
    _safety_flags,
    _write_manifest,
)
from kalshi_predictor.utils.time import utc_now

PHASE3BB_R3_VERSION = "phase3bb_r3_free_source_inventory_v1"

ACTIVE_MARKET_STATUSES = ("open", "active")
INVENTORY_CATEGORIES = (
    "weather",
    "sports",
    "economic",
    "news",
    "agriculture_usda",
    "transportation",
    "general",
    "cross_category",
    "crypto",
)

CATEGORY_ALIASES = {
    "weather": ("weather",),
    "sports": ("sports",),
    "economic": ("economic",),
    "news": ("news",),
    "agriculture_usda": ("agriculture", "agriculture_usda", "agriculture_commodities"),
    "transportation": ("transportation", "transportation_flight"),
    "general": ("general",),
    "cross_category": ("cross_category",),
    "crypto": ("crypto",),
}

MODEL_SELECTORS = {
    "weather": ("exact", "weather_v2"),
    "sports": ("prefix", "sports"),
    "economic": ("prefix", "economic"),
    "news": ("prefix", "news"),
    "agriculture_usda": ("prefix", "agriculture"),
    "transportation": ("prefix", "transportation"),
    "general": ("prefix", "general"),
    "cross_category": ("prefix", "cross_category"),
    "crypto": ("exact", "crypto_v2"),
}


@dataclass(frozen=True)
class SourceFamily:
    free_source_options: tuple[str, ...]
    official_source_options: tuple[str, ...]
    paid_deferred_sources: tuple[str, ...]


SOURCE_FAMILIES: dict[str, SourceFamily] = {
    "weather": SourceFamily(
        free_source_options=("NOAA/NWS", "Open-Meteo"),
        official_source_options=("NOAA/NWS",),
        paid_deferred_sources=("commercial weather APIs deferred",),
    ),
    "sports": SourceFamily(
        free_source_options=("official league schedules", "official team schedule pages"),
        official_source_options=("league/team schedule and roster pages",),
        paid_deferred_sources=("paid sports data/odds APIs deferred",),
    ),
    "economic": SourceFamily(
        free_source_options=("FRED", "BLS", "BEA", "Census", "Treasury", "Federal Reserve"),
        official_source_options=("FRED", "BLS", "BEA", "Census", "Treasury", "Federal Reserve"),
        paid_deferred_sources=("TradingEconomics=DEFERRED",),
    ),
    "news": SourceFamily(
        free_source_options=("official public pages", "official RSS feeds"),
        official_source_options=("agency/company official pages and RSS",),
        paid_deferred_sources=("paid news APIs deferred",),
    ),
    "agriculture_usda": SourceFamily(
        free_source_options=("USDA AMS", "USDA NASS"),
        official_source_options=("USDA AMS", "USDA NASS"),
        paid_deferred_sources=("paid commodity data feeds deferred",),
    ),
    "transportation": SourceFamily(
        free_source_options=("FAA", "DOT", "BTS"),
        official_source_options=("FAA", "DOT", "BTS"),
        paid_deferred_sources=("paid/proprietary flight feeds deferred",),
    ),
    "general": SourceFamily(
        free_source_options=("official pages/RSS after category split",),
        official_source_options=("category-specific official source pages",),
        paid_deferred_sources=("no paid source selected",),
    ),
    "cross_category": SourceFamily(
        free_source_options=("exact component official sources required",),
        official_source_options=("component-specific official source pages",),
        paid_deferred_sources=("component-specific paid sources deferred",),
    ),
    "crypto": SourceFamily(
        free_source_options=("existing crypto public source pipeline",),
        official_source_options=("Kalshi catalog plus public crypto market feeds",),
        paid_deferred_sources=("none selected",),
    ),
}


@dataclass(frozen=True)
class Phase3BBR3FreeSourceInventoryArtifacts:
    output_dir: Path
    inventory_path: Path
    scorecard_csv_path: Path
    backlog_path: Path
    next_category_path: Path
    manifest_path: Path


def write_phase3bb_r3_free_source_inventory_report(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3bb_r3"),
    reports_dir: Path = Path("reports"),
    settings: Settings | None = None,
    command_args: list[str] | None = None,
) -> Phase3BBR3FreeSourceInventoryArtifacts:
    payload = build_phase3bb_r3_free_source_inventory(
        session,
        output_dir=output_dir,
        reports_dir=reports_dir,
        settings=settings,
        command_args=command_args,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    inventory_path = output_dir / "free_source_inventory.md"
    scorecard_csv_path = output_dir / "category_scorecard.csv"
    backlog_path = output_dir / "free_source_backlog.md"
    next_category_path = output_dir / "NEXT_CATEGORY.md"
    manifest_path = output_dir / "MANIFEST.sha256"

    inventory_path.write_text(_render_inventory(payload), encoding="utf-8")
    _write_scorecard_csv(scorecard_csv_path, payload["category_rows"])
    backlog_path.write_text(_render_backlog(payload), encoding="utf-8")
    next_category_path.write_text(_render_next_category(payload), encoding="utf-8")
    _write_manifest(
        manifest_path,
        [inventory_path, scorecard_csv_path, backlog_path, next_category_path],
    )
    return Phase3BBR3FreeSourceInventoryArtifacts(
        output_dir=output_dir,
        inventory_path=inventory_path,
        scorecard_csv_path=scorecard_csv_path,
        backlog_path=backlog_path,
        next_category_path=next_category_path,
        manifest_path=manifest_path,
    )


def build_phase3bb_r3_free_source_inventory(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3bb_r3"),
    reports_dir: Path = Path("reports"),
    settings: Settings | None = None,
    command_args: list[str] | None = None,
) -> dict[str, Any]:
    resolved = settings or get_settings()
    metadata = _metadata(
        session,
        settings=resolved,
        generated_at=utc_now().isoformat(),
        command_args=command_args or [],
        output_dir=output_dir,
    )
    metadata["command_arguments"] = {
        "command": "kalshi-bot phase3bb-r3-free-source-inventory",
        "argv": command_args or [],
    }
    reports = _load_reports(reports_dir)
    coverage = reports.get("coverage") or {}
    coverage_by_category = {
        str(row.get("category")): row for row in coverage.get("category_rows", [])
    }
    rows = [
        _category_inventory_row(
            session,
            category,
            coverage_row=coverage_by_category.get(_coverage_category(category), {}),
            reports=reports,
        )
        for category in INVENTORY_CATEGORIES
    ]
    selected = select_best_noncrypto_category(rows)
    payload = {
        **metadata,
        "phase": "3BB-R3",
        "phase_version": PHASE3BB_R3_VERSION,
        "mode": "PAPER_READ_ONLY_FREE_SOURCE_INVENTORY",
        "reports_dir": str(reports_dir),
        "coverage_generated_at": coverage.get("generated_at"),
        "category_rows": rows,
        "selected_next_category": selected,
        "summary": _summary(rows, selected),
        "acceptance": _acceptance(rows, selected),
        "safety_flags": {
            **_safety_flags(),
            "paper_only": True,
            "diagnostic_only": True,
            "creates_paper_trades": False,
            "places_exchange_orders": False,
            "submits_cancels_replaces_orders": False,
            "fabricates_source_values": False,
            "tradingeconomics_deferred": True,
        },
        "operator_guardrails": [
            "PAPER / READ-ONLY inventory only.",
            "No paper trades.",
            "No live/demo exchange orders.",
            "No fabricated source values.",
            "TradingEconomics remains deferred.",
            "Crypto is background watch only for this selector.",
        ],
        "input_reports": {
            name: str(path)
            for name, path in {
                "paper_ready_truth": reports_dir / "phase3ba_r5" / "paper_ready_truth.json",
                "weather_fast_lane": reports_dir / "phase3bb_r2" / "weather_funnel.json",
                "noncrypto_backlog": reports_dir / "phase3ba_r6" / "noncrypto_engine_backlog.csv",
                "market_coverage": reports_dir
                / "market_coverage"
                / "market_coverage_doctor.json",
            }.items()
        },
    }
    return payload


def source_family_for_category(category: str) -> dict[str, str]:
    family = SOURCE_FAMILIES[category]
    return {
        "free_source_options": "; ".join(family.free_source_options),
        "official_source_options": "; ".join(family.official_source_options),
        "paid_deferred_sources": "; ".join(family.paid_deferred_sources),
    }


def select_best_noncrypto_category(rows: list[dict[str, Any]]) -> dict[str, Any]:
    noncrypto = [row for row in rows if row["category"] != "crypto"]
    viable = [row for row in noncrypto if int(row.get("score") or 0) > 0]
    if viable:
        selected = max(viable, key=lambda row: (int(row["score"]), row["category"]))
        reason = (
            f"{selected['category']} has the best non-crypto score "
            f"({selected['score']}) with blocker {selected['top_blocker']}."
        )
    elif noncrypto:
        selected = max(noncrypto, key=lambda row: (int(row["score"]), row["category"]))
        reason = (
            f"No non-crypto lane scored above zero; selecting the least blocked "
            f"non-crypto lane {selected['category']}."
        )
    else:
        selected = next((row for row in rows if row["category"] == "crypto"), {})
        reason = "Crypto selected only because no non-crypto categories were available."
    return {
        "category": selected.get("category", "none"),
        "score": int(selected.get("score") or 0),
        "top_blocker": selected.get("top_blocker", "UNKNOWN"),
        "next_implementation_step": selected.get("next_implementation_step", ""),
        "reason": reason,
        "crypto_selected": selected.get("category") == "crypto",
    }


def _category_inventory_row(
    session: Session,
    category: str,
    *,
    coverage_row: dict[str, Any],
    reports: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    source_family = source_family_for_category(category)
    active_markets = _active_market_count(session, category)
    parsed_markets = _parsed_market_count(session, category, coverage_row)
    linked_markets = _linked_market_count(session, category, coverage_row)
    source_ready_rows = _source_ready_rows(session, category)
    forecast_ready_rows = _model_count(session, category, Forecast, Forecast.model_name)
    ranking_ready_rows = _model_count(
        session,
        category,
        MarketRanking,
        MarketRanking.forecast_model,
    )
    paper_ready_rows = _paper_gate_ready_rows(category, reports)
    top_blocker = _top_blocker(
        category,
        active_markets=active_markets,
        parsed_markets=parsed_markets,
        linked_markets=linked_markets,
        source_ready_rows=source_ready_rows,
        forecast_ready_rows=forecast_ready_rows,
        ranking_ready_rows=ranking_ready_rows,
        paper_ready_rows=paper_ready_rows,
        coverage_row=coverage_row,
        reports=reports,
    )
    row = {
        "category": category,
        "active_markets": active_markets,
        "parsed_markets": parsed_markets,
        "linked_markets": linked_markets,
        "source_ready_rows": source_ready_rows,
        "forecast_ready_rows": forecast_ready_rows,
        "ranking_ready_rows": ranking_ready_rows,
        "paper_gate_ready_rows": paper_ready_rows,
        **source_family,
        "top_blocker": top_blocker,
        "coverage_status": coverage_row.get("status") or "NOT_IN_COVERAGE_REPORT",
        "coverage_percent": coverage_row.get("coverage_percent") or "n/a",
        "next_implementation_step": _next_implementation_step(category, top_blocker),
        "background_watch_only": category == "crypto",
    }
    row["score"] = category_score(row)
    return row


def category_score(row: dict[str, Any]) -> int:
    category = str(row["category"])
    score = 0
    score += min(int(row["active_markets"]), 500) // 20
    score += min(int(row["parsed_markets"]), 1000) // 25
    score += min(int(row["linked_markets"]), 500) // 10
    if int(row["source_ready_rows"]) > 0:
        score += 30
    if int(row["forecast_ready_rows"]) > 0:
        score += 20
    if int(row["ranking_ready_rows"]) > 0:
        score += 20
    if int(row["paper_gate_ready_rows"]) > 0:
        score += 100
    if category == "weather":
        score += 45
    if category == "crypto":
        score -= 80
    blocker = str(row["top_blocker"])
    if "NO_PARSED" in blocker:
        score -= 35
    if "LINKER_NOT_IMPLEMENTED" in blocker:
        score -= 30
    if "COMPOSITE" in blocker or "PARKED" in blocker:
        score -= 75
    if "PROVENANCE" in blocker:
        score -= 35
    return score


def _load_reports(reports_dir: Path) -> dict[str, dict[str, Any]]:
    return {
        "paper_ready_truth": _read_json(reports_dir / "phase3ba_r5" / "paper_ready_truth.json"),
        "weather_fast_lane": _read_json(reports_dir / "phase3bb_r2" / "weather_funnel.json"),
        "noncrypto_backlog": _read_backlog_csv(
            reports_dir / "phase3ba_r6" / "noncrypto_engine_backlog.csv"
        ),
        "coverage": _read_best_coverage_report(reports_dir),
    }


def _read_backlog_csv(path: Path) -> dict[str, Any]:
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
    except OSError:
        rows = []
    return {"rows": rows, "by_category": {row.get("category", ""): row for row in rows}}


def _read_best_coverage_report(reports_dir: Path) -> dict[str, Any]:
    for path in (
        reports_dir / "market_coverage" / "link_coverage.json",
        reports_dir / "market_coverage" / "market_coverage_doctor.json",
        reports_dir / "market_coverage" / "coverage_rows.json",
    ):
        coverage = _read_coverage_report(path)
        if coverage.get("category_rows"):
            return coverage
    return {"generated_at": None, "category_rows": []}


def _read_coverage_report(path: Path) -> dict[str, Any]:
    loaded_payload = _read_json(path)
    if isinstance(loaded_payload, list):
        payload = {"coverage_rows": loaded_payload}
    elif isinstance(loaded_payload, dict):
        payload = loaded_payload
    else:
        payload = {}
    raw_rows = payload.get("category_rows") or payload.get("coverage_rows") or []
    rows = []
    iterable_rows = raw_rows if isinstance(raw_rows, list) else []
    for raw in iterable_rows:
        category = raw.get("category") or raw.get("scope_key")
        if not category:
            continue
        coverage = raw.get("coverage_percent")
        if coverage is None and raw.get("coverage") is not None:
            coverage = f"{float(raw['coverage']) * 100:.1f}%"
        rows.append(
            {
                "category": str(category),
                "parsed_markets": raw.get("parsed_markets", 0),
                "linked_markets": (
                    raw.get("linked_markets")
                    or raw.get("external_linked_markets")
                    or raw.get("usable_markets")
                    or 0
                ),
                "unsupported_multileg_markets": (
                    raw.get("unsupported_multileg_markets")
                    or raw.get("unsupported_composites")
                    or 0
                ),
                "verified_schedule_markets": raw.get("verified_schedule_markets") or 0,
                "verified_schedule_link_rows": raw.get("verified_schedule_link_rows") or 0,
                "partial_link_rows": raw.get("partial_link_rows") or 0,
                "raw_unlinked_markets": raw.get("raw_unlinked_markets") or 0,
                "status": raw.get("status") or raw.get("health") or "UNKNOWN",
                "coverage_percent": coverage or "n/a",
            }
        )
    return {
        "generated_at": payload.get("generated_at")
        or payload.get("data_cutoff")
        or payload.get("created_at"),
        "category_rows": rows,
    }


def _coverage_category(category: str) -> str:
    return category


def _active_market_count(session: Session, category: str) -> int:
    return _distinct_market_leg_count(session, category, active_only=True)


def _parsed_market_count(
    session: Session,
    category: str,
    coverage_row: dict[str, Any],
) -> int:
    if category in {"weather", "sports", "economic", "news", "general", "cross_category", "crypto"}:
        return _to_int(coverage_row.get("parsed_markets"))
    return _distinct_market_leg_count(session, category, active_only=False)


def _linked_market_count(
    session: Session,
    category: str,
    coverage_row: dict[str, Any],
) -> int:
    direct = {
        "weather": (WeatherMarketLink, WeatherMarketLink.ticker),
        "sports": (SportsMarketLink, SportsMarketLink.ticker),
        "economic": (EconomicMarketLink, EconomicMarketLink.ticker),
        "news": (NewsMarketLink, NewsMarketLink.ticker),
        "crypto": (CryptoMarketLink, CryptoMarketLink.ticker),
    }
    if category in direct:
        table, column = direct[category]
        return _distinct_count(session, table, column)
    return _to_int(coverage_row.get("linked_markets")) if category == "cross_category" else 0


def _source_ready_rows(session: Session, category: str) -> int:
    try:
        if category == "weather":
            return int(
                session.scalar(
                    select(func.count(func.distinct(WeatherMarketLink.ticker)))
                    .select_from(WeatherMarketLink)
                    .join(
                        WeatherForecast,
                        WeatherForecast.location_key == WeatherMarketLink.location_key,
                    )
                )
                or 0
            )
        if category == "sports":
            return int(
                session.scalar(
                    select(func.count(func.distinct(SportsMarketLink.ticker)))
                    .select_from(SportsMarketLink)
                    .join(SportsGame, SportsGame.game_key == SportsMarketLink.game_key)
                )
                or 0
            )
        if category == "economic":
            return int(
                session.scalar(
                    select(func.count(func.distinct(EconomicMarketLink.ticker)))
                    .select_from(EconomicMarketLink)
                    .join(EconomicEvent, EconomicEvent.event_key == EconomicMarketLink.event_key)
                )
                or 0
            )
        if category == "news":
            return int(
                session.scalar(
                    select(func.count(func.distinct(NewsMarketLink.ticker)))
                    .select_from(NewsMarketLink)
                    .join(NewsItem, NewsItem.id == NewsMarketLink.news_item_id)
                )
                or 0
            )
        if category == "crypto":
            return int(
                session.scalar(
                    select(func.count(func.distinct(CryptoMarketLink.ticker)))
                    .select_from(CryptoMarketLink)
                    .join(CryptoFeature, CryptoFeature.symbol == CryptoMarketLink.symbol)
                )
                or 0
            )
    except Exception:
        return 0
    return 0


def _model_count(
    session: Session,
    category: str,
    table: Any,
    model_column: Any,
) -> int:
    selector = MODEL_SELECTORS.get(category)
    if selector is None:
        return 0
    mode, value = selector
    condition = model_column == value if mode == "exact" else model_column.like(f"{value}%")
    try:
        return int(
            session.scalar(
                select(func.count(func.distinct(table.ticker))).select_from(table).where(condition)
            )
            or 0
        )
    except Exception:
        return 0


def _paper_gate_ready_rows(category: str, reports: dict[str, dict[str, Any]]) -> int:
    truth = reports.get("paper_ready_truth") or {}
    summaries = truth.get("category_summaries") or {}
    summary = summaries.get(category) or {}
    ready = _to_int(summary.get("paper_ready_rows"))
    if ready:
        return ready
    rows = truth.get("paper_ready_rows") or []
    if isinstance(rows, list):
        return sum(1 for row in rows if row.get("category") == category)
    return 0


def _top_blocker(
    category: str,
    *,
    active_markets: int,
    parsed_markets: int,
    linked_markets: int,
    source_ready_rows: int,
    forecast_ready_rows: int,
    ranking_ready_rows: int,
    paper_ready_rows: int,
    coverage_row: dict[str, Any],
    reports: dict[str, dict[str, Any]],
) -> str:
    report_blocker = _report_blocker(category, reports)
    if paper_ready_rows > 0:
        return "PAPER_GATE_READY"
    if category == "crypto":
        return report_blocker or "BACKGROUND_WAIT_FOR_EXECUTABLE_BOOK"
    if category == "cross_category":
        return "PARKED_COMPOSITES_REQUIRE_COMPONENT_SUPPORT"
    if category == "general":
        return "NO_SPECIALIZED_SOURCE_OR_LINKER"
    if category in {"agriculture_usda", "transportation"}:
        if parsed_markets <= 0:
            return "NO_PARSED_CATEGORY_INVENTORY"
        return "LINKER_NOT_IMPLEMENTED"
    unsupported = _to_int(coverage_row.get("unsupported_multileg_markets"))
    if category == "sports" and unsupported > 0:
        return "UNSUPPORTED_KXMVE_COMPOSITES_PARKED"
    if active_markets <= 0:
        return "NO_CURRENT_ACTIVE_MARKETS"
    if parsed_markets <= 0:
        return "NO_PARSED_MARKET_INVENTORY"
    if linked_markets <= 0:
        return "SPECIALIZED_LINKER_NOT_POPULATED"
    if source_ready_rows <= 0:
        return "SOURCE_EVIDENCE_MISSING"
    if forecast_ready_rows <= 0:
        return "FORECAST_ENGINE_NOT_ACTIVE"
    if ranking_ready_rows <= 0:
        return "RANKING_ENGINE_NOT_ACTIVE"
    return report_blocker or "PAPER_GATE_NOT_OPEN"


def _report_blocker(category: str, reports: dict[str, dict[str, Any]]) -> str | None:
    truth = reports.get("paper_ready_truth") or {}
    summaries = truth.get("category_summaries") or {}
    summary = summaries.get(category) or {}
    blocker = summary.get("first_blocker") or summary.get("first_hard_blocker")
    if blocker:
        return str(blocker)
    if category == "weather":
        weather = reports.get("weather_fast_lane") or {}
        weather_summary = weather.get("summary") or {}
        blocker = weather_summary.get("first_hard_blocker")
        if blocker:
            return str(blocker)
    backlog = reports.get("noncrypto_backlog", {}).get("by_category", {})
    row = backlog.get(category) or {}
    blocker = row.get("primary_blocker")
    return str(blocker) if blocker else None


def _next_implementation_step(category: str, blocker: str) -> str:
    if category == "weather":
        if blocker in {"NO_CURRENT_ACTIVE_MARKETS", "NO_CURRENT_WEATHER_ROWS"}:
            return (
                "kalshi-bot db-writer-monitor --json\n"
                "kalshi-bot sync-markets --status open --limit 100 --max-pages 3 "
                "--series-ticker KXTEMPNYCH\n"
                "kalshi-bot market-legs-parse --refresh --limit 1500\n"
                "kalshi-bot phase3az-r12-weather-activation-preview --output-dir "
                "reports/phase3az_r12_weather --limit 2000 --fresh-window-hours 24 "
                "--match-tolerance-hours 3"
            )
        return (
            "kalshi-bot phase3bb-r2-weather-fast-lane --output-dir reports/phase3bb_r2 "
            "--reports-dir reports --limit 100"
        )
    if category == "sports":
        return (
            "kalshi-bot phase3bb-r6-sports-provenance-repair --output-dir "
            "reports/phase3bb_r6 --reports-dir reports"
        )
    if category == "economic":
        return (
            "kalshi-bot phase3bb-r4-economic-parser-backfill --output-dir "
            "reports/phase3bb_r4 --reports-dir reports"
        )
    if category == "news":
        return (
            "kalshi-bot phase3bb-r7-news-event-discovery --output-dir "
            "reports/phase3bb_r7 --reports-dir reports"
        )
    if category == "agriculture_usda":
        return "Build USDA AMS/NASS parser/linker preview for exact commodity/date rows."
    if category == "transportation":
        return "Build FAA/DOT/BTS parser/linker preview for exact airport/date rows."
    if category == "cross_category":
        return "Keep composites parked until exact component evidence support exists."
    if category == "general":
        return "Split general rows into specialized source families before linking."
    return "Keep crypto in background watcher; do not select as the next category sprint."


def _distinct_market_leg_count(session: Session, category: str, *, active_only: bool) -> int:
    filters = _category_conditions(category)
    if not filters:
        return 0
    stmt = (
        select(func.count(func.distinct(MarketLeg.ticker)))
        .select_from(MarketLeg)
        .join(Market, Market.ticker == MarketLeg.ticker)
        .where(or_(*filters))
    )
    if active_only:
        stmt = stmt.where(func.lower(func.coalesce(Market.status, "")).in_(ACTIVE_MARKET_STATUSES))
    try:
        return int(session.scalar(stmt) or 0)
    except Exception:
        return 0


def _category_conditions(category: str) -> list[Any]:
    conditions: list[Any] = []
    aliases = CATEGORY_ALIASES.get(category) or ()
    if aliases:
        conditions.append(func.lower(MarketLeg.category).in_(aliases))
    return conditions


def _distinct_count(session: Session, table: Any, column: Any) -> int:
    try:
        return int(
            session.scalar(select(func.count(func.distinct(column))).select_from(table)) or 0
        )
    except Exception:
        return 0


def _summary(rows: list[dict[str, Any]], selected: dict[str, Any]) -> dict[str, Any]:
    return {
        "categories_reviewed": len(rows),
        "selected_next_category": selected.get("category"),
        "selected_score": selected.get("score"),
        "crypto_background_watch_only": True,
        "tradingeconomics_deferred": True,
        "paper_ready_categories": [
            row["category"] for row in rows if int(row["paper_gate_ready_rows"]) > 0
        ],
        "top_noncrypto_scores": [
            {"category": row["category"], "score": row["score"]}
            for row in sorted(
                [item for item in rows if item["category"] != "crypto"],
                key=lambda item: (-int(item["score"]), item["category"]),
            )[:3]
        ],
    }


def _acceptance(rows: list[dict[str, Any]], selected: dict[str, Any]) -> dict[str, Any]:
    noncrypto_scores = [int(row["score"]) for row in rows if row["category"] != "crypto"]
    crypto_selected_allowed = bool(selected.get("category") != "crypto") or not any(
        score > 0 for score in noncrypto_scores
    )
    return {
        "one_best_noncrypto_category_selected": bool(selected.get("category"))
        and selected.get("category") != "crypto",
        "crypto_not_selected_unless_every_noncrypto_path_worse": crypto_selected_allowed,
        "tradingeconomics_deferred": True,
        "no_live_demo_or_paper_orders": True,
        "no_paper_trades_created": True,
        "no_fabricated_source_values": True,
    }


def _render_inventory(payload: dict[str, Any]) -> str:
    selected = payload["selected_next_category"]
    lines = _metadata_lines(payload, "# Phase 3BB-R3 Free Source Inventory")
    lines.extend(
        [
            "",
            "## Result",
            "",
            f"- Selected next non-crypto category: `{selected['category']}`",
            f"- Selected blocker: `{selected['top_blocker']}`",
            f"- Crypto selected: `{selected['crypto_selected']}`",
            "- TradingEconomics: `DEFERRED`",
            "",
            "## Category Inventory",
            "",
            "| Category | Active | Parsed | Linked | Source-ready | Forecast-ready | "
            "Ranking-ready | Paper-ready | Top blocker | Free sources | Official sources | "
            "Paid/deferred |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- | --- | --- |",
        ]
    )
    for row in payload["category_rows"]:
        lines.append(
            "| {category} | {active_markets} | {parsed_markets} | {linked_markets} | "
            "{source_ready_rows} | {forecast_ready_rows} | {ranking_ready_rows} | "
            "{paper_gate_ready_rows} | {top_blocker} | {free_source_options} | "
            "{official_source_options} | {paid_deferred_sources} |".format(
                **_markdown_safe_row(row)
            )
        )
    lines.extend(["", "## Acceptance", ""])
    for key, value in payload["acceptance"].items():
        lines.append(f"- {key}: `{value}`")
    lines.extend(["", "## Guardrails", ""])
    for guardrail in payload["operator_guardrails"]:
        lines.append(f"- {guardrail}")
    return "\n".join(lines).rstrip() + "\n"


def _render_backlog(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# Phase 3BB-R3 Free Source Backlog")
    lines.extend(["", "## Backlog", ""])
    sorted_rows = sorted(
        payload["category_rows"],
        key=lambda item: (-int(item["score"]), item["category"]),
    )
    for row in sorted_rows:
        lines.extend(
            [
                f"### {row['category']}",
                f"- Score: `{row['score']}`",
                f"- Top blocker: `{row['top_blocker']}`",
                f"- Free source options: `{row['free_source_options']}`",
                f"- Official source options: `{row['official_source_options']}`",
                f"- Paid/deferred sources: `{row['paid_deferred_sources']}`",
                f"- Next implementation step: `{row['next_implementation_step']}`",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def _render_next_category(payload: dict[str, Any]) -> str:
    selected = payload["selected_next_category"]
    lines = _metadata_lines(payload, "# Phase 3BB-R3 Next Category")
    lines.extend(
        [
            "",
            "## Selection",
            "",
            f"- Category: `{selected['category']}`",
            f"- Score: `{selected['score']}`",
            f"- Top blocker: `{selected['top_blocker']}`",
            f"- Reason: {selected['reason']}",
            "- Crypto is background-only and was not eligible for selection unless every "
            "non-crypto path scored worse.",
            "- TradingEconomics remains `DEFERRED`.",
            "",
            "## Next Codex Step",
            "",
            "```bash",
            selected.get("next_implementation_step")
            or (
                "kalshi-bot phase3bb-r3-free-source-inventory "
                "--output-dir reports/phase3bb_r3 --reports-dir reports"
            ),
            "```",
            "",
            "## Do Not Run",
            "",
            "- Do not create paper trades.",
            "- Do not submit, cancel, replace, or amend live/demo exchange orders.",
            "- Do not use TradingEconomics in this phase.",
            "- Do not fabricate source values.",
        ]
    )
    return "\n".join(lines).rstrip() + "\n"


def _write_scorecard_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fieldnames = [
        "category",
        "score",
        "active_markets",
        "parsed_markets",
        "linked_markets",
        "source_ready_rows",
        "forecast_ready_rows",
        "ranking_ready_rows",
        "paper_gate_ready_rows",
        "free_source_options",
        "official_source_options",
        "paid_deferred_sources",
        "top_blocker",
        "next_implementation_step",
        "coverage_status",
        "coverage_percent",
        "background_watch_only",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fieldnames})


def _markdown_safe_row(row: dict[str, Any]) -> dict[str, Any]:
    safe = dict(row)
    for key, value in safe.items():
        if isinstance(value, str):
            safe[key] = value.replace("|", "/").replace("\n", "<br>")
    return safe


def _to_int(value: Any) -> int:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return 0
