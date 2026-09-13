"""Bounded public GET monitoring for the isolated local settlement ledger.

Entry authorization and kill switches are deliberately absent: stopping new
positions must not stop monitoring existing positions. Durable, atomic watcher
receipts make re-running after a crash safe; this runner never creates orders.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

import httpx
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from kalshi_predictor.overnight_paper.watcher import (
    MAX_MARKETS,
    MAX_PAYLOAD_BYTES,
    PublicMarketObservation,
    WatcherReport,
    pending_dataset_tickers,
    reconcile_public_settlements,
)

PUBLIC_BASE = "https://external-api.kalshi.com/trade-api/v2/markets/"
MAX_PENDING_SHADOW_MARKETS = 10_000


@dataclass(frozen=True)
class SettlementRunReport:
    status: str
    cycles_completed: int
    reports: tuple[WatcherReport, ...]
    deferred_shadow_count: int = 0

    @property
    def deferred_nonpaper_market_count(self) -> int:
        """Includes dataset-only observations; old field retained for compatibility."""
        return self.deferred_shadow_count


def _now() -> datetime:
    return datetime.now(UTC)


def _tracked(session_factory: sessionmaker[Session], path: Path) -> tuple[tuple[str, ...], int]:
    with session_factory() as session:
        databases = session.execute(text("PRAGMA database_list")).all()
        if (
            len([row for row in databases if row[1] == "main"]) != 1
            or any(row[1] not in {"main", "temp"} for row in databases)
            or any(Path(row[2]).resolve() != path for row in databases if row[1] == "main")
        ):
            raise ValueError("DATABASE_PATH_MISMATCH")
        paper = tuple(
            session.execute(
                text(
                    "SELECT DISTINCT ticker FROM overnight_shadow "
                    "WHERE paper_order_id IS NOT NULL ORDER BY ticker LIMIT :limit"
                ),
                {"limit": MAX_MARKETS + 1},
            ).scalars()
        )
        if len(paper) > MAX_MARKETS:
            raise ValueError("WATCHER_LINKED_PAPER_MARKET_LIMIT")
        pending_query = (
            "FROM overnight_shadow AS shadow WHERE shadow.evaluation_json IS NULL "
            "AND NOT EXISTS (SELECT 1 FROM overnight_shadow AS linked "
            "WHERE linked.ticker=shadow.ticker AND linked.paper_order_id IS NOT NULL)"
        )
        shadow_pending = set(
            session.execute(
                text(
                    "SELECT DISTINCT shadow.ticker "
                    + pending_query
                    + " ORDER BY shadow.ticker LIMIT :limit"
                ),
                {"limit": MAX_PENDING_SHADOW_MARKETS + 1},
            ).scalars()
        )
        if len(shadow_pending) > MAX_PENDING_SHADOW_MARKETS:
            raise ValueError("WATCHER_PENDING_VALIDATION_BUDGET_EXCEEDED")
        all_pending = (shadow_pending | set(pending_dataset_tickers(session))) - set(paper)
        pending_count = len(all_pending)
        pending = tuple(sorted(all_pending)[: MAX_MARKETS - len(paper)])
    tickers = paper + pending
    if any(not isinstance(ticker, str) or not ticker for ticker in tickers):
        raise ValueError("WATCHER_TRACKED_TICKER_INVALID")
    return tickers, pending_count - len(pending)


def _observe(
    client: httpx.Client, ticker: str, clock: Callable[[], datetime]
) -> PublicMarketObservation:
    url = PUBLIC_BASE + quote(ticker, safe="")
    # Never follow redirects, load account settings, or attach authentication.
    with client.stream("GET", url, headers={"Accept": "application/json"}) as response:
        response.raise_for_status()
        chunks: list[bytes] = []
        size = 0
        for chunk in response.iter_bytes():
            size += len(chunk)
            if size > MAX_PAYLOAD_BYTES:
                raise ValueError("PUBLIC_MARKET_PAYLOAD_LIMIT")
            chunks.append(chunk)
        raw = b"".join(chunks)
    received = clock()
    observation = PublicMarketObservation(
        ticker, url, received, hashlib.sha256(raw).hexdigest(), raw
    )
    observation.market(now=received)
    return observation


def run_settlement_cycles(
    *,
    session_factory: sessionmaker[Session],
    database_path: Path,
    cycles: int = 1,
    interval_seconds: int = 60,
    transport: httpx.BaseTransport | None = None,
    clock: Callable[[], datetime] = _now,
    sleep: Callable[[float], None] = time.sleep,
    stop_requested: Callable[[], bool] = lambda: False,
) -> SettlementRunReport:
    """Poll at most three tracked tickers and 60 cycles; fail closed on errors.

    A supplied transport/clock/sleep is for deterministic fixture tests. No
    account client or credentials are accepted. Each public artifact is handed
    unchanged to the existing authoritative finality/identity verifier. Network
    errors and reconciliation conflicts propagate; earlier committed batches
    remain resumable without duplicating P&L. A restart re-fetches public data.

    This is a bounded foreground worker, not proof of a running background
    service or paper activation. Linked paper tickers always take priority and
    remain tracked after evaluation to detect corrections. Only unevaluated
    shadow-only and dataset-only tickers fill remaining capacity; deferred counts
    include both categories and are explicit.
    """
    if (
        type(cycles) is not int
        or not 1 <= cycles <= 60
        or type(interval_seconds) is not int
        or interval_seconds != 60
    ):
        raise ValueError("INVALID_SETTLEMENT_RUN_LIMITS")
    path = database_path.resolve(strict=True)
    if "onedrive" in str(path).lower() or any(
        item.is_symlink() or getattr(item, "is_junction", lambda: False)()
        for item in (database_path, *database_path.parents)
    ):
        raise ValueError("ISOLATED_UNLINKED_DATABASE_REQUIRED")
    reports: list[WatcherReport] = []
    deferred = 0
    with httpx.Client(
        transport=transport,
        trust_env=False,
        follow_redirects=False,
        timeout=httpx.Timeout(10),
    ) as client:
        for index in range(cycles):
            if stop_requested():
                return SettlementRunReport("STOPPED", len(reports), tuple(reports), deferred)
            tickers, deferred = _tracked(session_factory, path)
            if not tickers:
                return SettlementRunReport("NO_TRACKED_MARKETS", len(reports), tuple(reports))
            observations = tuple(_observe(client, ticker, clock) for ticker in tickers)
            report = reconcile_public_settlements(
                session_factory=session_factory,
                database_path=path,
                observations=observations,
                now=clock(),
            )
            reports.append(report)
            if deferred == 0 and all(
                row["state"] in {"PAPER_EVALUATED", "SHADOW_EVALUATED", "DATASET_OUTCOME_RECORDED"}
                for row in report.rows
            ):
                return SettlementRunReport(
                    "TRACKED_SETTLEMENTS_EVALUATED", len(reports), tuple(reports)
                )
            if index + 1 < cycles:
                sleep(interval_seconds)
    status = (
        "BOUNDED_MONITORING_WITH_DEFERRED_SHADOWS" if deferred else "BOUNDED_MONITORING_COMPLETE"
    )
    return SettlementRunReport(status, len(reports), tuple(reports), deferred)
