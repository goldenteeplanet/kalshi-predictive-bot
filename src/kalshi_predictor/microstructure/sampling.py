from __future__ import annotations

import json
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.data.locks import is_database_locked_error
from kalshi_predictor.data.repositories import insert_market_snapshot
from kalshi_predictor.data.schema import Market, MarketRanking, MarketSnapshot
from kalshi_predictor.kalshi.client import KalshiAPIError, KalshiClient
from kalshi_predictor.microstructure.orderbook_features import (
    MicrostructureBuildResult,
    build_microstructure_features,
)
from kalshi_predictor.paper.settlement_reconciliation import PAPER_ONLY_SAFETY
from kalshi_predictor.utils.time import utc_now

OPEN_STATUSES = {"", "open", "active", "trading"}
CLOSED_STATUSES = {"closed", "settled", "expired", "resolved", "finalized"}


class MicrostructureSamplingClient(Protocol):
    def get_market(self, ticker: str) -> Mapping[str, Any]:
        ...

    def get_orderbook(self, ticker: str) -> Mapping[str, Any]:
        ...


@dataclass(frozen=True)
class MicrostructureSamplingResult:
    target_tickers: int
    cycles: int
    snapshots_inserted: int
    errors: int
    skipped_closed: int
    feature_summary: MicrostructureBuildResult
    rows: list[dict[str, Any]]


@dataclass(frozen=True)
class MicrostructureSamplingArtifacts:
    result: MicrostructureSamplingResult
    json_path: Path
    markdown_path: Path
    rows_path: Path


def sample_microstructure_watchlist(
    session: Session,
    *,
    limit: int = 50,
    cycles: int = 3,
    interval_seconds: float = 5,
    lookback_minutes: int | None = None,
    include_orderbook: bool = True,
    settings: Settings | None = None,
    client: MicrostructureSamplingClient | None = None,
    release_read_lock_before_sampling: bool = False,
    commit_after_each_sample: bool = False,
    write_retries: int = 3,
    write_retry_seconds: float = 2,
) -> MicrostructureSamplingResult:
    """Collect repeated observations for stable tickers, then build microstructure rows."""

    resolved = settings or get_settings()
    targets = _target_tickers(session, limit=limit)
    if release_read_lock_before_sampling and targets:
        session.commit()
    owned_client: KalshiClient | None = None
    if client is None and targets:
        owned_client = KalshiClient()
        client = owned_client

    rows: list[dict[str, Any]] = []
    snapshots_inserted = 0
    errors = 0
    skipped_closed = 0
    try:
        for cycle in range(1, max(cycles, 0) + 1):
            for ticker in targets:
                if client is None:
                    break
                row = _sample_with_write_retry(
                    session,
                    ticker,
                    cycle=cycle,
                    include_orderbook=include_orderbook,
                    client=client,
                    commit_after_each_sample=commit_after_each_sample,
                    write_retries=write_retries,
                    write_retry_seconds=write_retry_seconds,
                )
                rows.append(row)
                if row["status"] == "sampled":
                    snapshots_inserted += 1
                elif row["status"] == "closed":
                    skipped_closed += 1
                else:
                    errors += 1
            session.flush()
            if cycle < cycles and interval_seconds > 0:
                time.sleep(interval_seconds)
    finally:
        if owned_client is not None:
            owned_client.close()

    feature_summary = build_microstructure_features(
        session,
        lookback_minutes=lookback_minutes or resolved.microstructure_lookback_minutes,
        settings=resolved,
    )
    return MicrostructureSamplingResult(
        target_tickers=len(targets),
        cycles=cycles,
        snapshots_inserted=snapshots_inserted,
        errors=errors,
        skipped_closed=skipped_closed,
        feature_summary=feature_summary,
        rows=rows,
    )


