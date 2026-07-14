import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from kalshi_predictor.economic.linker import EconomicLinkResult, link_economic_markets
from kalshi_predictor.forecasting.registry import (
    ForecastRunSummary,
    latest_snapshots_for_model,
    run_forecast_models,
)
from kalshi_predictor.ingest.markets import sync_markets
from kalshi_predictor.ingest.snapshots import capture_snapshots
from kalshi_predictor.kalshi.client import KalshiClient
from kalshi_predictor.utils.time import utc_now

ECONOMIC_SERIES_CATEGORY = "Economics"
ECONOMIC_CATEGORY_TERMS: dict[str, tuple[str, ...]] = {
    "cpi": (
        "cpi",
        "inflation",
        "pce",
        "core pce",
        "core cpi",
        "consumer price",
        "prices",
    ),
    "fed": (
        "fed",
        "fomc",
        "federal reserve",
        "interest rate",
        "rate cut",
        "rate hike",
        "dot plot",
    ),
    "jobs": (
        "unemployment",
        "jobs",
        "payroll",
        "employment",
        "labor",
        "jobless",
        "nonfarm",
    ),
    "gdp": (
        "gdp",
        "growth",
        "recession",
        "gross domestic product",
    ),
}
ECONOMIC_CATEGORY_ORDER = ("cpi", "fed", "jobs", "gdp")


@dataclass(frozen=True)
class EconomicSeriesCandidate:
    series_ticker: str
    title: str
    category: str
    tags: tuple[str, ...]
    frequency: str | None
    matched_event_category: str
    match_reason: str
    score: int


@dataclass(frozen=True)
class Phase3BDEconomicDiscoveryArtifacts:
    json_path: Path
    markdown_path: Path
    payload: dict[str, Any]


def economic_series_candidates(
    series_payload: dict[str, Any],
    *,
    max_candidates: int,
) -> tuple[list[EconomicSeriesCandidate], dict[str, int]]:
    rows = series_payload.get("series", [])
    if not isinstance(rows, list):
        rows = []
    economics_rows = [
        row
        for row in rows
        if isinstance(row, dict)
        and str(row.get("category") or "").casefold() == ECONOMIC_SERIES_CATEGORY.casefold()
    ]
    candidates = [
        candidate
        for row in economics_rows
        if (candidate := _candidate_from_series_row(row)) is not None
    ]
    selected = _select_diverse_candidates(candidates, max_candidates=max_candidates)
    counts = {
        "series_seen": len(rows),
        "economics_series_seen": len(economics_rows),
        "candidate_rows": len(candidates),
        "selected_candidates": len(selected),
    }
    return selected, counts


def run_phase3bd_economic_market_discovery(
    session: Session,
    *,
    client: KalshiClient | None = None,
    max_series: int = 24,
    markets_per_series: int = 100,
    snapshot_series_limit: int = 12,
    forecast_limit: int = 500,
    include_orderbooks: bool = True,
    series_api_limit: int | None = None,
) -> dict[str, Any]:
    owns_client = client is None
    client = client or KalshiClient()
    generated_at = utc_now()
    try:
        series_payload = client.get_series(limit=series_api_limit)
        selected_candidates, series_counts = economic_series_candidates(
            series_payload,
            max_candidates=max_series,
        )
        series_hints = {
            candidate.series_ticker: candidate.matched_event_category
            for candidate in selected_candidates
        }
        markets_synced_by_series: dict[str, int] = {}
        for candidate in selected_candidates:
            markets_synced_by_series[candidate.series_ticker] = sync_markets(
                status="open",
                max_pages=1,
                limit=markets_per_series,
                series_ticker=candidate.series_ticker,
                session=session,
                client=client,
            )

        link_result = link_economic_markets(
            session,
            series_tickers=series_hints,
            series_category_hints=series_hints,
        )

        snapshots_captured_by_series: dict[str, int] = {}
        snapshot_candidates = [
            candidate
            for candidate in selected_candidates
            if markets_synced_by_series.get(candidate.series_ticker, 0) > 0
        ][:snapshot_series_limit]
        for candidate in snapshot_candidates:
            snapshots = capture_snapshots(
                status="open",
                max_pages=1,
                limit=markets_per_series,
                series_ticker=candidate.series_ticker,
                include_orderbook=include_orderbooks,
                orderbook_throttle_seconds=0.05 if include_orderbooks else 0,
                session=session,
                client=client,
            )
            snapshots_captured_by_series[candidate.series_ticker] = len(snapshots)

        forecast_snapshots = latest_snapshots_for_model(
            session,
            model_name="economic_v1",
            limit=forecast_limit,
        ) or []
        forecast_summary = run_forecast_models(
            session,
            model_name="economic_v1",
            snapshots=forecast_snapshots,
        )

        payload = _payload(
            generated_at=generated_at.isoformat(),
            series_counts=series_counts,
            selected_candidates=selected_candidates,
            markets_synced_by_series=markets_synced_by_series,
            link_result=link_result,
            snapshots_captured_by_series=snapshots_captured_by_series,
            forecast_summary=forecast_summary,
            include_orderbooks=include_orderbooks,
        )
        return payload
    finally:
        if owns_client:
            client.close()


