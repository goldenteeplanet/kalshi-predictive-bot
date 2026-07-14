from __future__ import annotations

import csv
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.data.schema import Market, MarketLeg, NewsItem, NewsMarketLink
from kalshi_predictor.phase3bb_acceleration import (
    _metadata,
    _metadata_lines,
    _safety_flags,
    _write_manifest,
)
from kalshi_predictor.utils.time import utc_now

PHASE3BB_R7_VERSION = "phase3bb_r7_news_event_discovery_v1"
ACTIVE_MARKET_STATUSES = ("active", "open")
DEFAULT_OUTPUT_DIR = Path("reports/phase3bb_r7")
DEFAULT_REPORTS_DIR = Path("reports")
R7_COMMAND = (
    "kalshi-bot phase3bb-r7-news-event-discovery "
    "--output-dir reports/phase3bb_r7 --reports-dir reports --limit 2000"
)

NEWS_EVENT_KEYWORDS = (
    "announce",
    "announcement",
    "approval",
    "approve",
    "ban",
    "bill",
    "breaking",
    "ceasefire",
    "congress",
    "court",
    "election",
    "executive order",
    "fda",
    "headline",
    "law",
    "merger",
    "news",
    "ofac",
    "resign",
    "ruling",
    "sanction",
    "sec",
    "strike",
    "supreme court",
    "tariff",
    "trial",
    "vote",
    "white house",
)

UNSUPPORTED_DOMAIN_PATTERNS = (
    ("SPORTS_ENGINE", r"\b(mlb|nfl|nba|nhl|wnba|epl|soccer|basketball|baseball)\b"),
    ("WEATHER_ENGINE", r"\b(weather|temperature|rain|snow|hurricane|wind)\b"),
    ("ECONOMIC_ENGINE", r"\b(cpi|payroll|unemployment|gdp|fomc|fed funds)\b"),
)

SOURCE_RULES: tuple[dict[str, Any], ...] = (
    {
        "family": "official_court",
        "pattern": r"\b(supreme court|scotus|court|ruling|trial)\b",
        "official": ("Supreme Court opinions/orders", "CourtListener/official docket review"),
        "parser": "legal_event_parser",
    },
    {
        "family": "official_election",
        "pattern": r"\b(election|vote|ballot|senate|house|president|governor)\b",
        "official": ("state election boards", "FEC", "official election result pages"),
        "parser": "election_event_parser",
    },
    {
        "family": "official_regulatory",
        "pattern": r"\b(fda|sec|approval|approve|etf|drug)\b",
        "official": ("FDA announcements", "SEC releases", "EDGAR/company filings"),
        "parser": "regulatory_event_parser",
    },
    {
        "family": "official_us_policy",
        "pattern": r"\b(white house|executive order|congress|bill|law|tariff)\b",
        "official": ("whitehouse.gov", "congress.gov", "USTR official releases"),
        "parser": "policy_event_parser",
    },
    {
        "family": "official_sanctions_geopolitical",
        "pattern": r"\b(sanction|ofac|ceasefire|war|nato|united nations|strike)\b",
        "official": ("Treasury OFAC", "State Department", "UN/NATO official releases"),
        "parser": "geopolitical_event_parser",
    },
    {
        "family": "official_company_event",
        "pattern": r"\b(merger|acquisition|ipo|bankruptcy|resign|ceo)\b",
        "official": ("SEC EDGAR", "company investor relations", "official press release"),
        "parser": "company_event_parser",
    },
)

CANDIDATE_FIELDS = [
    "ticker",
    "title",
    "status",
    "series_ticker",
    "event_ticker",
    "candidate_bucket",
    "source_status",
    "parser_status",
    "source_family",
    "official_source_candidates",
    "rss_source_candidates",
    "existing_news_links",
    "existing_source_urls",
    "parsed_news_legs",
    "parse_ready",
    "source_ready",
    "needs_review",
    "unsupported",
    "ambiguous",
    "first_blocker",
    "next_parser_source_work",
    "forecast_allowed_by_this_phase",
    "paper_trade_creation",
    "live_or_demo_execution",
]

