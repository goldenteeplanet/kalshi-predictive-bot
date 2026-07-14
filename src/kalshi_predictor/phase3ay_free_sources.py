from __future__ import annotations

import csv
import hashlib
import json
import subprocess
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from sqlalchemy import and_, desc, func, or_, select
from sqlalchemy.orm import Session

from kalshi_predictor.active_universe import is_active_market_status
from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.data.repositories import decode_json
from kalshi_predictor.data.schema import (
    CryptoMarketLink,
    EconomicMarketLink,
    Forecast,
    Market,
    MarketLeg,
    MarketRanking,
    MarketSnapshot,
    NewsMarketLink,
    PaperOrder,
    SportsFeature,
    SportsMarketLink,
    WeatherMarketLink,
)
from kalshi_predictor.phase3z import PAPER_ONLY_SAFETY
from kalshi_predictor.utils.decimals import decimal_to_str
from kalshi_predictor.utils.time import utc_now

PHASE3AY_FREE_SOURCE_VERSION = "phase3ay_free_source_sprint_v1"
DEFAULT_OUTPUT_DIR = Path("reports/phase3ay")
DEFAULT_REPORTS_DIR = Path("reports")
PHASE3AY_MARKET_SCAN_COMMAND = (
    "kalshi-bot phase3ay-free-source-market-scan --output-dir reports/phase3ay --limit 5000"
)
PHASE3AY_CATEGORY_READINESS_COMMAND = (
    "kalshi-bot phase3ay-category-readiness --output-dir reports/phase3ay --reports-dir reports"
)
PHASE3AY_MULTICATEGORY_FUNNEL_COMMAND = (
    "kalshi-bot phase3ay-multicategory-paper-funnel "
    "--output-dir reports/phase3ay --reports-dir reports"
)
PHASE3AY_SPRINT_COMMAND = (
    "kalshi-bot phase3ay-free-source-sprint-report "
    "--output-dir reports/phase3ay --reports-dir reports"
)
PHASE3AH_R3_EXPANSION_COMMAND = (
    "kalshi-bot phase3ah-r3-bounded-scan-expansion "
    "--output-dir reports/phase3ah_r3 --reports-dir reports --max-rows 7500"
)

TARGET_CATEGORIES = (
    "weather",
    "sports",
    "economic",
    "news",
    "agriculture_commodities",
    "transportation_flight",
    "crypto",
)


@dataclass(frozen=True)
class Phase3AYFreeSourceArtifacts:
    output_dir: Path
    executive_summary_path: Path
    free_source_market_scan_md_path: Path
    free_source_market_scan_json_path: Path
    free_source_market_candidates_path: Path
    adapter_registry_md_path: Path
    adapter_registry_json_path: Path
    category_readiness_md_path: Path
    category_readiness_json_path: Path
    category_scorecard_path: Path
    multicategory_funnel_md_path: Path
    multicategory_funnel_json_path: Path
    multicategory_candidates_path: Path
    next_actions_path: Path
    next_category_sprint_path: Path
    sprint_report_json_path: Path
    manifest_path: Path


def build_free_source_adapter_registry() -> dict[str, Any]:
    adapters = [
        {
            "adapter_key": "nws_noaa_weather",
            "category": "weather",
            "source_name": "National Weather Service / NOAA",
            "source_url_pattern": "https://api.weather.gov/",
            "free_or_paid": "free",
            "official_or_third_party": "official",
            "update_cadence": "minutes_to_hours",
            "supported_market_patterns": ["temperature", "rain", "snow", "wind", "hurricane"],
            "freshness_requirement": "event_window_specific",
            "parser_status": "READY_EXISTING_TABLES",
            "forecast_safe_status": "REQUIRES_EXACT_LOCATION_AND_WINDOW",
            "link_safe_status": "REQUIRES_WEATHER_MARKET_LINK",
            "blockers": [],
        },
        {
            "adapter_key": "open_meteo_weather",
            "category": "weather",
            "source_name": "Open-Meteo",
            "source_url_pattern": "https://open-meteo.com/",
            "free_or_paid": "free",
            "official_or_third_party": "third_party_public",
            "update_cadence": "hourly",
            "supported_market_patterns": ["temperature", "precipitation", "wind"],
            "freshness_requirement": "event_window_specific",
            "parser_status": "REVIEW_REQUIRED",
            "forecast_safe_status": "SECONDARY_ONLY_UNTIL_VALIDATED",
            "link_safe_status": "REQUIRES_EXACT_LOCATION_AND_WINDOW",
            "blockers": ["SECONDARY_SOURCE_REVIEW_REQUIRED"],
        },
        {
            "adapter_key": "public_league_schedules",
            "category": "sports",
            "source_name": "Public league/team schedule and roster pages",
            "source_url_pattern": "league_official_schedule_or_roster_url",
            "free_or_paid": "free",
            "official_or_third_party": "official_or_public",
            "update_cadence": "daily_to_in_game",
            "supported_market_patterns": ["game winner", "team total", "player props"],
            "freshness_requirement": "game_and_roster_specific",
            "parser_status": "PARTIAL_EXISTING_TABLES",
            "forecast_safe_status": "BLOCKED_UNTIL_PROVENANCE_REPAIRED",
            "link_safe_status": "REQUIRES_PHASE3AH_R3_SAFE_ROWS",
            "blockers": ["SPORTS_PROVENANCE_REQUIRES_R3"],
        },
        {
            "adapter_key": "fred_economic",
            "category": "economic",
            "source_name": "FRED",
            "source_url_pattern": "https://fred.stlouisfed.org/",
            "free_or_paid": "free",
            "official_or_third_party": "official_public",
            "update_cadence": "release_calendar",
            "supported_market_patterns": ["rates", "inflation", "employment", "GDP"],
            "freshness_requirement": "release_specific",
            "parser_status": "AVAILABLE",
            "forecast_safe_status": "REQUIRES_RELEASE_MAPPING",
            "link_safe_status": "REQUIRES_ECONOMIC_MARKET_LINK",
            "blockers": [],
        },
        {
            "adapter_key": "bls_bea_census_treasury",
            "category": "economic",
            "source_name": "BLS / BEA / Census / Treasury",
            "source_url_pattern": "official_agency_release_url",
            "free_or_paid": "free",
            "official_or_third_party": "official",
            "update_cadence": "release_calendar",
            "supported_market_patterns": ["CPI", "jobs", "GDP", "retail sales", "Treasury"],
            "freshness_requirement": "release_specific",
            "parser_status": "AVAILABLE",
            "forecast_safe_status": "REQUIRES_RELEASE_MAPPING",
            "link_safe_status": "REQUIRES_ECONOMIC_MARKET_LINK",
            "blockers": [],
        },
        {
            "adapter_key": "tradingeconomics_deferred",
            "category": "economic",
            "source_name": "TradingEconomics",
            "source_url_pattern": "https://tradingeconomics.com/",
            "free_or_paid": "paid_or_restricted",
            "official_or_third_party": "third_party",
            "update_cadence": "realtime",
            "supported_market_patterns": ["economic calendar"],
            "freshness_requirement": "release_specific",
            "parser_status": "PAID_SOURCE_DEFERRED",
            "forecast_safe_status": "DEFERRED",
            "link_safe_status": "DEFERRED",
            "blockers": ["PAID_SOURCE_DEFERRED"],
        },
        {
            "adapter_key": "official_news_rss",
            "category": "news",
            "source_name": "Official public pages and RSS feeds",
            "source_url_pattern": "issuer_or_agency_public_feed",
            "free_or_paid": "free",
            "official_or_third_party": "official_or_public",
            "update_cadence": "event_driven",
            "supported_market_patterns": ["announcement", "policy", "company event", "geopolitics"],
            "freshness_requirement": "article_or_statement_specific",
            "parser_status": "PARTIAL_EXISTING_TABLES",
            "forecast_safe_status": "CONTEXT_ONLY_UNTIL_SOURCE_LINKED",
            "link_safe_status": "REQUIRES_NEWS_MARKET_LINK",
            "blockers": ["NEWS_CONTEXT_REVIEW_REQUIRED"],
        },
        {
            "adapter_key": "usda_ams_nass",
            "category": "agriculture_commodities",
            "source_name": "USDA AMS / NASS",
            "source_url_pattern": "https://www.usda.gov/",
            "free_or_paid": "free",
            "official_or_third_party": "official",
            "update_cadence": "release_calendar",
            "supported_market_patterns": ["corn", "soybeans", "wheat", "cattle", "crop reports"],
            "freshness_requirement": "release_or_report_specific",
            "parser_status": "AVAILABLE_NEEDS_LINKER",
            "forecast_safe_status": "REQUIRES_COMMODITY_LINKER",
            "link_safe_status": "LINKER_NOT_IMPLEMENTED",
            "blockers": ["COMMODITY_LINKER_NOT_IMPLEMENTED"],
        },
        {
            "adapter_key": "eia_energy",
            "category": "agriculture_commodities",
            "source_name": "EIA",
            "source_url_pattern": "https://www.eia.gov/",
            "free_or_paid": "free",
            "official_or_third_party": "official",
            "update_cadence": "release_calendar",
            "supported_market_patterns": ["oil", "natural gas", "gasoline", "inventories"],
            "freshness_requirement": "release_specific",
            "parser_status": "AVAILABLE_NEEDS_LINKER",
            "forecast_safe_status": "REQUIRES_ENERGY_LINKER",
            "link_safe_status": "LINKER_NOT_IMPLEMENTED",
            "blockers": ["ENERGY_LINKER_NOT_IMPLEMENTED"],
        },
        {
            "adapter_key": "faa_dot_bts",
            "category": "transportation_flight",
            "source_name": "FAA / DOT BTS",
            "source_url_pattern": "https://www.faa.gov/ and https://www.transtats.bts.gov/",
            "free_or_paid": "free",
            "official_or_third_party": "official",
            "update_cadence": "daily_to_realtime_public_pages",
            "supported_market_patterns": ["flight delays", "cancellations", "airport status"],
            "freshness_requirement": "date_and_airport_specific",
            "parser_status": "AVAILABLE_NEEDS_REVIEW",
            "forecast_safe_status": "REVIEW_TO_LINK_REQUIRED",
            "link_safe_status": "REQUIRES_FLIGHTAWARE_DATE_STABLE_REVIEW",
            "blockers": ["FLIGHTAWARE_REVIEW_TO_LINK_GATE"],
        },
        {
            "adapter_key": "coinbase_coingecko_crypto",
            "category": "crypto",
            "source_name": "Public crypto market data feeds",
            "source_url_pattern": "configured_public_crypto_feed",
            "free_or_paid": "free",
            "official_or_third_party": "third_party_public",
            "update_cadence": "minutes",
            "supported_market_patterns": ["BTC", "ETH", "XRP", "DOGE", "SOL"],
            "freshness_requirement": "current_window_specific",
            "parser_status": "READY_EXISTING_TABLES",
            "forecast_safe_status": "READY_WHEN_CURRENT",
            "link_safe_status": "REQUIRES_EXACT_CRYPTO_MARKET_LINK",
            "blockers": [],
        },
    ]
    return {
        "generated_at": utc_now().isoformat(),
        "phase_version": PHASE3AY_FREE_SOURCE_VERSION,
        "adapters": adapters,
        "summary": {
            "adapter_count": len(adapters),
            "free_public_adapter_count": sum(
                1 for row in adapters if str(row["free_or_paid"]).startswith("free")
            ),
            "paid_or_deferred_adapter_count": sum(
                1 for row in adapters if "paid" in str(row["free_or_paid"]).lower()
            ),
            "tradingeconomics_status": "PAID_SOURCE_DEFERRED",
        },
    }