def write_phase3bd_economic_market_discovery_report(
    *,
    session: Session,
    output_dir: Path,
    client: KalshiClient | None = None,
    max_series: int = 24,
    markets_per_series: int = 100,
    snapshot_series_limit: int = 12,
    forecast_limit: int = 500,
    include_orderbooks: bool = True,
    series_api_limit: int | None = None,
) -> Phase3BDEconomicDiscoveryArtifacts:
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = run_phase3bd_economic_market_discovery(
        session,
        client=client,
        max_series=max_series,
        markets_per_series=markets_per_series,
        snapshot_series_limit=snapshot_series_limit,
        forecast_limit=forecast_limit,
        include_orderbooks=include_orderbooks,
        series_api_limit=series_api_limit,
    )
    json_path = output_dir / "phase3bd_economic_market_discovery.json"
    markdown_path = output_dir / "phase3bd_economic_market_discovery.md"
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    markdown_path.write_text(_markdown(payload), encoding="utf-8")
    return Phase3BDEconomicDiscoveryArtifacts(
        json_path=json_path,
        markdown_path=markdown_path,
        payload=payload,
    )


def _candidate_from_series_row(row: dict[str, Any]) -> EconomicSeriesCandidate | None:
    series_ticker = str(row.get("ticker") or "").strip().upper()
    if not series_ticker:
        return None
    tags = tuple(str(tag) for tag in row.get("tags") or [] if tag)
    title = str(row.get("title") or "")
    text = " ".join(
        [
            series_ticker,
            title,
            str(row.get("subtitle") or ""),
            " ".join(tags),
        ]
    ).casefold()
    best_category = ""
    best_matches: list[str] = []
    for category in ECONOMIC_CATEGORY_ORDER:
        matches = [term for term in ECONOMIC_CATEGORY_TERMS[category] if term in text]
        if len(matches) > len(best_matches):
            best_category = category
            best_matches = matches
    if not best_category:
        return None
    ticker_bonus = 3 if best_category in series_ticker.casefold() else 0
    tag_bonus = 2 if any(best_category in tag.casefold() for tag in tags) else 0
    score = len(best_matches) * 10 + ticker_bonus + tag_bonus
    return EconomicSeriesCandidate(
        series_ticker=series_ticker,
        title=title,
        category=str(row.get("category") or ""),
        tags=tags,
        frequency=str(row.get("frequency") or "") or None,
        matched_event_category=best_category,
        match_reason=", ".join(best_matches[:6]),
        score=score,
    )


