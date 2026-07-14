import json
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from kalshi_predictor.data.repositories import insert_market_snapshot
from kalshi_predictor.data.schema import Market, MarketRanking, MarketSnapshot
from kalshi_predictor.kalshi.client import KalshiAPIError, KalshiClient
from kalshi_predictor.utils.decimals import decimal_to_str, to_decimal
from kalshi_predictor.utils.time import utc_now

REASON_MISSING_ORDERBOOK = "missing_orderbook"
REASON_MARKET_CLOSED = "market_closed"
REASON_API_NOT_FOUND = "api_not_found"
REASON_UNSUPPORTED_MULTILEG = "unsupported_multileg"
REASON_STALE_SNAPSHOT = "stale_snapshot"
REASON_NO_LIQUIDITY = "no_liquidity"
REASON_COLLECTION_ERROR = "collection_error"

OPEN_STATUSES = {"open", "active", "trading"}
CLOSED_STATUSES = {"closed", "settled", "expired", "resolved", "finalized"}


class SnapshotClient(Protocol):
    def get_market(self, ticker: str) -> Mapping[str, Any]:
        ...

    def get_orderbook(self, ticker: str) -> Mapping[str, Any]:
        ...


@dataclass(frozen=True)
class SnapshotCoverageRepairResult:
    ranked_markets_scanned: int
    missing_data_rankings_found: int
    snapshots_repaired: int
    still_missing: int
    reason_counts: dict[str, int]
    rows: list[dict[str, Any]]


@dataclass(frozen=True)
class SnapshotCoverageRepairArtifacts:
    result: SnapshotCoverageRepairResult
    markdown_path: Path
    json_path: Path
    rows_path: Path