def write_microstructure_sampling_report(
    session: Session,
    *,
    output_dir: Path = Path("reports/microstructure_sampling"),
    limit: int = 50,
    cycles: int = 3,
    interval_seconds: float = 5,
    lookback_minutes: int | None = None,
    include_orderbook: bool = True,
    settings: Settings | None = None,
    client: MicrostructureSamplingClient | None = None,
    release_read_lock_before_sampling: bool = False,
    commit_after_each_sample: bool = False,
    write_retries: int = 3,
    write_retry_seconds: float = 2,
) -> MicrostructureSamplingArtifacts:
    result = sample_microstructure_watchlist(
        session,
        limit=limit,
        cycles=cycles,
        interval_seconds=interval_seconds,
        lookback_minutes=lookback_minutes,
        include_orderbook=include_orderbook,
        settings=settings,
        client=client,
        release_read_lock_before_sampling=release_read_lock_before_sampling,
        commit_after_each_sample=commit_after_each_sample,
        write_retries=write_retries,
        write_retry_seconds=write_retry_seconds,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "microstructure_sampling_watchlist.json"
    markdown_path = output_dir / "microstructure_sampling_watchlist.md"
    rows_path = output_dir / "microstructure_sampling_watchlist_rows.json"
    payload = _payload(result)
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    rows_path.write_text(json.dumps(result.rows, indent=2, sort_keys=True), encoding="utf-8")
    markdown_path.write_text(_render_markdown(payload), encoding="utf-8")
    return MicrostructureSamplingArtifacts(result, json_path, markdown_path, rows_path)


def _sample_with_write_retry(
    session: Session,
    ticker: str,
    *,
    cycle: int,
    include_orderbook: bool,
    client: MicrostructureSamplingClient,
    commit_after_each_sample: bool,
    write_retries: int,
    write_retry_seconds: float,
) -> dict[str, Any]:
    attempts = max(write_retries, 0) + 1
    for attempt in range(1, attempts + 1):
        try:
            row = _sample_one(
                session,
                ticker,
                cycle=cycle,
                include_orderbook=include_orderbook,
                client=client,
            )
            if commit_after_each_sample:
                session.commit()
            return row
        except Exception as exc:
            if not is_database_locked_error(exc):
                session.rollback()
                return _row(
                    ticker,
                    cycle=cycle,
                    status="write_error",
                    error=str(exc),
                )
            session.rollback()
            if attempt < attempts:
                time.sleep(write_retry_seconds)
                continue
            return _row(
                ticker,
                cycle=cycle,
                status="database_busy",
                error=str(exc),
            )
    return _row(ticker, cycle=cycle, status="write_error", error="unreachable retry state")


def _sample_one(
    session: Session,
    ticker: str,
    *,
    cycle: int,
    include_orderbook: bool,
    client: MicrostructureSamplingClient,
) -> dict[str, Any]:
    try:
        market = dict(client.get_market(ticker))
    except KalshiAPIError as exc:
        return _row(ticker, cycle=cycle, status="api_error", error=str(exc))
    except Exception as exc:  # pragma: no cover - external client defensive boundary.
        return _row(ticker, cycle=cycle, status="collection_error", error=str(exc))

    market_status = str(market.get("status") or "").lower()
    if market_status in CLOSED_STATUSES:
        return _row(ticker, cycle=cycle, status="closed", market_status=market_status)

    orderbook: Mapping[str, Any] | None = None
    orderbook_error: str | None = None
    if include_orderbook:
        try:
            orderbook = client.get_orderbook(ticker)
        except Exception as exc:  # pragma: no cover - orderbook can legitimately be absent.
            orderbook_error = str(exc)

    snapshot = insert_market_snapshot(
        session,
        market,
        orderbook,
        captured_at=utc_now(),
    )
    return _row(
        ticker,
        cycle=cycle,
        status="sampled",
        market_status=market_status or "unknown",
        snapshot_id=snapshot.id,
        snapshot_at=snapshot.captured_at.isoformat(),
        error=orderbook_error,
    )


def _target_tickers(session: Session, *, limit: int) -> list[str]:
    tickers: list[str] = []
    seen: set[str] = set()
    ranking_statement = select(MarketRanking).order_by(
        desc(MarketRanking.ranked_at),
        desc(MarketRanking.opportunity_score),
        desc(MarketRanking.id),
    )
    for ranking in session.scalars(ranking_statement):
        if ranking.ticker in seen or _is_closed(session, ranking.ticker):
            continue
        seen.add(ranking.ticker)
        tickers.append(ranking.ticker)
        if len(tickers) >= limit:
            return tickers

    snapshot_statement = select(MarketSnapshot).order_by(
        desc(MarketSnapshot.captured_at),
        desc(MarketSnapshot.id),
    )
    for snapshot in session.scalars(snapshot_statement):
        if snapshot.ticker in seen or _is_closed(session, snapshot.ticker):
            continue
        seen.add(snapshot.ticker)
        tickers.append(snapshot.ticker)
        if len(tickers) >= limit:
            break
    return tickers


def _is_closed(session: Session, ticker: str) -> bool:
    market = session.get(Market, ticker)
    status = str(getattr(market, "status", "") or "").lower()
    return status in CLOSED_STATUSES


def _row(
    ticker: str,
    *,
    cycle: int,
    status: str,
    market_status: str | None = None,
    snapshot_id: int | None = None,
    snapshot_at: str | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    return {
        "ticker": ticker,
        "cycle": cycle,
        "status": status,
        "market_status": market_status,
        "snapshot_id": snapshot_id,
        "snapshot_at": snapshot_at,
        "error": error,
    }


def _payload(result: MicrostructureSamplingResult) -> dict[str, Any]:
    feature_summary = asdict(result.feature_summary)
    return {
        "generated_at": utc_now().isoformat(),
        "mode": "PAPER_ONLY_MICROSTRUCTURE_SAMPLING",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "live_or_demo_execution": False,
        "summary": {
            "target_tickers": result.target_tickers,
            "cycles": result.cycles,
            "snapshots_inserted": result.snapshots_inserted,
            "errors": result.errors,
            "skipped_closed": result.skipped_closed,
            **feature_summary,
        },
        "rows": result.rows,
    }


def _render_markdown(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    lines = [
        "# Microstructure Sampling Watchlist",
        "",
        f"- Generated at: {payload['generated_at']}",
        f"- Mode: {payload['mode']}",
        f"- Safety: {payload['paper_only_safety']}",
        "- Live/demo execution: blocked.",
        "",
        "## Summary",
        "",
    ]
    for key, value in summary.items():
        lines.append(f"- {key}: {value}")
    lines.extend(
        [
            "",
            "## Sample Rows",
            "",
            "| Cycle | Ticker | Status | Market status | Snapshot | Error |",
            "| ---: | --- | --- | --- | ---: | --- |",
        ]
    )
    for row in payload["rows"][:100]:
        lines.append(
            f"| {row['cycle']} | {row['ticker']} | {row['status']} | "
            f"{row.get('market_status') or ''} | {row.get('snapshot_id') or ''} | "
            f"{row.get('error') or ''} |"
        )
    lines.extend(
        [
            "",
            "## Recommended Next Action",
            "",
            "Run `kalshi-bot forecast --model microstructure_v1`, then "
            "`kalshi-bot microstructure-report` and `kalshi-bot microstructure-opportunities`.",
            "",
        ]
    )
    return "\n".join(lines)