def _select_diverse_candidates(
    candidates: list[EconomicSeriesCandidate],
    *,
    max_candidates: int,
) -> list[EconomicSeriesCandidate]:
    if max_candidates <= 0:
        return []
    ranked = sorted(
        candidates,
        key=lambda candidate: (
            -candidate.score,
            ECONOMIC_CATEGORY_ORDER.index(candidate.matched_event_category),
            candidate.series_ticker,
        ),
    )
    selected: list[EconomicSeriesCandidate] = []
    selected_tickers: set[str] = set()
    per_category_floor = max(1, max_candidates // len(ECONOMIC_CATEGORY_ORDER))
    for category in ECONOMIC_CATEGORY_ORDER:
        category_rows = [
            candidate for candidate in ranked if candidate.matched_event_category == category
        ][:per_category_floor]
        for candidate in category_rows:
            if len(selected) >= max_candidates:
                break
            selected.append(candidate)
            selected_tickers.add(candidate.series_ticker)
    for candidate in ranked:
        if len(selected) >= max_candidates:
            break
        if candidate.series_ticker in selected_tickers:
            continue
        selected.append(candidate)
        selected_tickers.add(candidate.series_ticker)
    return selected


def _payload(
    *,
    generated_at: str,
    series_counts: dict[str, int],
    selected_candidates: list[EconomicSeriesCandidate],
    markets_synced_by_series: dict[str, int],
    link_result: EconomicLinkResult,
    snapshots_captured_by_series: dict[str, int],
    forecast_summary: ForecastRunSummary,
    include_orderbooks: bool,
) -> dict[str, Any]:
    markets_synced = sum(markets_synced_by_series.values())
    snapshots_captured = sum(snapshots_captured_by_series.values())
    forecasts_inserted = forecast_summary.forecasts_inserted
    status = "ACTIVE" if forecasts_inserted > 0 else "WAITING_FOR_COMPATIBLE_MARKETS"
    if markets_synced > 0 and link_result.links_created == 0:
        status = "WAITING_FOR_LINKS"
    if link_result.links_created > 0 and snapshots_captured == 0:
        status = "WAITING_FOR_SNAPSHOTS"
    if snapshots_captured > 0 and forecasts_inserted == 0:
        status = "WAITING_FOR_FORECASTABLE_MARKETS"
    return {
        "phase": "3BD",
        "generated_at": generated_at,
        "mode": "PAPER_READ_ONLY_DISCOVERY",
        "live_demo_execution": "blocked",
        "summary": {
            **series_counts,
            "markets_synced": markets_synced,
            "links_created": link_result.links_created,
            "links_skipped_existing": link_result.links_skipped_existing,
            "snapshots_captured": snapshots_captured,
            "forecast_snapshots_scanned": forecast_summary.snapshots_scanned,
            "forecasts_inserted": forecasts_inserted,
            "forecast_skipped": forecast_summary.skipped,
            "status": status,
        },
        "selected_series": [asdict(candidate) for candidate in selected_candidates],
        "markets_synced_by_series": markets_synced_by_series,
        "links_by_category": link_result.by_category,
        "snapshots_captured_by_series": snapshots_captured_by_series,
        "config": {
            "include_orderbooks": include_orderbooks,
            "evidence_gate": (
                "Kalshi series category must be Economics before metadata hints are trusted."
            ),
        },
        "recommended_next_action": _recommended_next_action(
            status=status,
            forecasts_inserted=forecasts_inserted,
            links_created=link_result.links_created,
            snapshots_captured=snapshots_captured,
        ),
    }


def _recommended_next_action(
    *,
    status: str,
    forecasts_inserted: int,
    links_created: int,
    snapshots_captured: int,
) -> str:
    if forecasts_inserted > 0:
        return (
            "Run model-readiness and find-opportunities after Phase 3BD; "
            "economic_v1 now has fresh forecast evidence."
        )
    if links_created > 0 and snapshots_captured == 0:
        return "Capture economic market snapshots for linked series, then forecast economic_v1."
    if status == "WAITING_FOR_LINKS":
        return (
            "Inspect selected Economics series titles; no compatible open market titles linked yet."
        )
    return "Keep Phase 3BD in the safe reporting loop until open Economics markets are available."


def _markdown(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    lines = [
        "# Phase 3BD Economic Market Discovery",
        "",
        f"Generated: {payload['generated_at']}",
        "",
        "Mode: PAPER / READ ONLY. Live/demo execution remains blocked.",
        "",
        "## Summary",
        "",
        f"- Status: {summary['status']}",
        f"- Kalshi series seen: {summary['series_seen']}",
        f"- Economics series seen: {summary['economics_series_seen']}",
        f"- Selected candidates: {summary['selected_candidates']}",
        f"- Markets synced: {summary['markets_synced']}",
        f"- Links created: {summary['links_created']}",
        f"- Existing links skipped: {summary['links_skipped_existing']}",
        f"- Snapshots captured: {summary['snapshots_captured']}",
        f"- Forecasts inserted: {summary['forecasts_inserted']}",
        f"- Forecast skips: {summary['forecast_skipped']}",
        "",
        "## Selected Series",
        "",
        "| Series | Title | Category | Reason | Score | Markets | Snapshots |",
        "| --- | --- | --- | --- | ---: | ---: | ---: |",
    ]
    markets_by_series = payload["markets_synced_by_series"]
    snapshots_by_series = payload["snapshots_captured_by_series"]
    for row in payload["selected_series"]:
        series = row["series_ticker"]
        lines.append(
            "| "
            + " | ".join(
                [
                    series,
                    str(row["title"]).replace("|", "\\|"),
                    row["matched_event_category"],
                    str(row["match_reason"]).replace("|", "\\|"),
                    str(row["score"]),
                    str(markets_by_series.get(series, 0)),
                    str(snapshots_by_series.get(series, 0)),
                ]
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "## Next Action",
            "",
            payload["recommended_next_action"],
            "",
        ]
    )
    return "\n".join(lines)