def build_phase3ay_free_source_market_scan(
    session: Session,
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    reports_dir: Path = DEFAULT_REPORTS_DIR,
    settings: Settings | None = None,
    limit: int = 5000,
    command_args: list[str] | None = None,
) -> dict[str, Any]:
    del settings
    now = utc_now()
    limit = max(1, int(limit))
    registry = build_free_source_adapter_registry()
    markets = _current_markets(session, now=now, limit=limit)
    tickers = [market.ticker for market in markets]
    legs_by_ticker = _legs_by_ticker(session, tickers)
    evidence = _market_evidence(session, tickers)
    report_context = _report_context(reports_dir)
    rows = [
        _market_candidate_row(
            market,
            category=classify_free_source_category(market, legs_by_ticker.get(market.ticker, [])),
            evidence=evidence.get(market.ticker, {}),
            registry=registry,
            report_context=report_context,
            now=now,
        )
        for market in markets
    ]
    category_summary = _category_summary(rows)
    return {
        "generated_at": now.isoformat(),
        "phase": "3AY",
        "phase_version": PHASE3AY_FREE_SOURCE_VERSION,
        "mode": "PAPER_ONLY_READ_ONLY_FREE_SOURCE_MARKET_SCAN",
        "output_dir": str(output_dir),
        "reports_dir": str(reports_dir),
        "metadata": _run_metadata(session, command_args=command_args, rows=rows),
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "live_or_demo_execution": False,
        "order_submission": False,
        "paper_trade_creation": False,
        "thresholds_lowered": False,
        "fabricated_evidence": False,
        "stale_or_expired_current_opportunities_allowed": False,
        "sibling_or_fuzzy_matching": False,
        "summary": {
            "current_active_markets_scanned": len(rows),
            "categories_scanned": sorted(category_summary),
            "target_categories": list(TARGET_CATEGORIES),
            "current_candidate_rows": len(rows),
            "paper_ready_rows": sum(
                1 for row in rows if row["paper_funnel_status"] == "PAPER_READY"
            ),
            "positive_ev_rows": sum(1 for row in rows if row["ev_status"] == "EV_POSITIVE"),
            "top_blockers": _top_blockers(rows),
            "data_complete": False,
        },
        "category_summary": category_summary,
        "candidate_rows": rows,
        "adapter_registry_summary": registry["summary"],
    }


def build_phase3ay_category_readiness(
    session: Session,
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    reports_dir: Path = DEFAULT_REPORTS_DIR,
    settings: Settings | None = None,
    limit: int = 5000,
    command_args: list[str] | None = None,
) -> dict[str, Any]:
    scan = build_phase3ay_free_source_market_scan(
        session,
        output_dir=output_dir,
        reports_dir=reports_dir,
        settings=settings,
        limit=limit,
        command_args=command_args,
    )
    return _category_readiness_from_scan(scan, output_dir=output_dir, reports_dir=reports_dir)


def build_phase3ay_multicategory_paper_funnel(
    session: Session,
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    reports_dir: Path = DEFAULT_REPORTS_DIR,
    settings: Settings | None = None,
    limit: int = 5000,
    command_args: list[str] | None = None,
) -> dict[str, Any]:
    scan = build_phase3ay_free_source_market_scan(
        session,
        output_dir=output_dir,
        reports_dir=reports_dir,
        settings=settings,
        limit=limit,
        command_args=command_args,
    )
    return _multicategory_funnel_from_scan(scan, output_dir=output_dir, reports_dir=reports_dir)


