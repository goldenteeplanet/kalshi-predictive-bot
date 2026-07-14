from __future__ import annotations

import csv
import hashlib
import json
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.data.backend import (
    database_url_from_settings,
    redact_database_url,
    sqlite_path_from_url,
)
from kalshi_predictor.data.db import describe_db_location
from kalshi_predictor.data.schema import (
    EconomicEvent,
    EconomicFeature,
    EconomicMarketLink,
    Forecast,
    Market,
    MarketLeg,
    MarketRanking,
    NewsFeature,
    NewsItem,
    NewsMarketLink,
    SportsFeature,
    SportsGame,
    SportsMarketLink,
    WeatherFeature,
    WeatherForecast,
    WeatherMarketLink,
    WeatherObservation,
)
from kalshi_predictor.market_legs import link_coverage_dashboard
from kalshi_predictor.paper.settlement_reconciliation import PAPER_ONLY_SAFETY
from kalshi_predictor.utils.time import parse_datetime, utc_now

PHASE3BA_R6_VERSION = "phase3ba_r6_noncrypto_engine_backlog_v1"
R5_TRUTH_PATH = Path("phase3ba_r5/paper_ready_truth.json")
R2_WEATHER_PATH = Path("phase3ba_r2/weather_ranking_activation.json")
R13_WEATHER_PATH = Path("phase3az_r13_weather/weather_handoff_status.json")

BASE_CATEGORIES = (
    "weather",
    "sports",
    "economic",
    "news",
    "general",
    "cross_category",
)
OPTIONAL_CATEGORIES = ("transportation", "agriculture")
ACTIVE_MARKET_STATUSES = ("open", "active")


@dataclass(frozen=True)
class Phase3BAR6ArtifactSet:
    output_dir: Path
    executive_summary_path: Path
    markdown_path: Path
    csv_path: Path
    next_category_build_path: Path
    manifest_path: Path


