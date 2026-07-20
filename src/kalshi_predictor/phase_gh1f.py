from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Callable

import httpx

from kalshi_predictor.config import Settings
from kalshi_predictor.ingest.websocket_orderbooks import (
    ReadOnlyOrderbookWebSocketAdapter,
    websocket_auth_headers,
)
from kalshi_predictor.phase_gh1d import DEMO_REST_BASE_URL, write_gh1d_report
from kalshi_predictor.phase_gh1e import discover_quoted_demo_tickers
from kalshi_predictor.utils.time import utc_now

DiscoveryFn = Callable[..., dict[str, Any]]


class DemoReadOnlyClient:
    def __init__(self, base_url: str = DEMO_REST_BASE_URL) -> None:
        self.client = httpx.Client(base_url=base_url, timeout=15.0)

    def get_market(self, ticker: str) -> dict[str, Any]:
        response = self.client.get(f"/markets/{ticker}")
        response.raise_for_status()
        return response.json().get("market", response.json())

    def get_orderbook(self, ticker: str) -> dict[str, Any]:
        response = self.client.get(f"/markets/{ticker}/orderbook", params={"depth": 0})
        response.raise_for_status()
        return response.json()

    def close(self) -> None:
        self.client.close()


def run_gh1f_monitor(
    *,
    settings: Settings,
    output_dir: Path,
    series: list[str],
    cycles: int,
    interval_seconds: float,
    max_markets_per_series: int,
    max_quoted_per_category: int,
    stream_max_seconds: float,
    discovery_fn: DiscoveryFn = discover_quoted_demo_tickers,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    staging_dir = output_dir / "staging"
    cycle_rows: list[dict[str, Any]] = []
    comparisons_triggered = 0
    for cycle in range(1, cycles + 1):
        discovery = discovery_fn(
            series=series,
            max_markets_per_series=max_markets_per_series,
            max_quoted_per_category=max_quoted_per_category,
        )
        tickers = [str(row["ticker"]) for row in discovery.get("quoted_tickers", [])]
        cycle_row: dict[str, Any] = {
            "cycle": cycle,
            "checked_at": utc_now().isoformat(),
            "quoted_tickers": tickers,
            "comparison_triggered": False,
        }
        if tickers:
            client = DemoReadOnlyClient()
            try:
                adapter = ReadOnlyOrderbookWebSocketAdapter(
                    tickers=tickers,
                    auth_headers=websocket_auth_headers(
                        key_id=settings.kalshi_api_key_id,
                        private_key_path=Path(settings.kalshi_private_key_path),
                    ),
                    staging_dir=staging_dir,
                    rest_client=client,  # type: ignore[arg-type]
                    ws_url=settings.kalshi_websocket_url,
                    persist_every_deltas=1,
                )
                summary = asyncio.run(
                    adapter.run(max_messages=max(2, len(tickers) + 1), max_seconds=stream_max_seconds)
                )
            finally:
                client.close()
            artifacts = [Path(path) for path in summary.staged_files if Path(path).is_file()]
            if artifacts:
                comparison_path = write_gh1d_report(
                    artifacts=artifacts,
                    output_dir=output_dir / f"cycle_{cycle:03d}",
                )
                cycle_row["comparison_triggered"] = True
                cycle_row["comparison_report"] = str(comparison_path)
                cycle_row["stream_timed_out"] = summary.timed_out
                comparisons_triggered += 1
        cycle_rows.append(cycle_row)
        if cycle < cycles and interval_seconds > 0:
            time.sleep(interval_seconds)
    report = {
        "phase": "GH-1F",
        "generated_at": utc_now().isoformat(),
        "mode": "READ_ONLY_FINITE_MONITOR",
        "execution_enabled": False,
        "database_writes": 0,
        "orders_submitted": 0,
        "cycles_requested": cycles,
        "cycles_completed": len(cycle_rows),
        "comparisons_triggered": comparisons_triggered,
        "cycle_results": cycle_rows,
    }
    path = output_dir / "gh1f_demo_quote_monitor.json"
    path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    return path
