from __future__ import annotations

import csv
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.data.schema import (
    EconomicEvent,
    EconomicFeature,
    EconomicMarketLink,
    Market,
    MarketLeg,
)
from kalshi_predictor.market_legs import CATEGORY_ECONOMIC, ParsedMarketLeg, parse_market_legs
from kalshi_predictor.phase3bb_acceleration import (
    _metadata,
    _metadata_lines,
    _safety_flags,
    _write_manifest,
)
from kalshi_predictor.utils.time import utc_now

PHASE3BB_R4_VERSION = "phase3bb_r4_economic_parser_backfill_v1"
ACTIVE_MARKET_STATUSES = ("active", "open")
MIN_LINK_CONFIDENCE = 0.7
MIN_PARSER_CONFIDENCE = 0.55


@dataclass(frozen=True)
class SourceMapping:
    official_source_family: str
    source_options: tuple[str, ...]
    paid_deferred_sources: tuple[str, ...] = ("TradingEconomics=DEFERRED",)
    supported: bool = True


@dataclass(frozen=True)
class Phase3BBR4EconomicParserBackfillArtifacts:
    output_dir: Path
    executive_summary_path: Path
    markdown_path: Path
    rows_csv_path: Path
    source_mapping_backlog_path: Path
    manifest_path: Path


SOURCE_MAPPING_BY_PATTERN: dict[str, SourceMapping] = {
    "CPI": SourceMapping("BLS", ("BLS CPI release", "FRED CPI series")),
    "jobs/payroll": SourceMapping("BLS", ("BLS Employment Situation", "FRED payroll series")),
    "unemployment": SourceMapping("BLS", ("BLS Employment Situation", "FRED unemployment series")),
    "Fed rates": SourceMapping(
        "Federal Reserve",
        ("Federal Reserve FOMC calendar", "Federal Reserve target rate data", "FRED fed funds"),
    ),
    "GDP": SourceMapping("BEA", ("BEA GDP releases", "FRED GDP series")),
    "inflation": SourceMapping(
        "BLS/FRED",
        ("BLS CPI/PPI releases", "FRED inflation series", "BEA PCE releases"),
    ),
    "Treasury": SourceMapping(
        "Treasury",
        ("U.S. Treasury auction/rate data", "FRED Treasury series", "Federal Reserve H.15"),
    ),
    "other": SourceMapping(
        "UNSUPPORTED_PATTERN",
        ("manual official-source mapping required",),
        supported=False,
    ),
}

PATTERN_RULES: tuple[tuple[str, str], ...] = (
    ("CPI", r"\bcpi\b|consumer price index"),
    ("jobs/payroll", r"nonfarm payroll|payroll|jobs report|employment report"),
    ("unemployment", r"unemployment|jobless"),
    (
        "Fed rates",
        r"federal reserve|fomc|\bfed\b|interest rate|rate hike|rate cut|"
        r"fed funds|federal funds",
    ),
    ("GDP", r"\bgdp\b|gross domestic product"),
    ("Treasury", r"treasury|yield|t-bill|auction"),
    ("inflation", r"inflation|pce price|pce inflation|core pce|ppi\b"),
)

CSV_FIELDS = [
    "ticker",
    "market_title",
    "market_status",
    "event_key",
    "link_category",
    "link_confidence",
    "link_reason",
    "pattern",
    "supported_pattern",
    "official_source_family",
    "source_options",
    "paid_deferred_sources",
    "event_sources",
    "feature_present",
    "has_economic_market_leg",
    "missing_parsed_legs",
    "parser_preview_legs",
    "parser_preview_categories",
    "parser_min_confidence",
    "parser_ready",
    "forecast_safe_preview",
    "first_blocker",
    "proposed_market_type",
    "proposed_entity_name",
    "proposed_operator",
    "proposed_threshold_value",
    "proposed_unit",
    "would_write_market_leg_rows",
    "db_writes_performed",
]