def _category_readiness_from_scan(
    scan: dict[str, Any],
    *,
    output_dir: Path,
    reports_dir: Path,
) -> dict[str, Any]:
    scorecard = _category_scorecard(scan["candidate_rows"])
    next_sprint = _select_next_category_sprint(scorecard)
    return {
        "generated_at": utc_now().isoformat(),
        "phase": "3AY",
        "phase_version": PHASE3AY_FREE_SOURCE_VERSION,
        "mode": "PAPER_ONLY_READ_ONLY_CATEGORY_READINESS",
        "output_dir": str(output_dir),
        "reports_dir": str(reports_dir),
        "metadata": scan["metadata"],
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "live_or_demo_execution": False,
        "order_submission": False,
        "paper_trade_creation": False,
        "thresholds_lowered": False,
        "fabricated_evidence": False,
        "summary": {
            "categories_scanned": len(scorecard),
            "best_next_category": next_sprint["category"],
            "first_hard_blocker": next_sprint["first_hard_blocker"],
            "paper_ready_rows": sum(row["paper_ready_rows"] for row in scorecard),
            "positive_ev_rows": sum(row["positive_ev_rows"] for row in scorecard),
        },
        "category_scorecard": scorecard,
        "next_category_sprint": next_sprint,
    }


def _multicategory_funnel_from_scan(
    scan: dict[str, Any],
    *,
    output_dir: Path,
    reports_dir: Path,
) -> dict[str, Any]:
    rows = scan["candidate_rows"]
    stage_counts = {
        "current_active_rows": len(rows),
        "free_source_available_rows": sum(
            1 for row in rows if row["source_status"] == "FREE_SOURCE_AVAILABLE"
        ),
        "kalshi_link_ready_rows": sum(1 for row in rows if row["link_status"] == "LINK_READY"),
        "forecast_ready_rows": sum(1 for row in rows if row["forecast_status"] == "FORECAST_READY"),
        "book_ready_rows": sum(1 for row in rows if row["book_status"] == "BOOK_READY"),
        "positive_ev_rows": sum(1 for row in rows if row["ev_status"] == "EV_POSITIVE"),
        "paper_ready_rows": sum(1 for row in rows if row["paper_funnel_status"] == "PAPER_READY"),
    }
    return {
        "generated_at": utc_now().isoformat(),
        "phase": "3AY",
        "phase_version": PHASE3AY_FREE_SOURCE_VERSION,
        "mode": "PAPER_ONLY_READ_ONLY_MULTICATEGORY_PAPER_FUNNEL",
        "output_dir": str(output_dir),
        "reports_dir": str(reports_dir),
        "metadata": scan["metadata"],
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "live_or_demo_execution": False,
        "order_submission": False,
        "paper_trade_creation": False,
        "thresholds_lowered": False,
        "fabricated_evidence": False,
        "summary": {
            **stage_counts,
            "first_hard_blocker": _first_hard_blocker(rows),
            "top_blockers": _top_blockers(rows),
        },
        "stage_counts": stage_counts,
        "candidate_rows": rows,
    }


def build_phase3ay_free_source_sprint_report(
    session: Session,
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    reports_dir: Path = DEFAULT_REPORTS_DIR,
    settings: Settings | None = None,
    limit: int = 5000,
    command_args: list[str] | None = None,
    registered_commands: set[str] | None = None,
) -> dict[str, Any]:
    scan = build_phase3ay_free_source_market_scan(
        session,
        output_dir=output_dir,
        reports_dir=reports_dir,
        settings=settings,
        limit=limit,
        command_args=command_args,
    )
    registry = build_free_source_adapter_registry()
    readiness = _category_readiness_from_scan(scan, output_dir=output_dir, reports_dir=reports_dir)
    funnel = _multicategory_funnel_from_scan(scan, output_dir=output_dir, reports_dir=reports_dir)
    next_sprint = readiness["next_category_sprint"]
    command_audit = _command_audit(registered_commands or set(), next_sprint=next_sprint)
    return {
        "generated_at": utc_now().isoformat(),
        "phase": "3AY",
        "phase_version": PHASE3AY_FREE_SOURCE_VERSION,
        "mode": "PAPER_ONLY_READ_ONLY_FREE_SOURCE_EXPANSION_SPRINT",
        "output_dir": str(output_dir),
        "reports_dir": str(reports_dir),
        "metadata": scan["metadata"],
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "live_or_demo_execution": False,
        "order_submission": False,
        "paper_trade_creation": False,
        "thresholds_lowered": False,
        "fabricated_evidence": False,
        "stale_or_expired_current_opportunities_allowed": False,
        "sibling_or_fuzzy_matching": False,
        "summary": {
            "categories_scanned": len(readiness["category_scorecard"]),
            "markets_scanned": scan["summary"]["current_active_markets_scanned"],
            "best_next_category": next_sprint["category"],
            "best_next_category_score": next_sprint["sprint_score"],
            "first_hard_blocker": next_sprint["first_hard_blocker"],
            "top_blockers": funnel["summary"]["top_blockers"],
            "positive_ev_rows": funnel["summary"]["positive_ev_rows"],
            "paper_ready_rows": funnel["summary"]["paper_ready_rows"],
            "operator_next_command": _operator_next_command(command_audit),
            "next_codex_sprint": next_sprint["task_phase_name"],
        },
        "free_source_market_scan": scan,
        "free_source_adapter_registry": registry,
        "category_readiness": readiness,
        "multicategory_paper_funnel": funnel,
        "next_category_sprint": next_sprint,
        "command_registry_audit": command_audit,
        "next_actions": _registered_next_actions(command_audit),
        "operator_do_not_run": [
            "Do not submit, cancel, replace, or amend live/demo exchange orders.",
            "Do not create paper trades from this command.",
            (
                "Do not lower EV, confidence, liquidity, spread, score, "
                "settlement, or risk thresholds."
            ),
            (
                "Do not fabricate source values, links, features, forecasts, "
                "opportunities, books, fills, settlements, or outcomes."
            ),
            (
                "Do not use stale, expired, sibling, fuzzy, or historical "
                "rows as current opportunities."
            ),
        ],
    }


def write_phase3ay_free_source_market_scan_report(
    session: Session,
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    reports_dir: Path = DEFAULT_REPORTS_DIR,
    settings: Settings | None = None,
    limit: int = 5000,
    command_args: list[str] | None = None,
) -> Phase3AYFreeSourceArtifacts:
    return write_phase3ay_free_source_sprint_report(
        session,
        output_dir=output_dir,
        reports_dir=reports_dir,
        settings=settings,
        limit=limit,
        command_args=command_args,
    )