BACKLOG_FIELDS = [
    "source_family",
    "parser_recommendation",
    "row_count",
    "source_ready_rows",
    "parse_ready_rows",
    "needs_review_rows",
    "unsupported_rows",
    "official_source_candidates",
    "example_tickers",
    "backlog_action",
    "forecast_safe_when",
]


@dataclass(frozen=True)
class Phase3BBR7NewsEventDiscoveryArtifacts:
    output_dir: Path
    executive_summary_path: Path
    markdown_path: Path
    candidates_csv_path: Path
    source_backlog_csv_path: Path
    manifest_path: Path


def write_phase3bb_r7_news_event_discovery_report(
    session: Session,
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    reports_dir: Path = DEFAULT_REPORTS_DIR,
    settings: Settings | None = None,
    command_args: list[str] | None = None,
    limit: int = 2000,
) -> Phase3BBR7NewsEventDiscoveryArtifacts:
    payload = build_phase3bb_r7_news_event_discovery(
        session,
        output_dir=output_dir,
        reports_dir=reports_dir,
        settings=settings,
        command_args=command_args,
        limit=limit,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    executive_summary_path = output_dir / "EXECUTIVE_SUMMARY.md"
    markdown_path = output_dir / "news_event_discovery.md"
    candidates_csv_path = output_dir / "news_event_candidates.csv"
    source_backlog_csv_path = output_dir / "source_backlog.csv"
    manifest_path = output_dir / "MANIFEST.sha256"

    executive_summary_path.write_text(_render_executive_summary(payload), encoding="utf-8")
    markdown_path.write_text(_render_markdown(payload), encoding="utf-8")
    _write_csv(candidates_csv_path, payload["news_event_candidates"], CANDIDATE_FIELDS)
    _write_csv(source_backlog_csv_path, payload["source_backlog"], BACKLOG_FIELDS)
    _write_manifest(
        manifest_path,
        [executive_summary_path, markdown_path, candidates_csv_path, source_backlog_csv_path],
    )
    return Phase3BBR7NewsEventDiscoveryArtifacts(
        output_dir=output_dir,
        executive_summary_path=executive_summary_path,
        markdown_path=markdown_path,
        candidates_csv_path=candidates_csv_path,
        source_backlog_csv_path=source_backlog_csv_path,
        manifest_path=manifest_path,
    )


def build_phase3bb_r7_news_event_discovery(
    session: Session,
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    reports_dir: Path = DEFAULT_REPORTS_DIR,
    settings: Settings | None = None,
    command_args: list[str] | None = None,
    limit: int = 2000,
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
        "command": "kalshi-bot phase3bb-r7-news-event-discovery",
        "argv": command_args or [],
    }
    markets = _active_news_event_markets(session, limit=limit)
    rows = [_candidate_row(market, _news_context(session, market.ticker)) for market in markets]
    backlog = _source_backlog(rows)
    summary = _summary(rows, backlog, limit=limit)
    safety = {
        **_safety_flags(),
        "paper_only": True,
        "diagnostic_only": True,
        "creates_paper_trades": False,
        "creates_paper_orders": False,
        "places_exchange_orders": False,
        "submits_cancels_replaces_orders": False,
        "fabricates_news_events": False,
        "headline_only_forecasting": False,
        "uses_fuzzy_event_matching": False,
        "forecast_writes": False,
        "db_writes_performed": 0,
    }
    return {
        **metadata,
        "phase": "3BB-R7",
        "phase_version": PHASE3BB_R7_VERSION,
        "mode": "PAPER_READ_ONLY_NEWS_EVENT_DISCOVERY",
        "reports_dir": str(reports_dir),
        "limit": limit,
        "summary": summary,
        "news_event_candidates": rows,
        "source_backlog": backlog,
        "acceptance": _acceptance(summary),
        "safety_flags": safety,
        "operator_guardrails": [
            "PAPER / READ-ONLY discovery only.",
            "Do not fabricate news events.",
            "Do not create forecasts from unverified headlines.",
            "Do not create paper trades.",
            "Do not use fuzzy event matching.",
        ],
        "next_actions": _next_actions(summary),
    }


def _active_news_event_markets(session: Session, *, limit: int) -> list[Market]:
    linked = set(session.scalars(select(NewsMarketLink.ticker).distinct()))
    parsed = set(
        session.scalars(
            select(MarketLeg.ticker).where(MarketLeg.category == "news").distinct()
        )
    )
    text_conditions = [
        func.lower(Market.title).like(f"%{keyword}%") for keyword in NEWS_EVENT_KEYWORDS
    ]
    keyword_rows = session.scalars(
        select(Market)
        .where(func.lower(Market.status).in_(ACTIVE_MARKET_STATUSES))
        .where(or_(*text_conditions))
        .order_by(Market.last_seen_at.desc())
        .limit(max(limit * 5, 1000))
    )
    keyword_tickers = {
        market.ticker
        for market in keyword_rows
        if _has_news_event_keyword(market.title or market.subtitle or "")
    }
    tickers = keyword_tickers | linked | parsed
    if not tickers:
        return []
    rows = session.scalars(
        select(Market)
        .where(Market.ticker.in_(sorted(tickers)))
        .where(func.lower(Market.status).in_(ACTIVE_MARKET_STATUSES))
        .order_by(Market.last_seen_at.desc(), Market.ticker)
        .limit(max(limit, 1))
    )
    return list(rows)


def _has_news_event_keyword(text: str) -> bool:
    normalized = text.lower()
    return any(
        re.search(rf"(?<![a-z0-9]){re.escape(keyword)}(?![a-z0-9])", normalized)
        for keyword in NEWS_EVENT_KEYWORDS
    )


def _news_context(session: Session, ticker: str) -> dict[str, Any]:
    links = list(
        session.execute(
            select(NewsMarketLink, NewsItem)
            .join(NewsItem, NewsItem.id == NewsMarketLink.news_item_id)
            .where(NewsMarketLink.ticker == ticker)
            .order_by(NewsMarketLink.created_at.desc())
        )
    )
    legs = list(
        session.scalars(
            select(MarketLeg)
            .where(MarketLeg.ticker == ticker, MarketLeg.category == "news")
            .order_by(MarketLeg.leg_index)
        )
    )
    return {"links": links, "legs": legs}


def _candidate_row(market: Market, context: dict[str, Any]) -> dict[str, Any]:
    title = market.title or market.subtitle or market.ticker
    text = f"{market.ticker} {market.series_ticker or ''} {title}"
    rule = _source_rule(text)
    unsupported = _unsupported_reason(text)
    links = context["links"]
    legs = context["legs"]
    source_urls = _source_urls(links)
    source_status = _source_status(source_urls, links, unsupported)
    source_ready = source_status in {"OFFICIAL_SOURCE_BACKED", "RSS_SOURCE_PAGE_BACKED"}
    ambiguous = (rule is None or rule.get("family") == "ambiguous_multi_source") and not unsupported
    parse_ready = bool(legs) and source_ready and not ambiguous and not unsupported
    needs_review = not parse_ready and not unsupported
    parser_status = _parser_status(parse_ready, needs_review, unsupported, bool(legs))
    bucket = _candidate_bucket(parse_ready, source_ready, needs_review, unsupported, ambiguous)
    first_blocker = _first_blocker(
        parse_ready=parse_ready,
        source_ready=source_ready,
        unsupported=unsupported,
        ambiguous=ambiguous,
        has_legs=bool(legs),
    )
    return {
        "ticker": market.ticker,
        "title": title,
        "status": market.status or "",
        "series_ticker": market.series_ticker or "",
        "event_ticker": market.event_ticker or "",
        "candidate_bucket": bucket,
        "source_status": source_status,
        "parser_status": parser_status,
        "source_family": rule["family"] if rule else (unsupported or "unknown"),
        "official_source_candidates": _official_sources(rule),
        "rss_source_candidates": _rss_candidates(source_urls),
        "existing_news_links": len(links),
        "existing_source_urls": "; ".join(source_urls),
        "parsed_news_legs": len(legs),
        "parse_ready": parse_ready,
        "source_ready": source_ready,
        "needs_review": needs_review,
        "unsupported": bool(unsupported),
        "ambiguous": ambiguous,
        "first_blocker": first_blocker,
        "next_parser_source_work": _next_parser_work(rule, first_blocker),
        "forecast_allowed_by_this_phase": False,
        "paper_trade_creation": False,
        "live_or_demo_execution": False,
    }


def _source_rule(text: str) -> dict[str, Any] | None:
    normalized = text.lower()
    matches = [rule for rule in SOURCE_RULES if re.search(rule["pattern"], normalized)]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        families = ", ".join(rule["family"] for rule in matches)
        return {
            "family": "ambiguous_multi_source",
            "official": tuple(
                source for rule in matches for source in tuple(rule["official"])
            ),
            "parser": f"manual_source_family_review:{families}",
        }
    return None


def _unsupported_reason(text: str) -> str | None:
    upper = text.upper()
    if "KXMVECROSSCATEGORY" in upper or "KXMVESPORTSMULTIGAME" in upper:
        return "UNSUPPORTED_COMPOSITE_OR_SPORTS_ENGINE"
    normalized = text.lower()
    for label, pattern in UNSUPPORTED_DOMAIN_PATTERNS:
        if re.search(pattern, normalized):
            return label
    return None


def _source_urls(links: list[Any]) -> list[str]:
    urls: list[str] = []
    for _link, item in links:
        url = str(item.source_url or "").strip()
        if url and url not in urls:
            urls.append(url)
    return urls


def _source_status(source_urls: list[str], links: list[Any], unsupported: str | None) -> str:
    if unsupported:
        return "UNSUPPORTED"
    if any(_official_url(url) for url in source_urls):
        return "OFFICIAL_SOURCE_BACKED"
    if source_urls or links:
        return "RSS_SOURCE_PAGE_BACKED"
    return "NO_SOURCE_FOUND"


def _official_url(url: str) -> bool:
    lowered = url.lower()
    official_markers = (
        ".gov",
        "congress.gov",
        "supremecourt.gov",
        "sec.gov",
        "fda.gov",
        "whitehouse.gov",
        "treasury.gov",
        "ustr.gov",
        "un.org",
        "nato.int",
    )
    return any(marker in lowered for marker in official_markers)


def _candidate_bucket(
    parse_ready: bool,
    source_ready: bool,
    needs_review: bool,
    unsupported: str | None,
    ambiguous: bool,
) -> str:
    if unsupported:
        return "UNSUPPORTED"
    if parse_ready:
        return "PARSE_READY"
    if source_ready:
        return "SOURCE_READY"
    if ambiguous:
        return "AMBIGUOUS"
    if needs_review:
        return "NEEDS_REVIEW"
    return "NO_SOURCE_FOUND"


def _parser_status(
    parse_ready: bool,
    needs_review: bool,
    unsupported: str | None,
    has_legs: bool,
) -> str:
    if unsupported:
        return "UNSUPPORTED"
    if parse_ready:
        return "NEWS_EVENT_PARSER_READY"
    if has_legs:
        return "PARSED_NEWS_LEG_NEEDS_SOURCE"
    if needs_review:
        return "PARSER_BACKLOG"
    return "NO_NEWS_EVENT_PARSER"


def _first_blocker(
    *,
    parse_ready: bool,
    source_ready: bool,
    unsupported: str | None,
    ambiguous: bool,
    has_legs: bool,
) -> str:
    if parse_ready:
        return "DISCOVERY_ONLY_FORECASTS_BLOCKED"
    if unsupported:
        return unsupported
    if ambiguous:
        return "AMBIGUOUS_EVENT_SOURCE_MAPPING"
    if not source_ready:
        return "SOURCE_MISSING"
    if not has_legs:
        return "PARSER_BACKFILL_REQUIRED"
    return "REVIEW_REQUIRED"


def _official_sources(rule: dict[str, Any] | None) -> str:
    if not rule:
        return ""
    return "; ".join(str(item) for item in rule.get("official") or ())


def _rss_candidates(source_urls: list[str]) -> str:
    return "; ".join(source_urls)


def _next_parser_work(rule: dict[str, Any] | None, first_blocker: str) -> str:
    if first_blocker == "DISCOVERY_ONLY_FORECASTS_BLOCKED":
        return "operator review: source/event mapping exact before any later forecast phase"
    if first_blocker == "SOURCE_MISSING":
        if rule:
            return f"collect exact official source evidence for {rule['family']}"
        return "add deterministic source family rule or mark unsupported"
    if first_blocker == "PARSER_BACKFILL_REQUIRED":
        parser = rule["parser"] if rule else "news_event_parser"
        return f"build parser preview for {parser}"
    if first_blocker == "AMBIGUOUS_EVENT_SOURCE_MAPPING":
        return "manual official-source family split before parser work"
    return "keep blocked; not a news/event engine row"


def _source_backlog(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not rows:
        return [
            {
                "source_family": "news_event_market_scan",
                "parser_recommendation": "news_event_market_discovery",
                "row_count": 0,
                "source_ready_rows": 0,
                "parse_ready_rows": 0,
                "needs_review_rows": 0,
                "unsupported_rows": 0,
                "official_source_candidates": "",
                "example_tickers": "",
                "backlog_action": "refresh active market catalog and rerun discovery",
                "forecast_safe_when": "exact source and event mapping exists",
            }
        ]
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row["source_family"])].append(row)
    backlog: list[dict[str, Any]] = []
    for family, group in sorted(grouped.items()):
        source_ready = sum(1 for row in group if _truthy(row["source_ready"]))
        parse_ready = sum(1 for row in group if _truthy(row["parse_ready"]))
        unsupported = sum(1 for row in group if _truthy(row["unsupported"]))
        needs_review = sum(1 for row in group if _truthy(row["needs_review"]))
        source_options = sorted(
            {
                option.strip()
                for row in group
                for option in str(row["official_source_candidates"]).split(";")
                if option.strip()
            }
        )
        backlog.append(
            {
                "source_family": family,
                "parser_recommendation": _backlog_parser(group),
                "row_count": len(group),
                "source_ready_rows": source_ready,
                "parse_ready_rows": parse_ready,
                "needs_review_rows": needs_review,
                "unsupported_rows": unsupported,
                "official_source_candidates": "; ".join(source_options),
                "example_tickers": "; ".join(row["ticker"] for row in group[:5]),
                "backlog_action": _backlog_action(group),
                "forecast_safe_when": "source URL and event/parser mapping are exact",
            }
        )
    return backlog