def write_phase3bb_r4_economic_parser_backfill_report(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3bb_r4"),
    reports_dir: Path = Path("reports"),
    settings: Settings | None = None,
    command_args: list[str] | None = None,
) -> Phase3BBR4EconomicParserBackfillArtifacts:
    payload = build_phase3bb_r4_economic_parser_backfill(
        session,
        output_dir=output_dir,
        reports_dir=reports_dir,
        settings=settings,
        command_args=command_args,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    executive_summary_path = output_dir / "EXECUTIVE_SUMMARY.md"
    markdown_path = output_dir / "economic_parser_backfill.md"
    rows_csv_path = output_dir / "economic_parser_rows.csv"
    source_mapping_backlog_path = output_dir / "source_mapping_backlog.md"
    manifest_path = output_dir / "MANIFEST.sha256"

    executive_summary_path.write_text(_render_executive_summary(payload), encoding="utf-8")
    markdown_path.write_text(_render_backfill_markdown(payload), encoding="utf-8")
    _write_rows_csv(rows_csv_path, payload["economic_parser_rows"])
    source_mapping_backlog_path.write_text(
        _render_source_mapping_backlog(payload),
        encoding="utf-8",
    )
    _write_manifest(
        manifest_path,
        [executive_summary_path, markdown_path, rows_csv_path, source_mapping_backlog_path],
    )
    return Phase3BBR4EconomicParserBackfillArtifacts(
        output_dir=output_dir,
        executive_summary_path=executive_summary_path,
        markdown_path=markdown_path,
        rows_csv_path=rows_csv_path,
        source_mapping_backlog_path=source_mapping_backlog_path,
        manifest_path=manifest_path,
    )


def build_phase3bb_r4_economic_parser_backfill(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3bb_r4"),
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
        "command": "kalshi-bot phase3bb-r4-economic-parser-backfill",
        "argv": command_args or [],
    }
    economic_rows = economic_parser_preview_rows(session)
    pattern_rows = _pattern_rows(economic_rows)
    source_backlog_rows = _source_backlog_rows(economic_rows)
    summary = _summary(economic_rows, pattern_rows)
    safety = {
        **_safety_flags(),
        "paper_only": True,
        "diagnostic_only": True,
        "creates_paper_trades": False,
        "creates_paper_orders": False,
        "places_exchange_orders": False,
        "submits_cancels_replaces_orders": False,
        "fabricates_economic_data": False,
        "uses_fuzzy_market_matching": False,
        "tradingeconomics_deferred": True,
        "db_writes_performed": 0,
    }
    return {
        **metadata,
        "phase": "3BB-R4",
        "phase_version": PHASE3BB_R4_VERSION,
        "mode": "PAPER_READ_ONLY_ECONOMIC_PARSER_BACKFILL_PREVIEW",
        "reports_dir": str(reports_dir),
        "summary": summary,
        "pattern_rows": pattern_rows,
        "source_backlog_rows": source_backlog_rows,
        "economic_parser_rows": economic_rows,
        "acceptance": _acceptance(summary, economic_rows),
        "safety_flags": safety,
        "operator_guardrails": [
            "PAPER / READ-ONLY economic parser preview only.",
            "No fabricated economic data.",
            "No fuzzy market matching.",
            "No paper trades.",
            "No live/demo exchange orders.",
            "TradingEconomics remains deferred.",
            "No forecasts are run by this phase.",
        ],
    }


def classify_economic_pattern(text: str) -> str:
    normalized = _normalize_text(text)
    for label, pattern in PATTERN_RULES:
        if re.search(pattern, normalized):
            return label
    return "other"


def source_mapping_for_pattern(pattern: str) -> dict[str, Any]:
    mapping = SOURCE_MAPPING_BY_PATTERN.get(pattern, SOURCE_MAPPING_BY_PATTERN["other"])
    return {
        "official_source_family": mapping.official_source_family,
        "source_options": "; ".join(mapping.source_options),
        "paid_deferred_sources": "; ".join(mapping.paid_deferred_sources),
        "supported_pattern": mapping.supported,
    }


def parser_preview_status(
    *,
    market_active: bool,
    link_confidence: float,
    missing_parsed_legs: bool,
    supported_pattern: bool,
    parsed_economic_legs: list[ParsedMarketLeg],
    feature_present: bool,
) -> dict[str, Any]:
    min_confidence = _min_parser_confidence(parsed_economic_legs)
    if not market_active:
        blocker = "MARKET_NOT_ACTIVE"
    elif link_confidence < MIN_LINK_CONFIDENCE:
        blocker = "LINK_CONFIDENCE_TOO_LOW"
    elif not missing_parsed_legs:
        blocker = "ALREADY_PARSED"
    elif not supported_pattern:
        blocker = "UNSUPPORTED_PATTERN"
    elif not parsed_economic_legs:
        blocker = "PARSER_NO_ECONOMIC_LEG"
    elif min_confidence < MIN_PARSER_CONFIDENCE:
        blocker = "PARSER_CONFIDENCE_TOO_LOW"
    else:
        blocker = "PARSER_READY_READ_ONLY"
    parser_ready = blocker == "PARSER_READY_READ_ONLY"
    return {
        "first_blocker": blocker,
        "parser_ready": parser_ready,
        "forecast_safe_preview": bool(parser_ready and feature_present),
        "parser_min_confidence": min_confidence,
    }


def economic_parser_preview_rows(session: Session) -> list[dict[str, Any]]:
    market_links = _active_linked_economic_markets(session)
    tickers = [market.ticker for market, _link in market_links]
    event_keys = [link.event_key for _market, link in market_links]
    existing_leg_tickers = _existing_economic_leg_tickers(session, tickers)
    event_sources = _event_sources_by_key(session, event_keys)
    feature_keys = _feature_event_keys(session, event_keys)
    rows: list[dict[str, Any]] = []
    for market, link in market_links:
        title = _market_text(market)
        pattern = classify_economic_pattern(title)
        source_mapping = source_mapping_for_pattern(pattern)
        parsed_economic_legs = [
            leg for leg in parse_market_legs(market) if leg.category == CATEGORY_ECONOMIC
        ]
        link_confidence = _to_float(link.confidence)
        missing_parsed_legs = market.ticker not in existing_leg_tickers
        feature_present = link.event_key in feature_keys
        status = parser_preview_status(
            market_active=_is_active_status(market.status),
            link_confidence=link_confidence,
            missing_parsed_legs=missing_parsed_legs,
            supported_pattern=bool(source_mapping["supported_pattern"]),
            parsed_economic_legs=parsed_economic_legs,
            feature_present=feature_present,
        )
        primary_leg = parsed_economic_legs[0] if parsed_economic_legs else None
        would_write = len(parsed_economic_legs) if status["parser_ready"] else 0
        rows.append(
            {
                "ticker": market.ticker,
                "market_title": market.title or "",
                "market_status": market.status or "",
                "event_key": link.event_key,
                "link_category": link.category,
                "link_confidence": link.confidence,
                "link_reason": link.reason,
                "pattern": pattern,
                "supported_pattern": source_mapping["supported_pattern"],
                "official_source_family": source_mapping["official_source_family"],
                "source_options": source_mapping["source_options"],
                "paid_deferred_sources": source_mapping["paid_deferred_sources"],
                "event_sources": "; ".join(sorted(event_sources.get(link.event_key, set()))),
                "feature_present": feature_present,
                "has_economic_market_leg": not missing_parsed_legs,
                "missing_parsed_legs": missing_parsed_legs,
                "parser_preview_legs": len(parsed_economic_legs),
                "parser_preview_categories": "; ".join(
                    sorted({leg.category for leg in parsed_economic_legs})
                ),
                "parser_min_confidence": status["parser_min_confidence"],
                "parser_ready": status["parser_ready"],
                "forecast_safe_preview": status["forecast_safe_preview"],
                "first_blocker": status["first_blocker"],
                "proposed_market_type": primary_leg.market_type if primary_leg else "",
                "proposed_entity_name": primary_leg.entity_name if primary_leg else "",
                "proposed_operator": primary_leg.operator if primary_leg else "",
                "proposed_threshold_value": primary_leg.threshold_value if primary_leg else "",
                "proposed_unit": primary_leg.unit if primary_leg else "",
                "would_write_market_leg_rows": would_write,
                "db_writes_performed": 0,
            }
        )
    return sorted(rows, key=lambda row: (str(row["first_blocker"]), str(row["ticker"])))


def _active_linked_economic_markets(session: Session) -> list[tuple[Market, EconomicMarketLink]]:
    rows = session.execute(
        select(Market, EconomicMarketLink)
        .join(EconomicMarketLink, EconomicMarketLink.ticker == Market.ticker)
        .where(func.lower(func.coalesce(Market.status, "")).in_(ACTIVE_MARKET_STATUSES))
        .order_by(Market.ticker, EconomicMarketLink.detected_at.desc())
    ).all()
    by_ticker: dict[str, tuple[Market, EconomicMarketLink]] = {}
    for market, link in rows:
        existing = by_ticker.get(market.ticker)
        if existing is None or _link_sort_key(link) > _link_sort_key(existing[1]):
            by_ticker[market.ticker] = (market, link)
    return [by_ticker[ticker] for ticker in sorted(by_ticker)]


def _link_sort_key(link: EconomicMarketLink) -> tuple[float, str]:
    return (_to_float(link.confidence), str(link.detected_at or ""))


def _existing_economic_leg_tickers(session: Session, tickers: list[str]) -> set[str]:
    if not tickers:
        return set()
    return set(
        session.scalars(
            select(MarketLeg.ticker)
            .where(MarketLeg.category == CATEGORY_ECONOMIC, MarketLeg.ticker.in_(tickers))
            .distinct()
        )
    )


def _event_sources_by_key(session: Session, event_keys: list[str]) -> dict[str, set[str]]:
    if not event_keys:
        return {}
    mapping: dict[str, set[str]] = defaultdict(set)
    rows = session.execute(
        select(EconomicEvent.event_key, EconomicEvent.source)
        .where(EconomicEvent.event_key.in_(event_keys))
        .distinct()
    )
    for event_key, source in rows:
        mapping[str(event_key)].add(str(source))
    return mapping


def _feature_event_keys(session: Session, event_keys: list[str]) -> set[str]:
    if not event_keys:
        return set()
    return set(
        session.scalars(
            select(EconomicFeature.event_key)
            .where(EconomicFeature.event_key.in_(event_keys))
            .distinct()
        )
    )


def _pattern_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row["pattern"])].append(row)
    pattern_rows = []
    for pattern in sorted(SOURCE_MAPPING_BY_PATTERN):
        group = grouped.get(pattern, [])
        if not group and pattern == "other":
            continue
        mapping = source_mapping_for_pattern(pattern)
        pattern_rows.append(
            {
                "pattern": pattern,
                "official_source_family": mapping["official_source_family"],
                "active_linked_rows": len(group),
                "missing_parsed_leg_rows": sum(
                    _truthy(row["missing_parsed_legs"]) for row in group
                ),
                "supported_rows": sum(_truthy(row["supported_pattern"]) for row in group),
                "parser_ready_rows": sum(_truthy(row["parser_ready"]) for row in group),
                "unsupported_rows": sum(not _truthy(row["supported_pattern"]) for row in group),
                "feature_present_rows": sum(_truthy(row["feature_present"]) for row in group),
                "forecast_safe_preview_rows": sum(
                    _truthy(row["forecast_safe_preview"]) for row in group
                ),
                "first_blockers": _counter_string(row["first_blocker"] for row in group),
            }
        )
    return pattern_rows