def write_phase3ay_free_source_sprint_report(
    session: Session,
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    reports_dir: Path = DEFAULT_REPORTS_DIR,
    settings: Settings | None = None,
    limit: int = 5000,
    command_args: list[str] | None = None,
    registered_commands: set[str] | None = None,
) -> Phase3AYFreeSourceArtifacts:
    payload = build_phase3ay_free_source_sprint_report(
        session,
        output_dir=output_dir,
        reports_dir=reports_dir,
        settings=settings or get_settings(),
        limit=limit,
        command_args=command_args,
        registered_commands=registered_commands,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    executive_summary_path = output_dir / "EXECUTIVE_SUMMARY.md"
    market_scan_md_path = output_dir / "free_source_market_scan.md"
    market_scan_json_path = output_dir / "free_source_market_scan.json"
    market_candidates_path = output_dir / "free_source_market_candidates.csv"
    registry_md_path = output_dir / "free_source_adapter_registry.md"
    registry_json_path = output_dir / "free_source_adapter_registry.json"
    readiness_md_path = output_dir / "category_readiness.md"
    readiness_json_path = output_dir / "category_readiness.json"
    scorecard_path = output_dir / "category_scorecard.csv"
    funnel_md_path = output_dir / "multicategory_paper_funnel.md"
    funnel_json_path = output_dir / "multicategory_paper_funnel.json"
    funnel_candidates_path = output_dir / "multicategory_candidates.csv"
    next_actions_path = output_dir / "NEXT_ACTIONS.md"
    next_category_sprint_path = output_dir / "NEXT_CATEGORY_SPRINT.md"
    sprint_report_json_path = output_dir / "free_source_sprint_report.json"
    manifest_path = output_dir / "MANIFEST.sha256"

    _write_json(sprint_report_json_path, payload)
    _write_json(market_scan_json_path, payload["free_source_market_scan"])
    _write_json(registry_json_path, payload["free_source_adapter_registry"])
    _write_json(readiness_json_path, payload["category_readiness"])
    _write_json(funnel_json_path, payload["multicategory_paper_funnel"])
    _write_candidate_csv(
        market_candidates_path,
        payload["free_source_market_scan"]["candidate_rows"],
    )
    _write_candidate_csv(
        funnel_candidates_path,
        payload["multicategory_paper_funnel"]["candidate_rows"],
    )
    _write_scorecard_csv(
        scorecard_path,
        payload["category_readiness"]["category_scorecard"],
    )
    executive_summary_path.write_text(_render_executive_summary(payload), encoding="utf-8")
    market_scan_md_path.write_text(
        _render_market_scan(payload["free_source_market_scan"]),
        encoding="utf-8",
    )
    registry_md_path.write_text(
        _render_adapter_registry(payload["free_source_adapter_registry"]),
        encoding="utf-8",
    )
    readiness_md_path.write_text(
        _render_category_readiness(payload["category_readiness"]),
        encoding="utf-8",
    )
    funnel_md_path.write_text(
        _render_multicategory_funnel(payload["multicategory_paper_funnel"]),
        encoding="utf-8",
    )
    next_actions_path.write_text(_render_next_actions(payload), encoding="utf-8")
    next_category_sprint_path.write_text(_render_next_category_sprint(payload), encoding="utf-8")
    _write_manifest(
        manifest_path,
        [
            executive_summary_path,
            market_scan_md_path,
            market_scan_json_path,
            market_candidates_path,
            registry_md_path,
            registry_json_path,
            readiness_md_path,
            readiness_json_path,
            scorecard_path,
            funnel_md_path,
            funnel_json_path,
            funnel_candidates_path,
            next_actions_path,
            next_category_sprint_path,
            sprint_report_json_path,
        ],
    )
    return Phase3AYFreeSourceArtifacts(
        output_dir=output_dir,
        executive_summary_path=executive_summary_path,
        free_source_market_scan_md_path=market_scan_md_path,
        free_source_market_scan_json_path=market_scan_json_path,
        free_source_market_candidates_path=market_candidates_path,
        adapter_registry_md_path=registry_md_path,
        adapter_registry_json_path=registry_json_path,
        category_readiness_md_path=readiness_md_path,
        category_readiness_json_path=readiness_json_path,
        category_scorecard_path=scorecard_path,
        multicategory_funnel_md_path=funnel_md_path,
        multicategory_funnel_json_path=funnel_json_path,
        multicategory_candidates_path=funnel_candidates_path,
        next_actions_path=next_actions_path,
        next_category_sprint_path=next_category_sprint_path,
        sprint_report_json_path=sprint_report_json_path,
        manifest_path=manifest_path,
    )


def classify_free_source_category(market: Market, legs: list[MarketLeg] | None = None) -> str:
    leg_categories = {str(leg.category or "").lower() for leg in legs or [] if leg.category}
    text = _market_text(market)
    ticker = (market.ticker or "").upper()
    series = (market.series_ticker or "").upper()
    event = (market.event_ticker or "").upper()
    if "sports" in leg_categories:
        return "sports"
    if "weather" in leg_categories:
        return "weather"
    if "economic" in leg_categories:
        return "economic"
    if "crypto" in leg_categories:
        return "crypto"
    if any(
        prefix in ticker or prefix in series or prefix in event
        for prefix in ("KXBTC", "KXETH", "KXSOLE", "KXSOL", "KXXRP", "KXDOGE")
    ):
        return "crypto"
    if any(term in text for term in ("bitcoin", "ethereum", "solana", "xrp", "doge", "crypto")):
        return "crypto"
    if any(
        term in text
        for term in (
            "temperature",
            "rain",
            "snow",
            "weather",
            "hurricane",
            "tornado",
            "wind",
            "precipitation",
        )
    ):
        return "weather"
    if any(
        term in text
        for term in (
            "mlb",
            "nba",
            "nfl",
            "nhl",
            "wnba",
            "soccer",
            "football",
            "baseball",
            "basketball",
            "hockey",
            "tennis",
            "ufc",
            "game",
            "player prop",
        )
    ):
        return "sports"
    if any(
        term in text
        for term in (
            "cpi",
            "inflation",
            "unemployment",
            "payroll",
            "gdp",
            "fomc",
            "fed",
            "interest rate",
            "treasury",
            "retail sales",
            "jobs report",
        )
    ):
        return "economic"
    if any(
        term in text
        for term in (
            "usda",
            "corn",
            "soy",
            "soybean",
            "wheat",
            "cattle",
            "crop",
            "oil",
            "natural gas",
            "gasoline",
            "eia",
            "commodity",
        )
    ):
        return "agriculture_commodities"
    if any(
        term in text
        for term in ("flight", "airport", "airline", "faa", "delay", "cancellation", "bts")
    ):
        return "transportation_flight"
    if any(
        term in text
        for term in (
            "news",
            "announce",
            "announcement",
            "war",
            "ceasefire",
            "policy",
            "company",
            "lawsuit",
            "supreme court",
        )
    ):
        return "news"
    return "unknown"


def _current_markets(session: Session, *, now: datetime, limit: int) -> list[Market]:
    statement = (
        select(Market)
        .where(
            or_(Market.result.is_(None), Market.result == ""),
            Market.settlement_ts.is_(None),
            or_(Market.close_time.is_(None), Market.close_time > now),
            or_(
                Market.expected_expiration_time.is_(None),
                Market.expected_expiration_time > now,
            ),
        )
        .order_by(Market.close_time.is_(None), Market.close_time, Market.ticker)
        .limit(limit * 3)
    )
    rows = []
    for market in session.scalars(statement):
        if not _is_current_market(market, now=now):
            continue
        rows.append(market)
        if len(rows) >= limit:
            break
    return rows


def _is_current_market(market: Market, *, now: datetime) -> bool:
    if not is_active_market_status(market.status):
        return False
    if market.result or market.settlement_ts:
        return False
    close_time = _aware(market.close_time)
    expiration = _aware(market.expected_expiration_time or market.expiration_time)
    if close_time is not None and close_time <= now:
        return False
    if expiration is not None and expiration <= now:
        return False
    text = _market_text(market)
    if any(
        term in text for term in ("synthetic", "local-only", "historical-only", "diagnostic-only")
    ):
        return False
    if "crosscategory" in (market.ticker or "").lower():
        return False
    return True


def _market_candidate_row(
    market: Market,
    *,
    category: str,
    evidence: dict[str, Any],
    registry: dict[str, Any],
    report_context: dict[str, Any],
    now: datetime,
) -> dict[str, Any]:
    adapters = _adapters_for_category(registry, category)
    free_source_available = any(
        str(row.get("free_or_paid", "")).startswith("free")
        and "DEFERRED" not in str(row.get("parser_status", ""))
        for row in adapters
    )
    link_count = _category_link_count(evidence, category)
    forecast_count = int(evidence.get("forecast_count") or 0)
    ranking = evidence.get("latest_ranking")
    snapshot = evidence.get("latest_snapshot")
    book_ready = _book_ready(snapshot)
    best_ev_cents = _ranking_ev_cents(ranking)
    source_status = "FREE_SOURCE_AVAILABLE" if free_source_available else "NO_FREE_SOURCE_ADAPTER"
    link_status = "LINK_READY" if link_count > 0 else _missing_link_status(category)
    forecast_status = "FORECAST_READY" if forecast_count > 0 else "FORECAST_NOT_AVAILABLE"
    book_status = "BOOK_READY" if book_ready else "NO_EXECUTABLE_BOOK"
    ev_status = (
        "EV_POSITIVE" if best_ev_cents is not None and best_ev_cents > 0 else "EV_NOT_POSITIVE"
    )
    special_blockers = _special_category_blockers(category, report_context=report_context)
    blockers = _row_blockers(
        source_status=source_status,
        link_status=link_status,
        forecast_status=forecast_status,
        book_status=book_status,
        ev_status=ev_status,
        special_blockers=special_blockers,
    )
    paper_ready = not blockers and int(evidence.get("paper_order_count") or 0) > 0
    if not paper_ready and not blockers:
        blockers.append("PAPER_GATE_NOT_RUN")
    return {
        "ticker": market.ticker,
        "category": category,
        "title": market.title or "",
        "series_ticker": market.series_ticker or "",
        "event_ticker": market.event_ticker or "",
        "status": market.status or "",
        "close_time": _iso_or_blank(market.close_time),
        "expected_expiration_time": _iso_or_blank(market.expected_expiration_time),
        "last_seen_at": _iso_or_blank(market.last_seen_at),
        "time_to_close_minutes": _minutes_until(market.close_time, now=now),
        "source_status": source_status,
        "source_adapter_keys": [str(row["adapter_key"]) for row in adapters],
        "link_status": link_status,
        "link_count": link_count,
        "forecast_status": forecast_status,
        "forecast_count": forecast_count,
        "book_status": book_status,
        "snapshot_age_minutes": _age_minutes(getattr(snapshot, "captured_at", None), now=now),
        "ev_status": ev_status,
        "best_ev_cents": decimal_to_str(best_ev_cents),
        "paper_funnel_status": "PAPER_READY" if paper_ready else "BLOCKED",
        "paper_ready": paper_ready,
        "paper_order_count": int(evidence.get("paper_order_count") or 0),
        "main_blocker": blockers[0] if blockers else "NONE",
        "blockers": blockers,
        "data_completeness": "complete" if not blockers and paper_ready else "partial",
    }


def _market_evidence(session: Session, tickers: list[str]) -> dict[str, dict[str, Any]]:
    evidence: dict[str, dict[str, Any]] = {ticker: {} for ticker in tickers}
    if not tickers:
        return evidence
    for key, model in (
        ("crypto_link_count", CryptoMarketLink),
        ("weather_link_count", WeatherMarketLink),
        ("economic_link_count", EconomicMarketLink),
        ("news_link_count", NewsMarketLink),
        ("sports_link_count", SportsMarketLink),
    ):
        for ticker, count in _count_by_ticker(session, model, tickers).items():
            evidence[ticker][key] = count
    for ticker, count in _count_by_ticker(session, Forecast, tickers).items():
        evidence[ticker]["forecast_count"] = count
    for ticker, count in _count_by_ticker(session, SportsFeature, tickers).items():
        evidence[ticker]["sports_feature_count"] = count
    for ticker, count in _count_by_ticker(session, PaperOrder, tickers).items():
        evidence[ticker]["paper_order_count"] = count
    for ticker, snapshot in _latest_by_ticker(
        session, MarketSnapshot, tickers, "captured_at"
    ).items():
        evidence[ticker]["latest_snapshot"] = snapshot
    for ticker, ranking in _latest_by_ticker(session, MarketRanking, tickers, "ranked_at").items():
        evidence[ticker]["latest_ranking"] = ranking
    return evidence


def _count_by_ticker(session: Session, model: type[Any], tickers: list[str]) -> dict[str, int]:
    if not tickers:
        return {}
    ticker_column = model.ticker
    rows = session.execute(
        select(ticker_column, func.count())
        .where(ticker_column.in_(tickers))
        .group_by(ticker_column)
    ).all()
    return {str(ticker): int(count or 0) for ticker, count in rows}


def _latest_by_ticker(
    session: Session,
    model: type[Any],
    tickers: list[str],
    time_column_name: str,
) -> dict[str, Any]:
    if not tickers:
        return {}
    time_column = getattr(model, time_column_name)
    subquery = (
        select(model.ticker.label("ticker"), func.max(time_column).label("latest_at"))
        .where(model.ticker.in_(tickers))
        .group_by(model.ticker)
        .subquery()
    )
    rows = session.scalars(
        select(model)
        .join(
            subquery,
            and_(model.ticker == subquery.c.ticker, time_column == subquery.c.latest_at),
        )
        .order_by(desc(time_column))
    ).all()
    latest: dict[str, Any] = {}
    for row in rows:
        latest.setdefault(str(row.ticker), row)
    return latest


def _legs_by_ticker(session: Session, tickers: list[str]) -> dict[str, list[MarketLeg]]:
    grouped: dict[str, list[MarketLeg]] = defaultdict(list)
    if not tickers:
        return grouped
    for leg in session.scalars(select(MarketLeg).where(MarketLeg.ticker.in_(tickers))):
        grouped[str(leg.ticker)].append(leg)
    return grouped


def _category_link_count(evidence: dict[str, Any], category: str) -> int:
    return int(
        evidence.get(
            {
                "crypto": "crypto_link_count",
                "weather": "weather_link_count",
                "economic": "economic_link_count",
                "news": "news_link_count",
                "sports": "sports_link_count",
            }.get(category, "missing_link_count"),
            0,
        )
        or 0
    )


def _missing_link_status(category: str) -> str:
    if category == "sports":
        return "SPORTS_PROVENANCE_BLOCKED"
    if category == "transportation_flight":
        return "FLIGHT_SOURCE_REVIEW_BLOCKED"
    if category in {"agriculture_commodities", "unknown"}:
        return "LINKER_NOT_IMPLEMENTED"
    return "KALSHI_LINK_UNVERIFIED"


def _report_context(reports_dir: Path) -> dict[str, Any]:
    return {
        "sports_r3": _read_json(reports_dir / "phase3ah_r3" / "sports_provenance_repair.json"),
        "flightaware_r5": _read_json(
            reports_dir / "phase3bb_r5_flightaware" / "flightaware_date_stable_evidence.json"
        ),
    }


def _row_blockers(
    *,
    source_status: str,
    link_status: str,
    forecast_status: str,
    book_status: str,
    ev_status: str,
    special_blockers: list[str],
) -> list[str]:
    blockers: list[str] = []
    if source_status != "FREE_SOURCE_AVAILABLE":
        blockers.append(source_status)
    if link_status != "LINK_READY":
        blockers.append(link_status)
    blockers.extend(special_blockers)
    if forecast_status != "FORECAST_READY":
        blockers.append(forecast_status)
    if book_status != "BOOK_READY":
        blockers.append(book_status)
    if ev_status != "EV_POSITIVE":
        blockers.append(ev_status)
    return _unique(blockers)


def _special_category_blockers(
    category: str,
    *,
    report_context: dict[str, Any],
) -> list[str]:
    if category == "sports":
        report = report_context.get("sports_r3")
        report = report if isinstance(report, dict) else {}
        summary = report.get("summary", {}) if isinstance(report.get("summary"), dict) else {}
        if int(summary.get("rows_safe_to_repair") or 0) > 0:
            return ["SPORTS_SAFE_ROWS_REQUIRE_PHASE3AE_REVIEW"]
        if summary.get("status") == "NO_SAFE_SPORTS_REPAIR_ROWS":
            return ["SPORTS_NO_SAFE_REPAIR_ROWS"]
        return ["SPORTS_PROVENANCE_REQUIRES_R3"]
    if category == "transportation_flight":
        report = report_context.get("flightaware_r5")
        report = report if isinstance(report, dict) else {}
        summary = report.get("summary", {}) if isinstance(report.get("summary"), dict) else {}
        if (
            int(summary.get("link_safe_rows") or 0) > 0
            or int(summary.get("accepted_date_stable_evidence_rows") or 0) > 0
        ):
            return []
        return ["FLIGHTAWARE_REVIEW_TO_LINK_GATE"]
    return []


def _category_summary(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row["category"])].append(row)
    summary: dict[str, dict[str, Any]] = {}
    for category in sorted(set(TARGET_CATEGORIES) | set(grouped)):
        category_rows = grouped.get(category, [])
        summary[category] = {
            "current_active_markets": len(category_rows),
            "free_source_available_rows": sum(
                1 for row in category_rows if row["source_status"] == "FREE_SOURCE_AVAILABLE"
            ),
            "linked_rows": sum(1 for row in category_rows if row["link_status"] == "LINK_READY"),
            "forecast_ready_rows": sum(
                1 for row in category_rows if row["forecast_status"] == "FORECAST_READY"
            ),
            "book_ready_rows": sum(
                1 for row in category_rows if row["book_status"] == "BOOK_READY"
            ),
            "positive_ev_rows": sum(
                1 for row in category_rows if row["ev_status"] == "EV_POSITIVE"
            ),
            "paper_ready_rows": sum(1 for row in category_rows if row["paper_ready"]),
            "top_blocker": _first_hard_blocker(category_rows),
        }
    return summary