def _backlog_parser(group: list[dict[str, Any]]) -> str:
    work = [row["next_parser_source_work"] for row in group if row["next_parser_source_work"]]
    if not work:
        return "manual_review"
    return Counter(work).most_common(1)[0][0]


def _backlog_action(group: list[dict[str, Any]]) -> str:
    if any(row["candidate_bucket"] == "PARSE_READY" for row in group):
        return "review parse-ready rows; keep forecasts blocked in discovery"
    if any(row["source_status"] == "NO_SOURCE_FOUND" for row in group):
        return "collect official source/RSS evidence and rerun discovery"
    if any(row["candidate_bucket"] == "UNSUPPORTED" for row in group):
        return "route to the proper category engine or keep unsupported"
    return "manual source family review"


def _summary(
    rows: list[dict[str, Any]],
    backlog: list[dict[str, Any]],
    *,
    limit: int,
) -> dict[str, Any]:
    bucket_counts = Counter(row["candidate_bucket"] for row in rows)
    source_counts = Counter(row["source_status"] for row in rows)
    parse_ready = sum(1 for row in rows if _truthy(row["parse_ready"]))
    source_ready = sum(1 for row in rows if _truthy(row["source_ready"]))
    needs_review = sum(1 for row in rows if _truthy(row["needs_review"]))
    unsupported = sum(1 for row in rows if _truthy(row["unsupported"]))
    first_blocker = _summary_blocker(rows)
    return {
        "status": "NEWS_EVENT_DISCOVERY_READY",
        "active_news_event_candidates": len(rows),
        "scan_limit": limit,
        "official_source_backed": source_counts["OFFICIAL_SOURCE_BACKED"],
        "rss_source_page_backed": source_counts["RSS_SOURCE_PAGE_BACKED"],
        "no_source_found": source_counts["NO_SOURCE_FOUND"],
        "unsupported": unsupported,
        "ambiguous": bucket_counts["AMBIGUOUS"],
        "parse_ready": parse_ready,
        "source_ready": source_ready,
        "needs_review": needs_review,
        "parser_backlog_rows": max(needs_review, 0),
        "source_backlog_families": len(backlog),
        "first_hard_blocker": first_blocker,
        "forecasts_created": 0,
        "paper_trades_created": 0,
        "live_or_demo_execution": False,
        "db_writes_performed": 0,
    }