def _source_backlog_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        key = (str(row["pattern"]), str(row["official_source_family"]))
        grouped[key].append(row)
    backlog = []
    for (pattern, source_family), group in sorted(grouped.items()):
        mapping = source_mapping_for_pattern(pattern)
        parser_ready = sum(_truthy(row["parser_ready"]) for row in group)
        feature_present = sum(_truthy(row["feature_present"]) for row in group)
        unsupported = sum(not _truthy(row["supported_pattern"]) for row in group)
        backlog.append(
            {
                "pattern": pattern,
                "source_family": source_family,
                "official_sources": mapping["source_options"],
                "candidate_rows": len(group),
                "parser_ready_rows": parser_ready,
                "unsupported_rows": unsupported,
                "features_present_rows": feature_present,
                "forecast_safe_preview_rows": sum(
                    _truthy(row["forecast_safe_preview"]) for row in group
                ),
                "backlog_action": _source_backlog_action(
                    pattern=pattern,
                    parser_ready_rows=parser_ready,
                    features_present_rows=feature_present,
                    unsupported_rows=unsupported,
                ),
            }
        )
    return backlog


def _source_backlog_action(
    *,
    pattern: str,
    parser_ready_rows: int,
    features_present_rows: int,
    unsupported_rows: int,
) -> str:
    if unsupported_rows:
        return "Keep diagnostic-only until an exact official source/parser rule is added."
    if parser_ready_rows:
        return "Eligible for a later writer-gated market-leg apply preview."
    if features_present_rows:
        return "Parser evidence incomplete; keep forecasts gated."
    if pattern == "other":
        return "Add a supported economic pattern rule before parser backfill."
    return "Collect or verify official source evidence before forecast activation."