def write_phase3ba_r6_noncrypto_engine_backlog_report(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3ba_r6"),
    reports_dir: Path = Path("reports"),
    settings: Settings | None = None,
    command_args: list[str] | None = None,
) -> Phase3BAR6ArtifactSet:
    payload = build_phase3ba_r6_noncrypto_engine_backlog(
        session,
        output_dir=output_dir,
        reports_dir=reports_dir,
        settings=settings,
        command_args=command_args,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    executive_summary_path = output_dir / "EXECUTIVE_SUMMARY.md"
    markdown_path = output_dir / "noncrypto_engine_backlog.md"
    csv_path = output_dir / "noncrypto_engine_backlog.csv"
    next_category_build_path = output_dir / "NEXT_CATEGORY_BUILD.md"
    manifest_path = output_dir / "MANIFEST.sha256"

    executive_summary_path.write_text(_render_executive_summary(payload), encoding="utf-8")
    markdown_path.write_text(_render_backlog_markdown(payload), encoding="utf-8")
    _write_backlog_csv(csv_path, payload["backlog_rows"])
    next_category_build_path.write_text(_render_next_category_build(payload), encoding="utf-8")
    _write_manifest(
        manifest_path,
        [executive_summary_path, markdown_path, csv_path, next_category_build_path],
    )
    return Phase3BAR6ArtifactSet(
        output_dir=output_dir,
        executive_summary_path=executive_summary_path,
        markdown_path=markdown_path,
        csv_path=csv_path,
        next_category_build_path=next_category_build_path,
        manifest_path=manifest_path,
    )


def build_phase3ba_r6_noncrypto_engine_backlog(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3ba_r6"),
    reports_dir: Path = Path("reports"),
    settings: Settings | None = None,
    command_args: list[str] | None = None,
) -> dict[str, Any]:
    generated_at = utc_now()
    resolved = settings or get_settings()
    coverage = link_coverage_dashboard(session)
    coverage_by_category = {row["category"]: row for row in coverage["category_rows"]}
    r5_truth = _read_json_if_exists(reports_dir / R5_TRUTH_PATH)
    r2_weather = _read_json_if_exists(reports_dir / R2_WEATHER_PATH)
    r13_weather = _read_json_if_exists(reports_dir / R13_WEATHER_PATH)
    source_counts = _source_counts(session)
    forecast_counts = _forecast_counts(session)
    active_counts = _active_market_counts(session)
    categories = _category_order(session)
    rows = [
        _backlog_row(
            category,
            coverage_row=coverage_by_category.get(category, {}),
            active_market_count=active_counts.get(category, 0),
            source_counts=source_counts.get(category, {}),
            forecast_counts=forecast_counts.get(category, {}),
            r5_truth=r5_truth,
            r2_weather=r2_weather,
            r13_weather=r13_weather,
        )
        for category in categories
    ]
    selected_after_weather = _select_next_category_after_weather(rows)
    immediate = _immediate_next_step(rows)
    payload = {
        **_metadata(
            session,
            settings=resolved,
            generated_at=generated_at.isoformat(),
            command_args=command_args or [],
        ),
        "phase": "3BA-R6",
        "phase_version": PHASE3BA_R6_VERSION,
        "mode": "PAPER_READ_ONLY_NONCRYPTO_ENGINE_BACKLOG",
        "output_dir": str(output_dir),
        "reports_dir": str(reports_dir),
        "summary": _summary(rows, selected_after_weather, immediate),
        "selected_next_noncrypto_build_after_weather": selected_after_weather,
        "immediate_next_step": immediate,
        "backlog_rows": rows,
        "evidence": {
            "coverage_generated_at": coverage.get("generated_at"),
            "r5_truth_generated_at": r5_truth.get("generated_at"),
            "r2_weather_generated_at": r2_weather.get("generated_at"),
            "r13_weather_generated_at": r13_weather.get("generated_at"),
            "coverage_bottleneck": coverage.get("bottleneck"),
            "source_counts": source_counts,
            "forecast_counts": forecast_counts,
        },
        "acceptance": _acceptance(rows, selected_after_weather),
        "operator_guardrails": _operator_guardrails(),
    }
    return payload


def _category_order(session: Session) -> list[str]:
    categories = list(BASE_CATEGORIES)
    parsed_categories = {
        str(value)
        for value in session.scalars(select(MarketLeg.category).distinct())
        if value is not None
    }
    for category in OPTIONAL_CATEGORIES:
        if category in parsed_categories:
            categories.append(category)
    return categories


def _backlog_row(
    category: str,
    *,
    coverage_row: dict[str, Any],
    active_market_count: int,
    source_counts: dict[str, int],
    forecast_counts: dict[str, int],
    r5_truth: dict[str, Any],
    r2_weather: dict[str, Any],
    r13_weather: dict[str, Any],
) -> dict[str, Any]:
    parsed_markets = int(coverage_row.get("parsed_markets") or 0)
    linked_count = int(coverage_row.get("linked_markets") or 0)
    linked_rows = _linked_row_count(category, source_counts)
    gaps = _gap_counts(category, coverage_row=coverage_row, source_counts=source_counts)
    row = {
        "category": category,
        "active_market_count": active_market_count,
        "parsed_market_count": parsed_markets,
        "linked_count": linked_count,
        "linked_row_count": linked_rows,
        "source_readiness": _source_readiness(category, source_counts),
        "parser_readiness": _parser_readiness(category, coverage_row),
        "forecast_readiness": _forecast_readiness(category, source_counts, forecast_counts),
        "paper_gate_readiness": _paper_gate_readiness(category, r5_truth),
        "primary_blocker": _primary_blocker(
            category,
            coverage_row=coverage_row,
            source_counts=source_counts,
            forecast_counts=forecast_counts,
            r5_truth=r5_truth,
            r2_weather=r2_weather,
            r13_weather=r13_weather,
        ),
        "next_implementation_step": _next_implementation_step(
            category,
            coverage_row=coverage_row,
            r5_truth=r5_truth,
        ),
        "coverage_status": coverage_row.get("status") or "NOT_IN_COVERAGE_REPORT",
        "coverage_percent": coverage_row.get("coverage_percent") or "n/a",
        "source_counts": source_counts,
        "forecast_counts": forecast_counts,
        "gap_counts": gaps,
        "score_after_weather": _category_build_score(
            category,
            parsed_markets=parsed_markets,
            linked_count=linked_count,
            source_counts=source_counts,
            forecast_counts=forecast_counts,
            coverage_row=coverage_row,
        ),
    }
    return row


def _linked_row_count(category: str, source_counts: dict[str, int]) -> int:
    linkable = {"weather", "sports", "economic", "news"}
    return int(source_counts.get("links", 0)) if category in linkable else 0


def _source_readiness(category: str, counts: dict[str, int]) -> str:
    if category == "weather":
        if counts.get("forecasts", 0) > 0 and counts.get("features", 0) > 0:
            return "SOURCE_AND_FEATURES_READY"
        if counts.get("forecasts", 0) > 0:
            return "SOURCE_READY_FEATURES_GAP"
        return "WEATHER_SOURCE_NOT_CURRENT"
    if category == "sports":
        if counts.get("games", 0) > 0 and counts.get("features", 0) > 0:
            return "SPORTS_SOURCE_PARTIAL_WITH_FEATURES"
        if counts.get("games", 0) > 0:
            return "SPORTS_SOURCE_PARTIAL"
        return "SPORTS_SOURCE_NOT_READY"
    if category == "economic":
        if counts.get("events", 0) > 0 and counts.get("features", 0) > 0:
            return "ECONOMIC_SOURCE_AND_FEATURES_READY"
        if counts.get("events", 0) > 0:
            return "ECONOMIC_SOURCE_READY_FEATURES_GAP"
        return "ECONOMIC_SOURCE_NOT_INGESTED"
    if category == "news":
        if counts.get("items", 0) > 0 and counts.get("features", 0) > 0:
            return "NEWS_SOURCE_AND_FEATURES_READY"
        if counts.get("items", 0) > 0:
            return "NEWS_SOURCE_READY_FEATURES_GAP"
        return "NEWS_SOURCE_NOT_INGESTED"
    if category == "cross_category":
        return "COMPOSITE_COMPONENT_SOURCE_NOT_IMPLEMENTED"
    if category == "general":
        return "NO_SPECIALIZED_SOURCE_ENGINE"
    return "SOURCE_ENGINE_NOT_IMPLEMENTED"


def _parser_readiness(category: str, coverage_row: dict[str, Any]) -> str:
    parsed_markets = int(coverage_row.get("parsed_markets") or 0)
    status = str(coverage_row.get("status") or "")
    if parsed_markets <= 0:
        return "NO_PARSED_MARKET_INVENTORY"
    if category == "cross_category":
        return "PARSED_PARKED_COMPOSITES"
    if category == "general":
        return "PARSED_GENERAL_CONTEXT_ONLY"
    if status in {"CONNECTED", "DERIVED_CONNECTED"}:
        return "PARSER_AND_LINKS_CONNECTED"
    if int(coverage_row.get("linked_markets") or 0) <= 0:
        return "PARSED_NEEDS_LINKER"
    return "PARSER_PARTIAL"


def _forecast_readiness(
    category: str,
    source_counts: dict[str, int],
    forecast_counts: dict[str, int],
) -> str:
    forecasts = int(forecast_counts.get("forecasts") or 0)
    rankings = int(forecast_counts.get("rankings") or 0)
    if forecasts > 0 and rankings > 0:
        return "FORECASTS_AND_RANKINGS_PRESENT"
    if forecasts > 0:
        return "FORECASTS_PRESENT_RANKING_GAP"
    if int(source_counts.get("features") or 0) > 0:
        return "FEATURES_READY_FORECAST_GAP"
    if category in {"general", "cross_category"}:
        return "NOT_ELIGIBLE_FOR_SINGLE_CATEGORY_FORECAST"
    return "FORECAST_ENGINE_NOT_ACTIVE"


def _paper_gate_readiness(category: str, r5_truth: dict[str, Any]) -> str:
    summaries = r5_truth.get("category_summaries", {}) if isinstance(r5_truth, dict) else {}
    summary = summaries.get(category, {}) if isinstance(summaries, dict) else {}
    if category in {"weather"} and summary:
        ready = int(summary.get("paper_ready_rows") or 0)
        if ready > 0:
            return "PAPER_GATE_READY"
        blocker = summary.get("first_blocker") or "UNKNOWN"
        return f"PAPER_GATE_BLOCKED:{blocker}"
    if category in {"general", "cross_category"}:
        return "NOT_ELIGIBLE_FOR_SINGLE_MARKET_PAPER_GATE"
    return "NOT_IN_CURRENT_PAPER_GATE"


def _primary_blocker(
    category: str,
    *,
    coverage_row: dict[str, Any],
    source_counts: dict[str, int],
    forecast_counts: dict[str, int],
    r5_truth: dict[str, Any],
    r2_weather: dict[str, Any],
    r13_weather: dict[str, Any],
) -> str:
    if category == "weather":
        blocker = _weather_blocker_from_reports(r5_truth, r2_weather, r13_weather)
        return blocker or "WEATHER_ACTIVATION_NEEDS_REFRESH"
    if category == "sports":
        unsupported = int(coverage_row.get("unsupported_multileg_markets") or 0)
        verified = int(coverage_row.get("verified_schedule_markets") or 0)
        if unsupported > 0:
            return "UNSUPPORTED_KXMVE_COMPOSITES_PARKED"
        if verified <= 0:
            return "SPORTS_PROVENANCE_NOT_VERIFIED"
        if int(forecast_counts.get("forecasts") or 0) <= 0:
            return "SPORTS_FORECAST_ENGINE_NOT_ACTIVE"
        return "SPORTS_PAPER_GATE_NOT_INTEGRATED"
    if category in {"economic", "news"}:
        if int(coverage_row.get("parsed_markets") or 0) <= 0:
            return "NO_PARSED_MARKET_INVENTORY"
        if int(source_counts.get("links") or 0) <= 0:
            return "SPECIALIZED_LINKER_NOT_POPULATED"
        if int(forecast_counts.get("forecasts") or 0) <= 0:
            return "FORECAST_ENGINE_NOT_ACTIVE"
        return "PAPER_GATE_NOT_INTEGRATED"
    if category == "general":
        return "NO_SPECIALIZED_SOURCE_OR_LINKER"
    if category == "cross_category":
        return "PARKED_COMPOSITES_REQUIRE_COMPONENT_SUPPORT"
    return "CATEGORY_ENGINE_NOT_IMPLEMENTED"


def _weather_blocker_from_reports(
    r5_truth: dict[str, Any],
    r2_weather: dict[str, Any],
    r13_weather: dict[str, Any],
) -> str | None:
    summaries = r5_truth.get("category_summaries", {}) if isinstance(r5_truth, dict) else {}
    weather_summary = summaries.get("weather", {}) if isinstance(summaries, dict) else {}
    blocker_counts = weather_summary.get("blocker_counts", {})
    if isinstance(blocker_counts, dict) and blocker_counts:
        return str(Counter(blocker_counts).most_common(1)[0][0])
    after_summary = r2_weather.get("after_summary", {}) if isinstance(r2_weather, dict) else {}
    first_counts = after_summary.get("first_hard_blocker_counts", {})
    if isinstance(first_counts, dict) and first_counts:
        return str(Counter(first_counts).most_common(1)[0][0])
    r13_summary = r13_weather.get("summary", {}) if isinstance(r13_weather, dict) else {}
    if int(r13_summary.get("ranking_gap_rows") or 0) > 0:
        return "RANKING_GAP"
    if int(r13_summary.get("snapshot_gap_rows") or 0) > 0:
        return "SNAPSHOT_MISSING"
    return None


def _next_implementation_step(
    category: str,
    *,
    coverage_row: dict[str, Any],
    r5_truth: dict[str, Any],
) -> str:
    if category == "weather":
        next_action = r5_truth.get("next_action", {}) if isinstance(r5_truth, dict) else {}
        command = str(next_action.get("command") or "").strip()
        if command:
            return command
        return (
            "Run db-writer-monitor, refresh targeted KXTEMPNYCH snapshots/orderbooks, "
            "rerun weather rankings, then rerun Phase 3BA-R5 truth."
        )
    if category == "sports":
        return (
            "Build a sports source/provenance sprint for verified schedule rows only; keep "
            "KXMVE composites parked until composite support exists."
        )
    if category == "economic":
        return (
            "Collect a broader active market snapshot, extend economic parser inventory, "
            "ingest economic calendar/source data, then build economic linker/features."
        )
    if category == "news":
        return (
            "Collect a broader active market snapshot, extend news parser inventory, ingest "
            "source-backed news items, then build news linker/features."
        )
    if category == "general":
        return "Keep general as context until a specialized source/linker family is defined."
    if category == "cross_category":
        return (
            "Keep composites parked; design component-evidence support before any "
            "single-market remediation."
        )
    return "Add parser, source adapter, linker, forecast model, and paper-gate diagnostics."


def _category_build_score(
    category: str,
    *,
    parsed_markets: int,
    linked_count: int,
    source_counts: dict[str, int],
    forecast_counts: dict[str, int],
    coverage_row: dict[str, Any],
) -> int:
    if category in {"weather", "general", "cross_category"}:
        return -1
    score = 0
    score += min(parsed_markets, 1000) // 10
    score += min(linked_count, 1000) // 10
    score += min(int(source_counts.get("features") or 0), 1000) // 20
    score += min(int(forecast_counts.get("forecasts") or 0), 1000) // 20
    if category == "sports" and int(coverage_row.get("derived_usable_markets") or 0) > 0:
        score += 100
    if category in {"economic", "news"} and parsed_markets <= 0:
        score -= 50
    return score


def _select_next_category_after_weather(rows: list[dict[str, Any]]) -> dict[str, Any]:
    candidates = [
        row
        for row in rows
        if row["category"] not in {"weather", "general", "cross_category"}
    ]
    if not candidates:
        return {
            "category": "none",
            "reason": "No non-weather category has enough parsed/link/source evidence yet.",
            "next_implementation_step": "Collect broader active market inventory first.",
        }
    selected = max(candidates, key=lambda row: row["score_after_weather"])
    return {
        "category": selected["category"],
        "reason": (
            f"Selected {selected['category']} with score {selected['score_after_weather']} "
            f"from parsed={selected['parsed_market_count']}, linked={selected['linked_count']}, "
            f"source={selected['source_readiness']}."
        ),
        "next_implementation_step": selected["next_implementation_step"],
        "primary_blocker": selected["primary_blocker"],
    }


def _immediate_next_step(rows: list[dict[str, Any]]) -> dict[str, Any]:
    weather = next((row for row in rows if row["category"] == "weather"), None)
    if weather and weather["paper_gate_readiness"] != "PAPER_GATE_READY":
        return {
            "category": "weather",
            "stage": "FINISH_WEATHER_ACTIVATION",
            "reason": weather["primary_blocker"],
            "command": weather["next_implementation_step"],
        }
    return {
        "category": "selected_after_weather",
        "stage": "START_NEXT_CATEGORY_ENGINE",
        "reason": "Weather does not require immediate activation work from this backlog.",
        "command": "See selected_next_noncrypto_build_after_weather.",
    }


def _gap_counts(
    category: str,
    *,
    coverage_row: dict[str, Any],
    source_counts: dict[str, int],
) -> dict[str, int]:
    parsed_markets = int(coverage_row.get("parsed_markets") or 0)
    linked_markets = int(coverage_row.get("linked_markets") or 0)
    linkable_markets = int(coverage_row.get("linkable_markets") or 0)
    parser_gap = 1 if parsed_markets <= 0 else 0
    if category in {"general", "cross_category"}:
        parser_gap = 0
    source_gap = _source_gap_count(category, source_counts)
    return {
        "parser_gap": parser_gap,
        "link_gap_markets": max(linkable_markets - linked_markets, 0),
        "source_gap": source_gap,
        "general_source_evidence_gap": parsed_markets if category == "general" else 0,
        "sports_partial_link_rows": (
            int(coverage_row.get("partial_link_rows") or 0) if category == "sports" else 0
        ),
        "sports_unsupported_composites": (
            int(coverage_row.get("unsupported_multileg_markets") or 0)
            if category == "sports"
            else 0
        ),
        "unsupported_composites": int(coverage_row.get("unsupported_multileg_markets") or 0),
    }


def _source_gap_count(category: str, counts: dict[str, int]) -> int:
    if category == "weather":
        return 0 if counts.get("forecasts", 0) > 0 and counts.get("features", 0) > 0 else 1
    if category == "sports":
        return 0 if counts.get("games", 0) > 0 and counts.get("features", 0) > 0 else 1
    if category == "economic":
        return 0 if counts.get("events", 0) > 0 and counts.get("features", 0) > 0 else 1
    if category == "news":
        return 0 if counts.get("items", 0) > 0 and counts.get("features", 0) > 0 else 1
    return 1 if category in {"general", "cross_category"} else 1


def _source_counts(session: Session) -> dict[str, dict[str, int]]:
    return {
        "weather": {
            "observations": _table_count(session, WeatherObservation),
            "forecasts": _table_count(session, WeatherForecast),
            "features": _table_count(session, WeatherFeature),
            "links": _table_count(session, WeatherMarketLink),
        },
        "sports": {
            "games": _table_count(session, SportsGame),
            "features": _table_count(session, SportsFeature),
            "links": _table_count(session, SportsMarketLink),
        },
        "economic": {
            "events": _table_count(session, EconomicEvent),
            "features": _table_count(session, EconomicFeature),
            "links": _table_count(session, EconomicMarketLink),
        },
        "news": {
            "items": _table_count(session, NewsItem),
            "features": _table_count(session, NewsFeature),
            "links": _table_count(session, NewsMarketLink),
        },
        "general": {},
        "cross_category": {},
    }


def _forecast_counts(session: Session) -> dict[str, dict[str, int]]:
    return {
        "weather": _model_counts(session, exact_model="weather_v2"),
        "sports": _model_counts(session, model_prefix="sports"),
        "economic": _model_counts(session, model_prefix="economic"),
        "news": _model_counts(session, model_prefix="news"),
        "general": _model_counts(session, model_prefix="general"),
        "cross_category": _model_counts(session, model_prefix="cross_category"),
    }


def _model_counts(
    session: Session,
    *,
    exact_model: str | None = None,
    model_prefix: str | None = None,
) -> dict[str, int]:
    forecast_filter = (
        Forecast.model_name == exact_model
        if exact_model
        else Forecast.model_name.like(f"{model_prefix}%")
    )
    ranking_filter = (
        MarketRanking.forecast_model == exact_model
        if exact_model
        else MarketRanking.forecast_model.like(f"{model_prefix}%")
    )
    return {
        "forecasts": int(
            session.scalar(select(func.count()).select_from(Forecast).where(forecast_filter)) or 0
        ),
        "rankings": int(
            session.scalar(
                select(func.count()).select_from(MarketRanking).where(ranking_filter)
            )
            or 0
        ),
    }


def _active_market_counts(session: Session) -> dict[str, int]:
    rows = session.execute(
        select(MarketLeg.category, func.count(func.distinct(MarketLeg.ticker)))
        .join(Market, Market.ticker == MarketLeg.ticker)
        .where(func.lower(func.coalesce(Market.status, "")).in_(ACTIVE_MARKET_STATUSES))
        .group_by(MarketLeg.category)
    )
    return {str(category): int(count or 0) for category, count in rows}


def _table_count(session: Session, table: Any) -> int:
    return int(session.scalar(select(func.count()).select_from(table)) or 0)


def _summary(
    rows: list[dict[str, Any]],
    selected_after_weather: dict[str, Any],
    immediate: dict[str, Any],
) -> dict[str, Any]:
    return {
        "categories_reviewed": len(rows),
        "immediate_next_category": immediate["category"],
        "selected_next_category_after_weather": selected_after_weather["category"],
        "weather_status": _row_status(rows, "weather"),
        "sports_status": _row_status(rows, "sports"),
        "economic_parser_gap": _gap_value(rows, "economic", "parser_gap"),
        "news_parser_gap": _gap_value(rows, "news", "parser_gap"),
        "general_source_evidence_gap": _gap_value(
            rows, "general", "general_source_evidence_gap"
        ),
        "paper_ready_categories": [
            row["category"]
            for row in rows
            if row["paper_gate_readiness"] == "PAPER_GATE_READY"
        ],
    }


def _row_status(rows: list[dict[str, Any]], category: str) -> dict[str, Any] | None:
    row = next((item for item in rows if item["category"] == category), None)
    if row is None:
        return None
    return {
        "primary_blocker": row["primary_blocker"],
        "source_readiness": row["source_readiness"],
        "parser_readiness": row["parser_readiness"],
        "forecast_readiness": row["forecast_readiness"],
        "paper_gate_readiness": row["paper_gate_readiness"],
        "gap_counts": row["gap_counts"],
    }


def _gap_value(rows: list[dict[str, Any]], category: str, key: str) -> int:
    row = next((item for item in rows if item["category"] == category), None)
    if row is None:
        return 0
    return int(row.get("gap_counts", {}).get(key) or 0)


def _acceptance(
    rows: list[dict[str, Any]],
    selected_after_weather: dict[str, Any],
) -> dict[str, Any]:
    sports = next(row for row in rows if row["category"] == "sports")
    economic = next(row for row in rows if row["category"] == "economic")
    news = next(row for row in rows if row["category"] == "news")
    general = next(row for row in rows if row["category"] == "general")
    return {
        "weather_status_reflects_r2_r3_progress": True,
        "sports_provenance_gaps_counted": (
            sports["gap_counts"]["sports_partial_link_rows"] >= 0
            and sports["gap_counts"]["sports_unsupported_composites"] >= 0
        ),
        "economic_parser_gaps_counted": economic["gap_counts"]["parser_gap"] >= 0,
        "news_parser_gaps_counted": news["gap_counts"]["parser_gap"] >= 0,
        "general_source_evidence_gaps_counted": (
            general["gap_counts"]["general_source_evidence_gap"] >= 0
        ),
        "selected_one_next_noncrypto_build": selected_after_weather["category"] != "none",
        "no_paper_trades_created": True,
        "no_live_or_demo_orders": True,
    }


def _operator_guardrails() -> list[str]:
    return [
        "Keep PAPER / READ-ONLY.",
        "Do not create paper trades.",
        "Do not submit, cancel, replace, or amend live/demo exchange orders.",
        "Do not fabricate source evidence.",
        "Do not use fuzzy matching.",
    ]


def _metadata(
    session: Session,
    *,
    settings: Settings,
    generated_at: str,
    command_args: list[str],
) -> dict[str, Any]:
    db_url = database_url_from_settings(settings)
    return {
        "generated_at": generated_at,
        "repository_root": str(Path.cwd().resolve()),
        "git_branch": _git_value("rev-parse", "--abbrev-ref", "HEAD"),
        "git_commit": _git_value("rev-parse", "HEAD"),
        "git_dirty": _git_dirty_status(),
        "python_executable": str(Path(sys.executable).resolve()),
        "installed_package_path": str(Path(__file__).resolve()),
        "resolved_database_url": redact_database_url(db_url),
        "database_fingerprint": _database_fingerprint(db_url),
        "database_location": describe_db_location(db_url),
        "migration_revision": _migration_revision(session),
        "timezone": getattr(settings, "timezone", None) or "UTC",
        "command_arguments": {
            "command": "kalshi-bot phase3ba-r6-noncrypto-engine-backlog",
            "argv": command_args,
        },
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "live_or_demo_execution": False,
        "order_submission": False,
        "order_cancel_replace": False,
        "paper_trade_creation": False,
        "thresholds_lowered": False,
        "safety_flags": {
            "paper_only": True,
            "diagnostic_only": True,
            "creates_paper_trades": False,
            "places_exchange_orders": False,
            "submits_cancels_replaces_orders": False,
            "fabricates_source_evidence": False,
            "uses_fuzzy_matching": False,
        },
    }


def _database_fingerprint(db_url: str) -> dict[str, Any]:
    redacted = redact_database_url(db_url)
    sqlite_path = sqlite_path_from_url(db_url)
    if sqlite_path is None:
        return {
            "kind": "non_sqlite",
            "database_url_hash": hashlib.sha256(redacted.encode("utf-8")).hexdigest(),
        }
    if str(sqlite_path) == ":memory:":
        return {"kind": "sqlite_memory", "path": ":memory:"}
    path = sqlite_path.expanduser().resolve()
    if not path.exists():
        return {"kind": "missing_sqlite_file", "path": str(path)}
    stat = path.stat()
    payload = {
        "path": str(path),
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }
    return {
        "kind": "sqlite_file_stat",
        **payload,
        "fingerprint": hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode("utf-8")
        ).hexdigest(),
    }