def _summary_blocker(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "NO_ACTIVE_NEWS_EVENT_CANDIDATES"
    counts = Counter(row["first_blocker"] for row in rows)
    for blocker in (
        "SOURCE_MISSING",
        "PARSER_BACKFILL_REQUIRED",
        "AMBIGUOUS_EVENT_SOURCE_MAPPING",
        "DISCOVERY_ONLY_FORECASTS_BLOCKED",
    ):
        if counts.get(blocker):
            return blocker
    return counts.most_common(1)[0][0]


def _acceptance(summary: dict[str, Any]) -> dict[str, bool]:
    return {
        "news_explained_beyond_no_parsed_markets": True,
        "exact_next_parser_source_work_listed": _to_int(summary["source_backlog_families"]) > 0,
        "no_paper_orders": _to_int(summary["paper_trades_created"]) == 0,
        "no_live_demo_orders": summary["live_or_demo_execution"] is False,
        "db_writes_zero": _to_int(summary["db_writes_performed"]) == 0,
    }


def _next_actions(summary: dict[str, Any]) -> list[str]:
    if summary["first_hard_blocker"] == "NO_ACTIVE_NEWS_EVENT_CANDIDATES":
        return [
            "kalshi-bot sync-markets --status open --limit 100 --max-pages 5",
            R7_COMMAND,
        ]
    return [
        R7_COMMAND,
        "# do not run news forecasts until exact source/event mapping is reviewed",
    ]


def _render_executive_summary(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    lines = _metadata_lines(payload, "# Phase 3BB-R7 News And Event Market Discovery")
    lines.extend(
        [
            "",
            "## Status",
            "",
            f"- Status: `{summary['status']}`",
            f"- Active news/event candidates: `{summary['active_news_event_candidates']}`",
            f"- Official-source-backed: `{summary['official_source_backed']}`",
            f"- RSS/source-page-backed: `{summary['rss_source_page_backed']}`",
            f"- No source found: `{summary['no_source_found']}`",
            f"- Parse-ready: `{summary['parse_ready']}`",
            f"- Source-ready: `{summary['source_ready']}`",
            f"- Needs review: `{summary['needs_review']}`",
            f"- Unsupported: `{summary['unsupported']}`",
            f"- First hard blocker: `{summary['first_hard_blocker']}`",
            "",
            "No forecasts, paper trades, live/demo exchange writes, or DB writes were run.",
            "",
        ]
    )
    return "\n".join(lines)


def _render_markdown(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    lines = _metadata_lines(payload, "# Phase 3BB-R7 News/Event Discovery")
    lines.extend(
        [
            "",
            "## Funnel",
            "",
            f"- Candidates scanned: `{summary['active_news_event_candidates']}`",
            f"- Official-source-backed: `{summary['official_source_backed']}`",
            f"- RSS/source-page-backed: `{summary['rss_source_page_backed']}`",
            f"- No source found: `{summary['no_source_found']}`",
            f"- Parse-ready: `{summary['parse_ready']}`",
            f"- Source-ready: `{summary['source_ready']}`",
            f"- Needs review: `{summary['needs_review']}`",
            f"- Unsupported: `{summary['unsupported']}`",
            "",
            "## Source Backlog",
            "",
        ]
    )
    for row in payload["source_backlog"]:
        lines.extend(
            [
                f"### {row['source_family']}",
                "",
                f"- Rows: `{row['row_count']}`",
                f"- Parser/source work: `{row['parser_recommendation']}`",
                f"- Official candidates: `{row['official_source_candidates']}`",
                f"- Example tickers: `{row['example_tickers']}`",
                f"- Backlog action: `{row['backlog_action']}`",
                "",
            ]
        )
    lines.extend(
        [
            "## Guardrails",
            "",
            "- No forecasts from unverified headlines.",
            "- No fuzzy event matching.",
            "- No fabricated events or source values.",
            "- No paper/live/demo orders.",
            "",
        ]
    )
    return "\n".join(lines)


def _write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _to_int(value: Any) -> int:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return 0


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y"}