def _summary(rows: list[dict[str, Any]], pattern_rows: list[dict[str, Any]]) -> dict[str, Any]:
    blockers = Counter(str(row["first_blocker"]) for row in rows)
    return {
        "active_economic_markets": len(rows),
        "exact_linked_active_rows": len(rows),
        "missing_parsed_leg_rows": sum(_truthy(row["missing_parsed_legs"]) for row in rows),
        "supported_pattern_rows": sum(_truthy(row["supported_pattern"]) for row in rows),
        "parser_ready_rows": sum(_truthy(row["parser_ready"]) for row in rows),
        "unsupported_pattern_rows": sum(not _truthy(row["supported_pattern"]) for row in rows),
        "forecast_safe_preview_rows": sum(_truthy(row["forecast_safe_preview"]) for row in rows),
        "db_writes_performed": 0,
        "paper_trades_created": 0,
        "tradingeconomics_deferred": True,
        "first_blocker": _dominant_blocker(blockers),
        "pattern_counts": {row["pattern"]: row["active_linked_rows"] for row in pattern_rows},
        "next_operator_command": _next_operator_command(rows),
    }


def _acceptance(summary: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "economic_rows_no_longer_generic_no_parsed_markets": bool(rows),
        "supported_rows_become_parser_ready": (
            int(summary["supported_pattern_rows"]) == 0 or int(summary["parser_ready_rows"]) > 0
        ),
        "unsupported_rows_are_explicit": all(
            _truthy(row["supported_pattern"]) or row["first_blocker"] == "UNSUPPORTED_PATTERN"
            for row in rows
        ),
        "no_paper_live_demo_orders": True,
        "tradingeconomics_deferred": bool(summary["tradingeconomics_deferred"]),
        "db_writes_performed_zero": int(summary["db_writes_performed"]) == 0,
    }