def write_snapshot_coverage_repair_report(
    session: Session,
    *,
    limit: int = 500,
    output: Path = Path("reports/snapshot_coverage_repair.md"),
    client: SnapshotClient | None = None,
) -> SnapshotCoverageRepairArtifacts:
    result = run_snapshot_coverage_repair(session, limit=limit, client=client)
    output.parent.mkdir(parents=True, exist_ok=True)
    json_path = output.with_suffix(".json")
    rows_path = output.with_name(f"{output.stem}_rows.json")
    output.write_text(_render_markdown(result), encoding="utf-8")
    json_path.write_text(
        json.dumps(
            {
                "safety": "PAPER_ONLY_NO_EXECUTION",
                "ranked_markets_scanned": result.ranked_markets_scanned,
                "missing_data_rankings_found": result.missing_data_rankings_found,
                "snapshots_repaired": result.snapshots_repaired,
                "still_missing": result.still_missing,
                "reason_counts": result.reason_counts,
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    rows_path.write_text(json.dumps(result.rows, indent=2, sort_keys=True), encoding="utf-8")
    return SnapshotCoverageRepairArtifacts(
        result=result,
        markdown_path=output,
        json_path=json_path,
        rows_path=rows_path,
    )


def run_snapshot_coverage_repair(
    session: Session,
    *,
    limit: int = 500,
    client: SnapshotClient | None = None,
) -> SnapshotCoverageRepairResult:
    owned_client: KalshiClient | None = None
    if client is None:
        owned_client = KalshiClient()
        client = owned_client

    try:
        scanned, rankings = _missing_data_rankings(session, limit=limit)
        rows = [_repair_ranking(session, ranking, client=client) for ranking in rankings]
    finally:
        if owned_client is not None:
            owned_client.close()

    reason_counts = Counter(row["reason"] for row in rows if row["reason"] != "repaired")
    snapshots_repaired = sum(1 for row in rows if row["repair_status"] == "repaired")
    return SnapshotCoverageRepairResult(
        ranked_markets_scanned=scanned,
        missing_data_rankings_found=len(rankings),
        snapshots_repaired=snapshots_repaired,
        still_missing=len(rows) - snapshots_repaired,
        reason_counts=dict(sorted(reason_counts.items())),
        rows=rows,
    )


def _missing_data_rankings(
    session: Session,
    *,
    limit: int,
) -> tuple[int, list[MarketRanking]]:
    statement = select(MarketRanking).order_by(
        desc(MarketRanking.ranked_at),
        desc(MarketRanking.opportunity_score),
        desc(MarketRanking.id),
    )
    scanned = 0
    rankings: list[MarketRanking] = []
    seen: set[str] = set()
    for ranking in session.scalars(statement):
        if ranking.ticker in seen:
            continue
        seen.add(ranking.ticker)
        scanned += 1
        market = session.get(Market, ranking.ticker)
        snapshot = _latest_snapshot(session, ranking.ticker)
        if _missing_effective_market_data(ranking, snapshot, market):
            rankings.append(ranking)
        if scanned >= limit:
            break
    return scanned, rankings


def _repair_ranking(
    session: Session,
    ranking: MarketRanking,
    *,
    client: SnapshotClient,
) -> dict[str, Any]:
    market = session.get(Market, ranking.ticker)
    latest = _latest_snapshot(session, ranking.ticker)
    before = _effective_market_data(ranking, latest, market)
    status = _status_from(market=market, ranking=ranking, snapshot=latest)
    if _is_closed_status(status):
        return _repair_row(
            ranking,
            market=market,
            latest=latest,
            before=before,
            after=before,
            repair_status="unrepaired",
            reason=REASON_MARKET_CLOSED,
        )

    api_market: Mapping[str, Any] | None = None
    orderbook: Mapping[str, Any] | None = None
    error: str | None = None
    try:
        api_market = client.get_market(ranking.ticker)
    except KalshiAPIError as exc:
        reason = REASON_API_NOT_FOUND if _looks_like_not_found(exc) else REASON_COLLECTION_ERROR
        return _repair_row(
            ranking,
            market=market,
            latest=latest,
            before=before,
            after=before,
            repair_status="unrepaired",
            reason=reason,
            error=str(exc),
        )
    except Exception as exc:  # pragma: no cover - defensive around external client adapters.
        return _repair_row(
            ranking,
            market=market,
            latest=latest,
            before=before,
            after=before,
            repair_status="unrepaired",
            reason=REASON_COLLECTION_ERROR,
            error=str(exc),
        )

    api_status = str(api_market.get("status") or status or "").lower()
    if _is_closed_status(api_status):
        return _repair_row(
            ranking,
            market=market,
            latest=latest,
            before=before,
            after=before,
            repair_status="unrepaired",
            reason=REASON_MARKET_CLOSED,
        )

    try:
        orderbook = client.get_orderbook(ranking.ticker)
    except KalshiAPIError as exc:
        error = str(exc)
        orderbook = None
    except Exception as exc:  # pragma: no cover - defensive around external client adapters.
        return _repair_row(
            ranking,
            market=market,
            latest=latest,
            before=before,
            after=before,
            repair_status="unrepaired",
            reason=REASON_COLLECTION_ERROR,
            error=str(exc),
        )

    snapshot = insert_market_snapshot(
        session,
        api_market,
        orderbook,
        captured_at=utc_now(),
    )
    refreshed_market = session.get(Market, ranking.ticker)
    after = _effective_market_data(ranking, snapshot, refreshed_market)
    if not _has_missing(after):
        return _repair_row(
            ranking,
            market=refreshed_market,
            latest=snapshot,
            before=before,
            after=after,
            repair_status="repaired",
            reason="repaired",
        )

    reason = _unrepaired_reason(
        ranking,
        market=refreshed_market,
        latest=snapshot,
        orderbook=orderbook,
        error=error,
    )
    return _repair_row(
        ranking,
        market=refreshed_market,
        latest=snapshot,
        before=before,
        after=after,
        repair_status="unrepaired",
        reason=reason,
        error=error,
    )


def _unrepaired_reason(
    ranking: MarketRanking,
    *,
    market: Market | None,
    latest: MarketSnapshot | None,
    orderbook: Mapping[str, Any] | None,
    error: str | None,
) -> str:
    title = ranking.title or (market.title if market is not None else None) or ""
    if _looks_like_multileg_market(ticker=ranking.ticker, title=title):
        return REASON_UNSUPPORTED_MULTILEG
    if orderbook is None:
        return REASON_MISSING_ORDERBOOK if error is None else REASON_COLLECTION_ERROR
    data = _effective_market_data(ranking, latest, market)
    if data["liquidity"] is None:
        return REASON_NO_LIQUIDITY
    if latest is not None and _is_stale_snapshot(latest):
        return REASON_STALE_SNAPSHOT
    return REASON_MISSING_ORDERBOOK


def _repair_row(
    ranking: MarketRanking,
    *,
    market: Market | None,
    latest: MarketSnapshot | None,
    before: dict[str, Decimal | None],
    after: dict[str, Decimal | None],
    repair_status: str,
    reason: str,
    error: str | None = None,
) -> dict[str, Any]:
    title = ranking.title or (market.title if market is not None else None) or ranking.ticker
    return {
        "ticker": ranking.ticker,
        "title": title,
        "forecast_model": ranking.forecast_model,
        "opportunity_score": ranking.opportunity_score,
        "market_status": _status_from(market=market, ranking=ranking, snapshot=latest) or "unknown",
        "latest_snapshot_at": latest.captured_at.isoformat() if latest is not None else None,
        "before_price": decimal_to_str(before["price"]),
        "before_spread": decimal_to_str(before["spread"]),
        "before_liquidity": decimal_to_str(before["liquidity"]),
        "after_price": decimal_to_str(after["price"]),
        "after_spread": decimal_to_str(after["spread"]),
        "after_liquidity": decimal_to_str(after["liquidity"]),
        "repair_status": repair_status,
        "reason": reason,
        "error": error,
    }


def _latest_snapshot(session: Session, ticker: str) -> MarketSnapshot | None:
    return session.scalar(
        select(MarketSnapshot)
        .where(MarketSnapshot.ticker == ticker)
        .order_by(desc(MarketSnapshot.captured_at), desc(MarketSnapshot.id))
        .limit(1)
    )


def _missing_effective_market_data(
    ranking: MarketRanking,
    snapshot: MarketSnapshot | None,
    market: Market | None,
) -> bool:
    return _has_missing(_effective_market_data(ranking, snapshot, market))


def _has_missing(data: Mapping[str, Decimal | None]) -> bool:
    return any(value is None or value == 0 for value in data.values())


def _effective_market_data(
    ranking: MarketRanking,
    snapshot: MarketSnapshot | None,
    market: Market | None,
) -> dict[str, Decimal | None]:
    return {
        "price": _effective_price(ranking, snapshot),
        "spread": _effective_spread(ranking, snapshot),
        "liquidity": _first_nonzero_decimal(
            ranking.liquidity,
            market.liquidity_dollars if market is not None else None,
            snapshot.open_interest_fp if snapshot is not None else None,
            snapshot.volume_24h_fp if snapshot is not None else None,
            snapshot.volume_fp if snapshot is not None else None,
        ),
    }


def _effective_price(
    ranking: MarketRanking,
    snapshot: MarketSnapshot | None,
) -> Decimal | None:
    ranking_price = _first_nonzero_decimal(ranking.best_price, ranking.midpoint)
    if ranking_price is not None:
        return ranking_price
    if snapshot is None:
        return None
    bid = to_decimal(snapshot.best_yes_bid)
    ask = to_decimal(snapshot.best_yes_ask)
    if bid is not None and ask is not None and bid != 0 and ask != 0:
        return (bid + ask) / Decimal("2")
    return _first_nonzero_decimal(snapshot.last_price_dollars, snapshot.best_yes_ask)


def _effective_spread(
    ranking: MarketRanking,
    snapshot: MarketSnapshot | None,
) -> Decimal | None:
    spread = _first_nonzero_decimal(ranking.spread, snapshot.spread if snapshot else None)
    if spread is not None:
        return spread
    if snapshot is None:
        return None
    bid = to_decimal(snapshot.best_yes_bid)
    ask = to_decimal(snapshot.best_yes_ask)
    if bid is None or ask is None:
        return None
    computed = abs(ask - bid)
    return computed if computed != 0 else None


def _first_nonzero_decimal(*values: Any) -> Decimal | None:
    for value in values:
        decimal = to_decimal(value)
        if decimal is not None and decimal != 0:
            return decimal
    return None


def _status_from(
    *,
    market: Market | None,
    ranking: MarketRanking,
    snapshot: MarketSnapshot | None,
) -> str | None:
    for value in (
        market.status if market is not None else None,
        ranking.status,
        snapshot.status if snapshot is not None else None,
    ):
        if value:
            return str(value)
    return None


def _is_closed_status(status: str | None) -> bool:
    return str(status or "").lower() in CLOSED_STATUSES


def _looks_like_not_found(exc: Exception) -> bool:
    text = str(exc).lower()
    return "404" in text or "not found" in text


def _is_stale_snapshot(snapshot: MarketSnapshot) -> bool:
    captured_at = snapshot.captured_at
    if captured_at.tzinfo is None:
        captured_at = captured_at.replace(tzinfo=utc_now().tzinfo)
    return captured_at < utc_now() - timedelta(hours=1)


def _looks_like_multileg_market(*, ticker: str, title: str) -> bool:
    text = f"{ticker} {title}".lower()
    return (
        "multigame" in text
        or "crosscategory" in text
        or ("," in title and (text.count(",yes ") + text.count(",no ") >= 1))
    )


def _render_markdown(result: SnapshotCoverageRepairResult) -> str:
    lines = [
        "# Phase 3AH Market Snapshot Coverage Repair",
        "",
        f"- Generated at: {utc_now().isoformat()}",
        "- Safety: PAPER_ONLY_NO_EXECUTION",
        f"- Ranked markets scanned: {result.ranked_markets_scanned}",
        f"- Missing-data rankings found: {result.missing_data_rankings_found}",
        f"- Snapshots repaired: {result.snapshots_repaired}",
        f"- Still missing: {result.still_missing}",
        "",
        "## Reason Breakdown",
        "",
    ]
    if result.reason_counts:
        lines.extend(["| Reason | Count |", "| --- | ---: |"])
        for reason, count in result.reason_counts.items():
            lines.append(f"| {reason} | {count} |")
    else:
        lines.append("No unrepaired rows.")
    lines.extend(["", "## Top Repaired Markets", ""])
    repaired = [row for row in result.rows if row["repair_status"] == "repaired"][:20]
    lines.extend(_rows_table(repaired, include_reason=False))
    lines.extend(["", "## Top Unrepaired Markets", ""])
    unrepaired = [row for row in result.rows if row["repair_status"] != "repaired"][:20]
    lines.extend(_rows_table(unrepaired, include_reason=True))
    lines.extend(
        [
            "",
            "## Recommended Next Action",
            "",
            _recommended_next_action(result),
            "",
        ]
    )
    return "\n".join(lines)


def _rows_table(rows: list[dict[str, Any]], *, include_reason: bool) -> list[str]:
    if not rows:
        return ["None."]
    headers = ["Ticker", "Score", "Status", "Price", "Spread", "Liquidity"]
    if include_reason:
        headers.append("Reason")
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        values = [
            str(row["ticker"]),
            str(row["opportunity_score"]),
            str(row["market_status"]),
            str(row["after_price"] or "n/a"),
            str(row["after_spread"] or "n/a"),
            str(row["after_liquidity"] or "n/a"),
        ]
        if include_reason:
            values.append(str(row["reason"]))
        lines.append("| " + " | ".join(value.replace("|", "\\|") for value in values) + " |")
    return lines


def _recommended_next_action(result: SnapshotCoverageRepairResult) -> str:
    if result.still_missing == 0:
        return "Snapshot coverage is repaired for the scanned ranked markets."
    if not result.reason_counts:
        return (
            "Run `kalshi-bot collect-once --status open --limit 100 --max-pages 1` "
            "and re-run this repair."
        )
    top_reason = max(result.reason_counts.items(), key=lambda item: item[1])[0]
    if top_reason == REASON_UNSUPPORTED_MULTILEG:
        return (
            "Multi-leg markets are still the main gap. Keep them grouped/deprioritized "
            "until the leg parser and market-specific orderbook support can validate them."
        )
    if top_reason == REASON_MARKET_CLOSED:
        return (
            "Closed markets cannot be repaired with live snapshots; rely on settlement "
            "reconciliation."
        )
    if top_reason == REASON_NO_LIQUIDITY:
        return (
            "Markets have price data but no usable liquidity; keep them out of learning "
            "decisions."
        )
    return (
        "Re-run snapshot collection, then re-run "
        "`kalshi-bot snapshot-coverage-repair --limit 500`."
    )