def _category_scorecard(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row["category"])].append(row)
    scorecard: list[dict[str, Any]] = []
    for category in sorted(set(TARGET_CATEGORIES) | set(grouped)):
        category_rows = grouped.get(category, [])
        active = len(category_rows)
        free_source = sum(
            1 for row in category_rows if row["source_status"] == "FREE_SOURCE_AVAILABLE"
        )
        linked = sum(1 for row in category_rows if row["link_status"] == "LINK_READY")
        forecast_ready = sum(
            1 for row in category_rows if row["forecast_status"] == "FORECAST_READY"
        )
        book_ready = sum(1 for row in category_rows if row["book_status"] == "BOOK_READY")
        positive_ev = sum(1 for row in category_rows if row["ev_status"] == "EV_POSITIVE")
        paper_ready = sum(1 for row in category_rows if row["paper_ready"])
        score = _category_sprint_score(
            category=category,
            active=active,
            free_source=free_source,
            linked=linked,
            forecast_ready=forecast_ready,
            book_ready=book_ready,
        )
        scorecard.append(
            {
                "category": category,
                "current_active_markets": active,
                "free_source_available_rows": free_source,
                "linked_rows": linked,
                "verified_kalshi_links": linked,
                "forecast_ready_rows": forecast_ready,
                "book_ready_rows": book_ready,
                "positive_ev_rows": positive_ev,
                "paper_ready_rows": paper_ready,
                "top_blocker": _first_hard_blocker(category_rows),
                "sprint_score": score,
                "next_action": _category_next_action(category, category_rows),
            }
        )
    return sorted(
        scorecard,
        key=lambda row: (int(row["sprint_score"]), int(row["current_active_markets"])),
        reverse=True,
    )