def _next_operator_command(rows: list[dict[str, Any]]) -> str:
    if any(_truthy(row["parser_ready"]) for row in rows):
        return (
            "Next Codex step: add a writer-gated economic market-leg apply command "
            "for rows where parser_ready=true, with dry-run default and "
            "--apply --backup-first only after db-writer-monitor is clear."
        )
    return (
        "Next Codex step: expand exact economic parser/source rules for rows marked "
        "UNSUPPORTED_PATTERN or PARSER_NO_ECONOMIC_LEG; do not forecast yet."
    )


def _dominant_blocker(blockers: Counter[str]) -> str:
    if not blockers:
        return "NO_ACTIVE_EXACT_LINKED_ECONOMIC_ROWS"
    return blockers.most_common(1)[0][0]


def _render_executive_summary(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    lines = _metadata_lines(payload, "# Phase 3BB-R4 Economic Parser Backfill")
    lines.extend(
        [
            "",
            "## Status",
            "",
            f"- Active exact-linked economic rows: `{summary['exact_linked_active_rows']}`",
            f"- Missing parsed-leg rows: `{summary['missing_parsed_leg_rows']}`",
            f"- Supported pattern rows: `{summary['supported_pattern_rows']}`",
            f"- Parser-ready rows: `{summary['parser_ready_rows']}`",
            f"- Unsupported pattern rows: `{summary['unsupported_pattern_rows']}`",
            f"- Forecast-safe preview rows: `{summary['forecast_safe_preview_rows']}`",
            f"- First blocker: `{summary['first_blocker']}`",
            f"- DB writes performed: `{summary['db_writes_performed']}`",
            f"- Paper trades created: `{summary['paper_trades_created']}`",
            "- TradingEconomics: `DEFERRED`",
            "",
            "## Acceptance",
            "",
        ]
    )
    for key, value in payload["acceptance"].items():
        lines.append(f"- {key}: `{value}`")
    lines.extend(["", "## Next", "", summary["next_operator_command"], ""])
    return "\n".join(lines)


def _render_backfill_markdown(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# Economic Parser Backfill Preview")
    lines.extend(
        [
            "",
            "## Pattern Summary",
            "",
            (
                "| Pattern | Source family | Active linked | Missing parsed | Supported | "
                "Parser-ready | Unsupported | Feature present | Forecast-safe preview | Blockers |"
            ),
            "|---|---|---:|---:|---:|---:|---:|---:|---:|---|",
        ]
    )
    for row in payload["pattern_rows"]:
        lines.append(
            "| {pattern} | {official_source_family} | {active_linked_rows} | "
            "{missing_parsed_leg_rows} | {supported_rows} | {parser_ready_rows} | "
            "{unsupported_rows} | {feature_present_rows} | {forecast_safe_preview_rows} | "
            "{first_blockers} |".format(**row)
        )
    lines.extend(
        [
            "",
            "## Sample Rows",
            "",
            "| Ticker | Pattern | Source | Parser-ready | First blocker | Preview legs |",
            "|---|---|---|---:|---|---:|",
        ]
    )
    for row in payload["economic_parser_rows"][:30]:
        lines.append(
            "| {ticker} | {pattern} | {official_source_family} | {parser_ready} | "
            "{first_blocker} | {parser_preview_legs} |".format(**row)
        )
    lines.extend(
        [
            "",
            "## Guardrails",
            "",
        ]
    )
    for guardrail in payload["operator_guardrails"]:
        lines.append(f"- {guardrail}")
    lines.append("")
    return "\n".join(lines)


def _render_source_mapping_backlog(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# Economic Source Mapping Backlog")
    lines.extend(
        [
            "",
            "TradingEconomics remains DEFERRED. Only free official source families are mapped.",
            "",
            (
                "| Pattern | Source family | Official sources | Candidates | Parser-ready | "
                "Unsupported | Feature present | Forecast-safe preview | Action |"
            ),
            "|---|---|---|---:|---:|---:|---:|---:|---|",
        ]
    )
    for row in payload["source_backlog_rows"]:
        lines.append(
            "| {pattern} | {source_family} | {official_sources} | {candidate_rows} | "
            "{parser_ready_rows} | {unsupported_rows} | {features_present_rows} | "
            "{forecast_safe_preview_rows} | {backlog_action} |".format(**row)
        )
    lines.append("")
    return "\n".join(lines)


def _write_rows_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _market_text(market: Market) -> str:
    return " ".join(
        part
        for part in (
            market.ticker,
            market.event_ticker,
            market.series_ticker,
            market.title,
            market.subtitle,
            market.rules_primary,
            market.rules_secondary,
        )
        if part
    )


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower()).strip()


def _min_parser_confidence(legs: list[ParsedMarketLeg]) -> float:
    if not legs:
        return 0.0
    return min(_to_float(leg.confidence) for leg in legs)


def _is_active_status(status: str | None) -> bool:
    return str(status or "").strip().lower() in ACTIVE_MARKET_STATUSES


def _to_float(value: Any) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return 0.0


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def _counter_string(values: Any) -> str:
    counter = Counter(str(value) for value in values if str(value))
    if not counter:
        return ""
    return "; ".join(f"{key}={count}" for key, count in counter.most_common())