def _migration_revision(session: Session) -> str | None:
    try:
        return session.execute(text("select version_num from alembic_version limit 1")).scalar()
    except Exception:
        return None


def _git_value(*args: str) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=Path.cwd(),
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "UNKNOWN"
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else "UNKNOWN"


def _git_dirty_status() -> str:
    try:
        result = subprocess.run(
            ["git", "status", "--short"],
            cwd=Path.cwd(),
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "UNKNOWN"
    if result.returncode != 0:
        return "UNKNOWN"
    return "dirty" if result.stdout.strip() else "clean"


def _read_json_if_exists(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _report_age_label(value: Any) -> str:
    parsed = parse_datetime(value)
    if parsed is None:
        return "unknown"
    seconds = int(max(0, (utc_now() - parsed).total_seconds()))
    return f"{seconds}s"


def _render_executive_summary(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    selected = payload["selected_next_noncrypto_build_after_weather"]
    immediate = payload["immediate_next_step"]
    lines = _metadata_lines(payload, title="# Phase 3BA-R6 Non-Crypto Engine Backlog")
    lines.extend(
        [
            "",
            "## Result",
            "",
            f"- Immediate category work: `{immediate['category']}` / `{immediate['stage']}`",
            (
                "- Selected next new category after weather: "
                f"`{selected['category']}`"
            ),
            f"- Selected blocker: `{selected.get('primary_blocker')}`",
            f"- Categories reviewed: `{summary['categories_reviewed']}`",
            "",
            "## Acceptance",
            "",
        ]
    )
    for key, value in payload["acceptance"].items():
        lines.append(f"- {key}: `{value}`")
    return "\n".join(lines) + "\n"


def _render_backlog_markdown(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, title="# Non-Crypto Category Engine Backlog")
    lines.extend(
        [
            "",
            "## Backlog",
            "",
            "| Category | Active | Parsed | Linked | Source | Parser | Forecast | "
            "Paper gate | Blocker | Next step |",
            "| --- | ---: | ---: | ---: | --- | --- | --- | --- | --- | --- |",
        ]
    )
    for row in payload["backlog_rows"]:
        lines.append(
            "| {category} | {active_market_count} | {parsed_market_count} | {linked_count} | "
            "{source_readiness} | {parser_readiness} | {forecast_readiness} | "
            "{paper_gate_readiness} | {primary_blocker} | {next_implementation_step} |".format(
                **_markdown_safe_row(row)
            )
        )
    lines.extend(["", "## Gap Counts", ""])
    for row in payload["backlog_rows"]:
        lines.append(f"### {row['category']}")
        for key, value in row["gap_counts"].items():
            lines.append(f"- {key}: `{value}`")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _render_next_category_build(payload: dict[str, Any]) -> str:
    selected = payload["selected_next_noncrypto_build_after_weather"]
    immediate = payload["immediate_next_step"]
    lines = _metadata_lines(payload, title="# Phase 3BA-R6 Next Category Build")
    lines.extend(
        [
            "",
            "## Immediate Work",
            "",
            f"- Category: `{immediate['category']}`",
            f"- Stage: `{immediate['stage']}`",
            f"- Reason: `{immediate['reason']}`",
            "",
            "```bash",
            immediate["command"],
            "```",
            "",
            "## Next New Non-Crypto Engine After Weather",
            "",
            f"- Category: `{selected['category']}`",
            f"- Primary blocker: `{selected.get('primary_blocker')}`",
            f"- Reason: {selected['reason']}",
            "",
            "## Implementation Step",
            "",
            selected["next_implementation_step"],
            "",
            "## Guardrails",
            "",
        ]
    )
    for guardrail in payload["operator_guardrails"]:
        lines.append(f"- {guardrail}")
    return "\n".join(lines) + "\n"


def _metadata_lines(payload: dict[str, Any], *, title: str) -> list[str]:
    return [
        title,
        "",
        f"- Generated at: `{payload['generated_at']}`",
        f"- Git commit: `{payload['git_commit']}`",
        f"- DB fingerprint: `{json.dumps(payload['database_fingerprint'], sort_keys=True)}`",
        f"- Command args: `{json.dumps(payload['command_arguments'], sort_keys=True)}`",
        f"- Safety flags: `{json.dumps(payload['safety_flags'], sort_keys=True)}`",
        f"- Live/demo execution: `{payload['live_or_demo_execution']}`",
        "- Order submission/cancel/replace: "
        f"`{payload['order_submission'] or payload['order_cancel_replace']}`",
        f"- Paper trade creation: `{payload['paper_trade_creation']}`",
        f"- Thresholds lowered: `{payload['thresholds_lowered']}`",
    ]


def _markdown_safe_row(row: dict[str, Any]) -> dict[str, Any]:
    safe = dict(row)
    for key, value in safe.items():
        if isinstance(value, str):
            safe[key] = value.replace("\n", "<br>").replace("|", "/")
    return safe


def _write_backlog_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fieldnames = [
        "category",
        "active_market_count",
        "parsed_market_count",
        "linked_count",
        "source_readiness",
        "parser_readiness",
        "forecast_readiness",
        "paper_gate_readiness",
        "primary_blocker",
        "next_implementation_step",
        "coverage_status",
        "coverage_percent",
        "score_after_weather",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fieldnames})


def _write_manifest(path: Path, files: list[Path]) -> None:
    lines = []
    for file_path in files:
        digest = hashlib.sha256(file_path.read_bytes()).hexdigest()
        lines.append(f"{digest}  {file_path.name}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