def _category_sprint_score(
    *,
    category: str,
    active: int,
    free_source: int,
    linked: int,
    forecast_ready: int,
    book_ready: int,
) -> int:
    if active <= 0:
        return 0
    source_bonus = 25 if free_source > 0 else 0
    link_bonus = min(linked, 20)
    forecast_bonus = min(forecast_ready * 2, 20)
    book_bonus = min(book_ready, 20)
    crypto_penalty = 35 if category == "crypto" else 0
    category_bonus = {
        "weather": 10,
        "economic": 12,
        "agriculture_commodities": 14,
        "transportation_flight": 8,
        "sports": 9,
        "news": 6,
    }.get(category, 0)
    return max(
        0,
        active
        + source_bonus
        + link_bonus
        + forecast_bonus
        + book_bonus
        + category_bonus
        - crypto_penalty,
    )


def _select_next_category_sprint(scorecard: list[dict[str, Any]]) -> dict[str, Any]:
    viable = [row for row in scorecard if int(row.get("current_active_markets") or 0) > 0]
    if not viable:
        return {
            "category": "NO_CURRENT_ACTIVE_FREE_SOURCE_MARKETS",
            "task_phase_name": "Phase 3AY-R1 Current Market Discovery Refresh",
            "sprint_score": 0,
            "first_hard_blocker": "NO_CURRENT_ACTIVE_MARKETS",
            "why_this_category": (
                "No current active non-expired markets were available in the bounded scan."
            ),
            "next_operator_command": PHASE3AY_SPRINT_COMMAND,
            "acceptance_criteria": [
                "Keep the command report-only and bounded.",
                "Do not create paper trades from discovery diagnostics.",
            ],
        }
    non_exhausted = [row for row in viable if not _category_exhausted_for_next_sprint(row)]
    preferred = (
        [row for row in non_exhausted if row["category"] != "crypto"]
        or non_exhausted
        or [row for row in viable if row["category"] != "crypto"]
        or viable
    )
    best = max(
        preferred, key=lambda row: (int(row["sprint_score"]), int(row["current_active_markets"]))
    )
    category = str(best["category"])
    task_name = {
        "sports": "Phase 3AH-R3 Sports Provenance Bounded Scan Expansion",
        "weather": "Phase 3AY-R1 Weather Free Source Exact Linker Sprint",
        "economic": "Phase 3AN Economic/News Compatibility Watch",
        "news": "Phase 3AN Economic/News Compatibility Watch",
        "agriculture_commodities": "Phase 3AY-R1 USDA/EIA Commodity Linker Sprint",
        "transportation_flight": "Phase 3BB-R6 Flight Source Linker Activation Sprint",
        "crypto": "Phase 3AY Positive EV Accelerator Continuation",
    }.get(category, "Phase 3AY-R1 Free Source Linker Sprint")
    return {
        "category": category,
        "task_phase_name": task_name,
        "sprint_score": int(best["sprint_score"]),
        "first_hard_blocker": str(best["top_blocker"]),
        "why_this_category": (
            f"{category} has {best['current_active_markets']} current market(s), "
            f"{best['free_source_available_rows']} free-source-ready row(s), "
            f"and top blocker {best['top_blocker']}."
        ),
        "next_operator_command": PHASE3AY_SPRINT_COMMAND,
        "acceptance_criteria": [
            "Keep PAPER / READ-ONLY safety intact.",
            "Only use exact market tickers and exact source links.",
            "Do not lower thresholds or create paper trades from partial data.",
            "Report current source/link/forecast/book evidence separately.",
        ],
    }


