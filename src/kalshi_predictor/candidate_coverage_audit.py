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
    Forecast,
    Market,
    MarketLeg,
    MarketRanking,
    MarketSnapshot,
    WeatherMarketLink,
)
from kalshi_predictor.phase_gh2 import CRYPTO_TICKER_PREFIXES
from kalshi_predictor.utils.time import utc_now
from kalshi_predictor.weather.linker import WEATHER_TICKER_PREFIXES

AUDIT_VERSION = "candidate_coverage_audit_v1"
FUNNEL_STAGES = (
    "catalog",
    "active",
    "semantically_supported",
    "linked",
    "fresh_snapshot",
    "forecast",
    "ranking",
    "candidate_manifest",
    "positive_gross_ev",
    "executable_ev",
    "paper_ready",
)


@dataclass(frozen=True)
class CandidateCoverageArtifacts:
    json_path: Path
    markdown_path: Path


def write_candidate_coverage_audit(
    session: Session,
    *,
    gh1_manifest_path: Path,
    gh2_report_path: Path,
    crypto_r5_path: Path,
    output_dir: Path = Path("reports/candidate_coverage"),
    freshness_minutes: int = 15,
    addition_limit: int = 50,
) -> CandidateCoverageArtifacts:
    manifest = _read_object(gh1_manifest_path)
    gh2 = _read_object(gh2_report_path)
    r5 = _read_object(crypto_r5_path)
    evidence = collect_candidate_coverage_evidence(
        session,
        freshness_minutes=freshness_minutes,
    )
    payload = build_candidate_coverage_audit(
        evidence=evidence,
        manifest_payload=manifest,
        gh2_payload=gh2,
        crypto_r5_payload=r5,
        freshness_minutes=freshness_minutes,
        addition_limit=addition_limit,
        sources={
            "gh1_manifest": str(gh1_manifest_path),
            "gh2": str(gh2_report_path),
            "crypto_r5": str(crypto_r5_path),
        },
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "candidate_coverage_audit.json"
    markdown_path = output_dir / "candidate_coverage_audit.md"
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    markdown_path.write_text(_render_markdown(payload), encoding="utf-8")
    return CandidateCoverageArtifacts(json_path=json_path, markdown_path=markdown_path)


def collect_candidate_coverage_evidence(
    session: Session,
    *,
    freshness_minutes: int,
) -> list[dict[str, Any]]:
    now = utc_now()
    fresh_after = now - timedelta(minutes=freshness_minutes)
    market_rows = session.execute(
        select(
            Market.ticker,
            Market.event_ticker,
            Market.series_ticker,
            Market.title,
            Market.status,
            Market.close_time,
        )
    ).all()
    legs: dict[str, set[str]] = defaultdict(set)
    for ticker, category in session.execute(
        select(MarketLeg.ticker, MarketLeg.category).distinct()
    ):
        legs[str(ticker)].add(str(category).lower())
    crypto_links = {
        str(ticker): str(symbol)
        for ticker, symbol in session.execute(
            select(CryptoMarketLink.ticker, CryptoMarketLink.symbol).distinct()
        )
    }
    weather_links = {
        str(ticker): {"location_key": str(location), "target_time": target_time}
        for ticker, location, target_time in session.execute(
            select(
                WeatherMarketLink.ticker,
                WeatherMarketLink.location_key,
                WeatherMarketLink.target_time,
            )
        )
    }
    snapshots = _latest_times(session, MarketSnapshot.ticker, MarketSnapshot.captured_at)
    forecasts = _latest_times(session, Forecast.ticker, Forecast.forecasted_at)
    ranking_ids: dict[str, int] = {
        str(ticker): int(ranking_id)
        for ticker, ranking_id in session.execute(
            select(MarketRanking.ticker, func.max(MarketRanking.id)).group_by(
                MarketRanking.ticker
            )
        )
        if ranking_id is not None
    }
    rankings: dict[str, MarketRanking] = {}
    if ranking_ids:
        for ranking_row in session.scalars(
            select(MarketRanking).where(MarketRanking.id.in_(ranking_ids.values()))
        ):
            rankings[ranking_row.ticker] = ranking_row
    evidence = []
    for ticker, event_ticker, series_ticker, title, status, close_time in market_rows:
        ticker = str(ticker)
        category = _category(ticker, legs.get(ticker, set()))
        active = _active(status, close_time, now=now)
        linked = ticker in crypto_links or ticker in weather_links
        supported = _semantically_supported(
            ticker=ticker,
            category=category,
            leg_categories=legs.get(ticker, set()),
            linked=linked,
        )
        ranking = rankings.get(ticker)
        ranking_raw = _decode_object(ranking.raw_json if ranking else None)
        gross_ev = _decimal(
            ranking_raw.get("gross_expected_value")
            if ranking_raw
            else ranking.estimated_edge if ranking else None
        )
        fee_ev = _decimal(ranking_raw.get("fee_adjusted_expected_value"))
        tick_valid = ranking_raw.get("price_tick_valid")
        snapshot_at = snapshots.get(ticker)
        forecast_at = forecasts.get(ticker)
        ranking_at = ranking.ranked_at if ranking else None
        evidence.append(
            {
                "ticker": ticker,
                "event_ticker": event_ticker,
                "series_ticker": series_ticker,
                "title": title,
                "category": category,
                "catalog": True,
                "active": active,
                "semantically_supported": supported,
                "linked": linked,
                "fresh_snapshot": _fresh(snapshot_at, fresh_after),
                "forecast": _fresh(forecast_at, fresh_after),
                "ranking": _fresh(ranking_at, fresh_after),
                "positive_gross_ev": gross_ev is not None and gross_ev > 0,
                "executable_ev": (
                    fee_ev is not None and fee_ev > 0 and tick_valid is not False
                ),
                "gross_expected_value": _decimal_text(gross_ev),
                "estimated_taker_fee": ranking_raw.get("estimated_taker_fee"),
                "fee_adjusted_expected_value": _decimal_text(fee_ev),
                "price_tick_valid": tick_valid,
                "forecast_probability": ranking.forecast_probability if ranking else None,
                "best_side": ranking.best_side if ranking else None,
                "best_price": ranking.best_price if ranking else None,
                "liquidity": ranking.liquidity if ranking else None,
                "liquidity_score": ranking.liquidity_score if ranking else None,
                "spread": ranking.spread if ranking else None,
                "snapshot_at": _iso(snapshot_at),
                "forecast_at": _iso(forecast_at),
                "ranking_at": _iso(ranking_at),
                "crypto_symbol": crypto_links.get(ticker),
                "weather_location": (weather_links.get(ticker) or {}).get("location_key"),
                "weather_target_time": _iso(
                    (weather_links.get(ticker) or {}).get("target_time")
                ),
                "leg_categories": sorted(legs.get(ticker, set())),
            }
        )
    return evidence


def build_candidate_coverage_audit(
    *,
    evidence: list[dict[str, Any]],
    manifest_payload: dict[str, Any],
    gh2_payload: dict[str, Any],
    crypto_r5_payload: dict[str, Any],
    freshness_minutes: int,
    addition_limit: int,
    sources: dict[str, str] | None = None,
) -> dict[str, Any]:
    manifest = _manifest_tickers(manifest_payload, gh2_payload)
    paper_ready = _paper_ready_tickers(gh2_payload, crypto_r5_payload)
    rows = []
    for source in evidence:
        row = dict(source)
        ticker = str(row["ticker"])
        row["candidate_manifest"] = ticker in manifest
        row["paper_ready"] = ticker in paper_ready
        row["first_exclusion_stage"] = _first_exclusion_stage(row)
        row["exclusion_reason"] = _exclusion_reason(row["first_exclusion_stage"], row)
        rows.append(row)
    categories = sorted({str(row["category"]) for row in rows})
    funnels = {
        category: _category_funnel(rows, category=category) for category in categories
    }
    additions = _safe_additions(rows, limit=addition_limit)
    return {
        "audit_version": AUDIT_VERSION,
        "generated_at": utc_now().isoformat(),
        "mode": "READ_ONLY_CANDIDATE_COVERAGE_DIAGNOSTIC",
        "safety": {
            "database_open_mode": "sqlite_mode_ro_query_only",
            "database_writes": 0,
            "order_apis_imported": False,
            "order_creation": False,
            "gh2_triggered": False,
            "thresholds_changed": False,
            "fee_adjusted_ev_is_diagnostic_only": True,
        },
        "parameters": {
            "freshness_minutes": freshness_minutes,
            "addition_limit": addition_limit,
        },
        "sources": sources or {},
        "source_generated_at": {
            "gh1_manifest": manifest_payload.get("generated_at"),
            "gh2": gh2_payload.get("generated_at"),
            "crypto_r5": crypto_r5_payload.get("generated_at"),
        },
        "summary": {
            "catalog_rows": len(rows),
            "active_rows": sum(bool(row["active"]) for row in rows),
            "manifest_rows": sum(bool(row["candidate_manifest"]) for row in rows),
            "paper_ready_rows": sum(bool(row["paper_ready"]) for row in rows),
            "safe_addition_rows": len(additions),
            "category_count": len(categories),
        },
        "category_funnels": funnels,
        "manifest_selection_diagnostics": _manifest_selection_diagnostics(
            rows,
            gh2_payload=gh2_payload,
            manifest_payload=manifest_payload,
        ),
        "exclusion_reason_counts": dict(
            sorted(Counter(str(row["exclusion_reason"]) for row in rows).items())
        ),
        "safe_coverage_additions": additions,
        "coverage_dimensions": _coverage_dimensions(rows, additions),
    }


def _category_funnel(rows: list[dict[str, Any]], *, category: str) -> dict[str, Any]:
    category_rows = [row for row in rows if row["category"] == category]
    counts: dict[str, int] = {}
    surviving = category_rows
    for stage in FUNNEL_STAGES:
        surviving = [row for row in surviving if bool(row.get(stage))]
        counts[stage] = len(surviving)
    exclusions = Counter(
        str(row["exclusion_reason"])
        for row in category_rows
        if row["first_exclusion_stage"] != "paper_ready"
    )
    return {
        "counts": counts,
        "excluded_by_reason": dict(sorted(exclusions.items())),
        "first_blocker": _most_common(exclusions),
    }


def _safe_additions(rows: list[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
    candidates = [
        row
        for row in rows
        if row["active"]
        and row["semantically_supported"]
        and not row["candidate_manifest"]
    ]
    candidates.sort(
        key=lambda row: (
            _decimal(row.get("gross_expected_value")) or Decimal("-999"),
            bool(row.get("ranking")),
            bool(row.get("forecast")),
            bool(row.get("fresh_snapshot")),
            str(row["ticker"]),
        ),
        reverse=True,
    )
    return [
        {
            "ticker": row["ticker"],
            "category": row["category"],
            "series_ticker": row.get("series_ticker"),
            "event_ticker": row.get("event_ticker"),
            "crypto_symbol": row.get("crypto_symbol"),
            "weather_location": row.get("weather_location"),
            "weather_target_time": row.get("weather_target_time"),
            "first_missing_stage": row["first_exclusion_stage"],
            "reason": row["exclusion_reason"],
            "gross_expected_value": row.get("gross_expected_value"),
            "fee_adjusted_expected_value": row.get("fee_adjusted_expected_value"),
            "next_action": _safe_next_action(row),
        }
        for row in candidates[: max(0, limit)]
    ]


def _coverage_dimensions(
    rows: list[dict[str, Any]], additions: list[dict[str, Any]]
) -> dict[str, Any]:
    manifest_rows = [row for row in rows if row["candidate_manifest"]]
    return {
        "current_manifest": {
            "crypto_symbols": sorted(
                {str(row["crypto_symbol"]) for row in manifest_rows if row.get("crypto_symbol")}
            ),
            "crypto_events": sorted(
                {str(row["event_ticker"]) for row in manifest_rows if row["category"] == "crypto"}
            ),
            "weather_locations": sorted(
                {
                    str(row["weather_location"])
                    for row in manifest_rows
                    if row.get("weather_location")
                }
            ),
        },
        "safe_additions": {
            "crypto_symbols": sorted(
                {str(row["crypto_symbol"]) for row in additions if row.get("crypto_symbol")}
            ),
            "crypto_events": sorted(
                {str(row["event_ticker"]) for row in additions if row["category"] == "crypto"}
            ),
            "weather_locations": sorted(
                {str(row["weather_location"]) for row in additions if row.get("weather_location")}
            ),
            "weather_target_times": sorted(
                {
                    str(row["weather_target_time"])
                    for row in additions
                    if row.get("weather_target_time")
                }
            ),
        },
    }


def _first_exclusion_stage(row: dict[str, Any]) -> str:
    for stage in FUNNEL_STAGES:
        if not bool(row.get(stage)):
            return stage
    return "paper_ready"


def _exclusion_reason(stage: str, row: dict[str, Any]) -> str:
    if stage == "active":
        return "MARKET_NOT_ACTIVE"
    if stage == "semantically_supported":
        return "CATEGORY_NOT_SUPPORTED_BY_GH2"
    if stage == "linked":
        return "SUPPORTED_MARKET_NOT_LINKED"
    if stage == "fresh_snapshot":
        return "SNAPSHOT_MISSING_OR_STALE"
    if stage == "forecast":
        return "FORECAST_MISSING_OR_STALE"
    if stage == "ranking":
        return "RANKING_MISSING_OR_STALE"
    if stage == "candidate_manifest":
        if not row.get("series_ticker"):
            return "MANIFEST_UNKNOWN_SERIES_BUCKET"
        return "NOT_SELECTED_IN_CANDIDATE_MANIFEST"
    if stage == "positive_gross_ev":
        return "EV_NOT_POSITIVE"
    if stage == "executable_ev":
        return "FEE_ADJUSTED_EV_NOT_POSITIVE_OR_TICK_INVALID"
    if stage == "paper_ready":
        return "DOWNSTREAM_EXECUTION_OR_RISK_GATE"
    return f"UNKNOWN_{stage.upper()}"


def _safe_next_action(row: dict[str, Any]) -> str:
    stage = str(row["first_exclusion_stage"])
    if row.get("exclusion_reason") == "MANIFEST_UNKNOWN_SERIES_BUCKET":
        return (
            "Repair series lineage before considering bounded manifest coverage; "
            "do not bypass diversity selection."
        )
    return {
        "linked": "Review exact semantic link evidence; do not auto-link ambiguity.",
        "fresh_snapshot": "Allow the existing scheduled collector to capture this ticker.",
        "forecast": "Include the ticker in a future bounded forecast scope.",
        "ranking": "Include the ticker in a future bounded ranking scope.",
        "candidate_manifest": "Consider bounded manifest coverage after freshness review.",
        "positive_gross_ev": "Observe only; wait for model or price movement.",
        "executable_ev": "Observe only; wait for fee-adjusted executable EV.",
    }.get(stage, "Keep excluded until authoritative evidence clears the current stage.")


def _manifest_selection_diagnostics(
    rows: list[dict[str, Any]],
    *,
    gh2_payload: dict[str, Any],
    manifest_payload: dict[str, Any],
) -> dict[str, Any]:
    ranked = [row for row in rows if row.get("ranking")]
    unknown_series = [row for row in ranked if not row.get("series_ticker")]
    unknown_manifest = [row for row in unknown_series if row.get("candidate_manifest")]
    unknown_excluded = [row for row in unknown_series if not row.get("candidate_manifest")]
    alignment = gh2_payload.get("candidate_alignment") or {}
    return {
        "selection": manifest_payload.get("selection"),
        "gh2_ranked_candidates": alignment.get("ranked_candidates"),
        "ranked_rows_without_series": len(unknown_series),
        "manifest_rows_without_series": len(unknown_manifest),
        "excluded_ranked_rows_without_series": len(unknown_excluded),
        "excluded_tickers": sorted(str(row["ticker"]) for row in unknown_excluded),
        "interpretation": (
            "Rows without series_ticker share the selector's UNKNOWN diversity bucket; "
            "this diagnostic does not change manifest selection or gate authority."
            if unknown_excluded
            else "No ranked row was excluded from the manifest with missing series lineage."
        ),
    }


def _category(ticker: str, leg_categories: set[str]) -> str:
    if ticker.startswith(CRYPTO_TICKER_PREFIXES):
        return "crypto"
    if ticker.startswith(WEATHER_TICKER_PREFIXES):
        return "weather"
    normalized = {category for category in leg_categories if category}
    if len(normalized) == 1:
        return next(iter(normalized))
    if len(normalized) > 1:
        return "composite"
    return "unclassified"


def _semantically_supported(
    *, ticker: str, category: str, leg_categories: set[str], linked: bool
) -> bool:
    if category == "crypto":
        return ticker.startswith(CRYPTO_TICKER_PREFIXES) and (
            "crypto" in leg_categories or linked
        )
    if category == "weather":
        return ticker.startswith(WEATHER_TICKER_PREFIXES) and (
            "weather" in leg_categories or linked
        )
    return False


def _active(status: Any, close_time: datetime | None, *, now: datetime) -> bool:
    if str(status or "").lower() not in {"active", "open"}:
        return False
    return close_time is None or _aware(close_time) > now


def _latest_times(session: Session, ticker_column: Any, time_column: Any) -> dict[str, datetime]:
    return {
        str(ticker): timestamp
        for ticker, timestamp in session.execute(
            select(ticker_column, func.max(time_column)).group_by(ticker_column)
        )
        if timestamp is not None
    }


def _fresh(timestamp: datetime | None, fresh_after: datetime) -> bool:
    return timestamp is not None and _aware(timestamp) >= fresh_after


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=utc_now().tzinfo)


def _iso(value: Any) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else None


def _decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _decimal_text(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


def _decode_object(value: str | None) -> dict[str, Any]:
    try:
        payload = json.loads(value or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _manifest_tickers(manifest: dict[str, Any], gh2: dict[str, Any]) -> set[str]:
    raw = manifest.get("tickers") or [
        row.get("ticker") for row in manifest.get("candidates") or []
    ]
    if not raw:
        raw = (gh2.get("candidate_alignment") or {}).get("tickers") or []
    return {str(ticker) for ticker in raw if ticker}


def _paper_ready_tickers(gh2: dict[str, Any], r5: dict[str, Any]) -> set[str]:
    tickers = {
        str(row.get("ticker"))
        for row in (gh2.get("weather_gate") or {}).get("weather_rows") or []
        if row.get("ticker") and row.get("paper_ready")
    }
    for row in r5.get("positive_ev_preflight_candidates") or []:
        if row.get("ticker") and (
            row.get("paper_ready") or row.get("readiness_status") == "PAPER_READY"
        ):
            tickers.add(str(row["ticker"]))
    return tickers


def _most_common(counter: Counter[str]) -> str | None:
    return counter.most_common(1)[0][0] if counter else None


def _read_object(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Expected a JSON object at {path}")
    return payload


def _render_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Candidate Coverage Audit",
        "",
        "Read-only diagnostic. Existing safety and profitability gates are unchanged.",
        "",
        "## Funnel Counts",
        "",
        "| Category | " + " | ".join(FUNNEL_STAGES) + " |",
        "| --- | " + " | ".join("---:" for _ in FUNNEL_STAGES) + " |",
    ]
    for category, funnel in payload["category_funnels"].items():
        counts = funnel["counts"]
        lines.append(
            f"| {category} | "
            + " | ".join(str(counts[stage]) for stage in FUNNEL_STAGES)
            + " |"
        )
    lines.extend(
        [
            "",
            "## Safe Coverage Additions",
            "",
            "| Ticker | Category | Missing stage | Reason | Next action |",
            "| --- | --- | --- | --- | --- |",
        ]
    )
    for row in payload["safe_coverage_additions"]:
        lines.append(
            f"| {row['ticker']} | {row['category']} | {row['first_missing_stage']} | "
            f"{row['reason']} | {row['next_action']} |"
        )
    return "\n".join(lines) + "\n"