def _category_exhausted_for_next_sprint(row: dict[str, Any]) -> bool:
    return row.get("category") == "sports" and row.get("top_blocker") in {
        "SPORTS_NO_SAFE_REPAIR_ROWS",
        "HOLD_PLACEHOLDER_UPGRADES",
        "HOLD_PARTIAL_PROVENANCE",
    }


def _category_next_action(category: str, rows: list[dict[str, Any]]) -> str:
    blocker = _first_hard_blocker(rows)
    if not rows:
        return "Wait for current active markets in this category."
    if category == "sports" and blocker in {
        "SPORTS_PROVENANCE_REQUIRES_R3",
        "SPORTS_NO_SAFE_REPAIR_ROWS",
    }:
        return "Run the bounded sports provenance scan expansion before any sports upgrade."
    if category == "transportation_flight":
        return "Complete FlightAware/date-stable review-to-link evidence before forecasting."
    if blocker in {"LINKER_NOT_IMPLEMENTED", "KALSHI_LINK_UNVERIFIED"}:
        return "Build exact-ticker free-source linker and report verified link evidence."
    if blocker == "FORECAST_NOT_AVAILABLE":
        return "Add report-only forecast features after exact source links are proven."
    if blocker == "NO_EXECUTABLE_BOOK":
        return "Refresh books only after source and forecast evidence are current."
    if blocker == "EV_NOT_POSITIVE":
        return "Wait for positive EV without lowering thresholds."
    return f"Resolve {blocker}."


def _first_hard_blocker(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "NO_CURRENT_MARKETS"
    counter: Counter[str] = Counter()
    for row in rows:
        blocker = str(row.get("main_blocker") or "UNKNOWN")
        if blocker != "NONE":
            counter[blocker] += 1
    if not counter:
        return "NONE"
    return counter.most_common(1)[0][0]


def _top_blockers(rows: list[dict[str, Any]], *, limit: int = 8) -> list[dict[str, Any]]:
    counter: Counter[str] = Counter()
    for row in rows:
        for blocker in row.get("blockers", []):
            counter[str(blocker)] += 1
    return [{"blocker": blocker, "rows": count} for blocker, count in counter.most_common(limit)]


def _command_audit(
    registered_commands: set[str],
    *,
    next_sprint: dict[str, Any],
) -> dict[str, Any]:
    commands = [
        PHASE3AY_MARKET_SCAN_COMMAND,
        PHASE3AY_CATEGORY_READINESS_COMMAND,
        PHASE3AY_MULTICATEGORY_FUNNEL_COMMAND,
        PHASE3AY_SPRINT_COMMAND,
    ]
    if next_sprint.get("category") == "sports":
        commands.append(PHASE3AH_R3_EXPANSION_COMMAND)
    rows = []
    for command in commands:
        name = _command_name(command)
        rows.append(
            {
                "command": name,
                "full_command": command,
                "registered": name in registered_commands,
                "included_in_next_actions": name in registered_commands,
            }
        )
    return {
        "candidate_commands": rows,
        "missing_command_names": [row["command"] for row in rows if not row["registered"]],
        "next_actions_reference_only_registered_commands": True,
    }


def _registered_next_actions(command_audit: dict[str, Any]) -> list[str]:
    return [
        str(row["full_command"])
        for row in command_audit["candidate_commands"]
        if row.get("registered")
    ]


def _operator_next_command(command_audit: dict[str, Any]) -> str:
    for row in command_audit["candidate_commands"]:
        if row.get("registered") and row["command"] == "phase3ay-free-source-sprint-report":
            return str(row["full_command"])
    for row in command_audit["candidate_commands"]:
        if row.get("registered"):
            return str(row["full_command"])
    return "NO_REGISTERED_OPERATOR_COMMAND"


def _run_metadata(
    session: Session,
    *,
    command_args: list[str] | None,
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "generated_at": utc_now().isoformat(),
        "command_args": command_args or [],
        "git_commit": _git_commit(),
        "db_fingerprint": _db_fingerprint(session),
        "data_watermark": _data_watermark(session, rows=rows),
        "safety_flags": {
            "paper_only": True,
            "live_demo_order_submission": False,
            "order_cancel_replace": False,
            "paper_trade_creation": False,
            "thresholds_lowered": False,
            "fabricated_evidence": False,
        },
    }


def _git_commit() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=Path.cwd(),
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "UNAVAILABLE"
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else "UNAVAILABLE"


def _db_fingerprint(session: Session) -> dict[str, Any]:
    bind = session.get_bind()
    url = bind.url.render_as_string(hide_password=True)
    return {
        "dialect": bind.dialect.name,
        "url_hash": hashlib.sha256(url.encode("utf-8")).hexdigest()[:16],
    }


def _data_watermark(session: Session, *, rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "market_last_seen_at_max": _iso_or_blank(
            session.scalar(select(func.max(Market.last_seen_at)))
        ),
        "snapshot_captured_at_max": _iso_or_blank(
            session.scalar(select(func.max(MarketSnapshot.captured_at)))
        ),
        "forecasted_at_max": _iso_or_blank(
            session.scalar(select(func.max(Forecast.forecasted_at)))
        ),
        "ranked_at_max": _iso_or_blank(session.scalar(select(func.max(MarketRanking.ranked_at)))),
        "current_candidate_rows": len(rows),
    }


def _render_executive_summary(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    next_sprint = payload["next_category_sprint"]
    lines = [
        "# Phase 3AY Free Source Expansion Sprint",
        "",
        f"- Generated at: `{payload['generated_at']}`",
        f"- Mode: `{payload['mode']}`",
        f"- Categories scanned: `{summary['categories_scanned']}`",
        f"- Markets scanned: `{summary['markets_scanned']}`",
        f"- Best next category: `{summary['best_next_category']}`",
        f"- First hard blocker: `{summary['first_hard_blocker']}`",
        f"- Positive-EV rows: `{summary['positive_ev_rows']}`",
        f"- Paper-ready rows: `{summary['paper_ready_rows']}`",
        f"- Next Codex sprint: `{summary['next_codex_sprint']}`",
        f"- Next operator command: `{summary['operator_next_command']}`",
        "",
        "## What should the operator do next?",
        "",
        next_sprint["why_this_category"],
        "",
        "## Top blockers",
        "",
    ]
    if summary["top_blockers"]:
        lines.extend(
            f"- `{row['blocker']}`: `{row['rows']}` row(s)" for row in summary["top_blockers"]
        )
    else:
        lines.append("- `NONE`")
    lines.extend(
        [
            "",
            (
                "No live/demo exchange writes, paper trades, threshold changes, "
                "or fabricated evidence were created."
            ),
            "",
        ]
    )
    return "\n".join(lines)


def _render_market_scan(payload: dict[str, Any]) -> str:
    lines = [
        "# Free Source Market Scan",
        "",
        f"- Generated at: `{payload['generated_at']}`",
        f"- Current candidate rows: `{payload['summary']['current_candidate_rows']}`",
        f"- Positive-EV rows: `{payload['summary']['positive_ev_rows']}`",
        f"- Paper-ready rows: `{payload['summary']['paper_ready_rows']}`",
        "",
        "## Category Summary",
        "",
        (
            "| Category | Current | Source | Linked | Forecast | Book | "
            "Positive EV | Paper Ready | Top Blocker |"
        ),
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for category, row in payload["category_summary"].items():
        lines.append(
            f"| {category} | {row['current_active_markets']} | "
            f"{row['free_source_available_rows']} | {row['linked_rows']} | "
            f"{row['forecast_ready_rows']} | {row['book_ready_rows']} | "
            f"{row['positive_ev_rows']} | {row['paper_ready_rows']} | `{row['top_blocker']}` |"
        )
    lines.append("")
    return "\n".join(lines)


def _render_adapter_registry(payload: dict[str, Any]) -> str:
    lines = [
        "# Free Source Adapter Registry",
        "",
        f"- Generated at: `{payload['generated_at']}`",
        f"- Adapter count: `{payload['summary']['adapter_count']}`",
        f"- TradingEconomics status: `{payload['summary']['tradingeconomics_status']}`",
        "",
        "| Category | Adapter | Source | Status | Blockers |",
        "| --- | --- | --- | --- | --- |",
    ]
    for row in payload["adapters"]:
        blockers = ", ".join(row.get("blockers") or []) or "NONE"
        lines.append(
            f"| {row['category']} | `{row['adapter_key']}` | {row['source_name']} | "
            f"`{row['parser_status']}` | `{blockers}` |"
        )
    lines.append("")
    return "\n".join(lines)


def _render_category_readiness(payload: dict[str, Any]) -> str:
    lines = [
        "# Category Readiness",
        "",
        f"- Best next category: `{payload['summary']['best_next_category']}`",
        f"- First hard blocker: `{payload['summary']['first_hard_blocker']}`",
        "",
        (
            "| Category | Score | Current | Source | Linked | Forecast | Book | "
            "Positive EV | Paper Ready | Top Blocker |"
        ),
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for row in payload["category_scorecard"]:
        lines.append(
            f"| {row['category']} | {row['sprint_score']} | {row['current_active_markets']} | "
            f"{row['free_source_available_rows']} | {row['linked_rows']} | "
            f"{row['forecast_ready_rows']} | {row['book_ready_rows']} | "
            f"{row['positive_ev_rows']} | {row['paper_ready_rows']} | "
            f"`{row['top_blocker']}` |"
        )
    lines.append("")
    return "\n".join(lines)


def _render_multicategory_funnel(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    lines = [
        "# Multicategory Paper Funnel",
        "",
        f"- Current active rows: `{summary['current_active_rows']}`",
        f"- Free source available rows: `{summary['free_source_available_rows']}`",
        f"- Linked rows: `{summary['kalshi_link_ready_rows']}`",
        f"- Forecast-ready rows: `{summary['forecast_ready_rows']}`",
        f"- Book-ready rows: `{summary['book_ready_rows']}`",
        f"- Positive-EV rows: `{summary['positive_ev_rows']}`",
        f"- Paper-ready rows: `{summary['paper_ready_rows']}`",
        f"- First hard blocker: `{summary['first_hard_blocker']}`",
        "",
        "## Blockers",
        "",
    ]
    if summary["top_blockers"]:
        lines.extend(
            f"- `{row['blocker']}`: `{row['rows']}` row(s)" for row in summary["top_blockers"]
        )
    else:
        lines.append("- `NONE`")
    lines.append("")
    return "\n".join(lines)


def _render_next_actions(payload: dict[str, Any]) -> str:
    lines = [
        "# Phase 3AY Next Actions",
        "",
        f"- Next Codex sprint: `{payload['summary']['next_codex_sprint']}`",
        f"- Best next category: `{payload['summary']['best_next_category']}`",
        f"- First hard blocker: `{payload['summary']['first_hard_blocker']}`",
        "",
        "## Registered Commands",
        "",
    ]
    if payload["next_actions"]:
        lines.extend(f"- `{command}`" for command in payload["next_actions"])
    else:
        lines.append("- No registered command recommendations are available.")
    lines.extend(
        [
            "",
            "## Safety",
            "",
            "- Keep PAPER / READ-ONLY.",
            "- Do not create paper trades from partial source diagnostics.",
            "- Do not submit/cancel/replace/amend live or demo exchange orders.",
            "",
        ]
    )
    return "\n".join(lines)


def _render_next_category_sprint(payload: dict[str, Any]) -> str:
    sprint = payload["next_category_sprint"]
    lines = [
        "# Next Category Sprint",
        "",
        f"Task phase name: `{sprint['task_phase_name']}`",
        "",
        f"Category: `{sprint['category']}`",
        "",
        f"Reason: {sprint['why_this_category']}",
        "",
        f"First hard blocker: `{sprint['first_hard_blocker']}`",
        "",
        "Acceptance criteria:",
    ]
    lines.extend(f"- {item}" for item in sprint["acceptance_criteria"])
    lines.append("")
    return "\n".join(lines)


def _write_candidate_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fieldnames = [
        "ticker",
        "category",
        "title",
        "status",
        "close_time",
        "source_status",
        "link_status",
        "forecast_status",
        "book_status",
        "ev_status",
        "paper_funnel_status",
        "main_blocker",
        "best_ev_cents",
        "time_to_close_minutes",
        "source_adapter_keys",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    key: json.dumps(row.get(key), sort_keys=True)
                    if isinstance(row.get(key), (list, dict))
                    else row.get(key)
                    for key in fieldnames
                }
            )


def _write_scorecard_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fieldnames = [
        "category",
        "sprint_score",
        "current_active_markets",
        "free_source_available_rows",
        "linked_rows",
        "verified_kalshi_links",
        "forecast_ready_rows",
        "book_ready_rows",
        "positive_ev_rows",
        "paper_ready_rows",
        "top_blocker",
        "next_action",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key) for key in fieldnames})


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")


def _write_manifest(path: Path, files: list[Path]) -> None:
    lines = []
    for artifact in files:
        if artifact.exists():
            digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
            lines.append(f"{digest}  {artifact.name}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        decoded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _adapters_for_category(registry: dict[str, Any], category: str) -> list[dict[str, Any]]:
    return [
        row
        for row in registry.get("adapters", [])
        if isinstance(row, dict) and row.get("category") == category
    ]


def _book_ready(snapshot: MarketSnapshot | None) -> bool:
    if snapshot is None:
        return False
    if snapshot.raw_orderbook_json:
        parsed = decode_json(snapshot.raw_orderbook_json)
        if parsed:
            return True
    return bool(snapshot.best_yes_ask or snapshot.best_no_ask)


def _ranking_ev_cents(ranking: MarketRanking | None) -> Decimal | None:
    if ranking is None:
        return None
    for value in (
        ranking.estimated_edge,
        decode_json(ranking.raw_json).get("expected_value_cents"),
    ):
        parsed = _decimal_value(value)
        if parsed is not None:
            return parsed
    raw = decode_json(ranking.raw_json)
    for key in ("expected_value", "estimated_edge", "edge"):
        parsed = _decimal_value(raw.get(key))
        if parsed is not None:
            return parsed
    return None


def _decimal_value(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _market_text(market: Market) -> str:
    return " ".join(
        str(value or "")
        for value in (
            market.ticker,
            market.event_ticker,
            market.series_ticker,
            market.title,
            market.subtitle,
            market.rules_primary,
            market.rules_secondary,
        )
    ).lower()


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _iso_or_blank(value: Any) -> str:
    if isinstance(value, datetime):
        return _aware(value).isoformat() if _aware(value) else ""
    return str(value) if value is not None else ""


def _minutes_until(value: datetime | None, *, now: datetime) -> int | None:
    aware = _aware(value)
    if aware is None:
        return None
    return int((aware - now).total_seconds() // 60)


def _age_minutes(value: datetime | None, *, now: datetime) -> int | None:
    aware = _aware(value)
    if aware is None:
        return None
    return max(0, int((now - aware).total_seconds() // 60))


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _command_name(command: str) -> str:
    parts = command.split()
    return parts[1] if len(parts) > 1 and parts[0] == "kalshi-bot" else parts[0]
