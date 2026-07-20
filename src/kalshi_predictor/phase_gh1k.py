from __future__ import annotations

import json
import sqlite3
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx

from kalshi_predictor.kalshi.orderbook import LocalOrderbook
from kalshi_predictor.opportunities.scoring import score_liquidity
from kalshi_predictor.phase_gh1h import PRODUCTION_PUBLIC_REST_URL
from kalshi_predictor.utils.decimals import to_decimal
from kalshi_predictor.utils.time import utc_now


def depth_notional(book: LocalOrderbook, *, levels: int = 5) -> Decimal:
    yes = sorted(book.yes.items(), reverse=True)[:levels]
    no = sorted(book.no.items(), reverse=True)[:levels]
    return sum((price * quantity for price, quantity in yes + no), Decimal("0"))


def preview_liquidity_score(*, volume: Any, open_interest: Any, market_liquidity: Any,
                            orderbook_depth_notional: Decimal) -> dict[str, str]:
    existing = max(to_decimal(market_liquidity) or Decimal("0"), Decimal("0"))
    proposed_input = max(existing, orderbook_depth_notional)
    return {
        "current_score": str(score_liquidity(volume=volume, open_interest=open_interest, liquidity=existing)),
        "current_market_liquidity_input": str(existing),
        "orderbook_depth_notional": str(orderbook_depth_notional),
        "preview_liquidity_input": str(proposed_input),
        "preview_score": str(score_liquidity(volume=volume, open_interest=open_interest, liquidity=proposed_input)),
    }


def write_gh1k_report(*, gh1j_report: Path, database_path: Path, output_dir: Path,
                      rest_base_url: str = PRODUCTION_PUBLIC_REST_URL) -> Path:
    audit = json.loads(gh1j_report.read_text(encoding="utf-8"))
    crypto = [row for row in audit["ticker_audits"] if row["category"] == "crypto"]
    weather = [row for row in audit["ticker_audits"] if row["category"] == "weather"
               and row["calibration_ranking_advance"] and row["first_funnel_break"] == "LOCAL_SNAPSHOT_MISSING"]
    output_dir.mkdir(parents=True, exist_ok=True)
    staging_dir = output_dir / "weather_snapshot_staging"
    staging_dir.mkdir(parents=True, exist_ok=True)
    repairs: list[dict[str, Any]] = []
    staged: list[str] = []
    connection = sqlite3.connect(f"file:{database_path.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        with httpx.Client(base_url=rest_base_url, timeout=15.0) as client:
            for item in crypto:
                ticker = item["ticker"]
                snapshot = connection.execute(
                    "SELECT * FROM market_snapshots WHERE ticker=? ORDER BY captured_at DESC LIMIT 1", (ticker,)
                ).fetchone()
                ranking = connection.execute(
                    "SELECT * FROM market_rankings WHERE ticker=? ORDER BY ranked_at DESC LIMIT 1", (ticker,)
                ).fetchone()
                response = client.get(f"/markets/{ticker}/orderbook", params={"depth": 5})
                response.raise_for_status()
                book = LocalOrderbook(ticker)
                book.apply_rest_snapshot(response.json(), resume_sequence=0)
                raw_market = json.loads(snapshot["raw_market_json"] or "{}") if snapshot else {}
                scoring = preview_liquidity_score(
                    volume=snapshot["volume_fp"] if snapshot else None,
                    open_interest=snapshot["open_interest_fp"] if snapshot else None,
                    market_liquidity=raw_market.get("liquidity_dollars"),
                    orderbook_depth_notional=depth_notional(book),
                )
                repairs.append({
                    "ticker": ticker,
                    "stored_ranking_liquidity_score": ranking["liquidity_score"] if ranking else None,
                    **scoring,
                    "trace": "Ranking liquidity uses market volume, open interest, and liquidity_dollars; orderbook depth is not an input.",
                    "repair_action": "PREVIEW_ONLY_USE_MAX_MARKET_LIQUIDITY_OR_TOP5_ORDERBOOK_NOTIONAL",
                    "database_write": False,
                })
            for item in weather:
                ticker = item["ticker"]
                market_response = client.get(f"/markets/{ticker}")
                market_response.raise_for_status()
                book_response = client.get(f"/markets/{ticker}/orderbook", params={"depth": 0})
                book_response.raise_for_status()
                payload = {
                    "category": "gh1k_weather_snapshot_preview",
                    "ticker": ticker,
                    "staged_at": utc_now().isoformat(),
                    "market": market_response.json().get("market", market_response.json()),
                    "orderbook": book_response.json(),
                    "safety": {"filesystem_stage_only": True, "database_write": False,
                               "execution_enabled": False, "orders_submitted": 0},
                }
                path = staging_dir / f"{ticker}.json"
                path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
                staged.append(str(path))
    finally:
        connection.close()
    report = {
        "phase": "GH-1K", "generated_at": utc_now().isoformat(),
        "mode": "FILESYSTEM_PREVIEW_ONLY", "execution_enabled": False, "database_writes": 0,
        "trace_result": "ORDERBOOK_DEPTH_NOT_CURRENTLY_USED_BY_RANKING_LIQUIDITY_SCORE",
        "crypto_repair_preview": repairs, "weather_staged_files": staged,
        "summary": {"crypto_rows_previewed": len(repairs), "weather_rows_staged": len(staged),
                    "crypto_scores_changed_in_preview": sum(row["current_score"] != row["preview_score"] for row in repairs)},
    }
    path = output_dir / "gh1k_snapshot_staging_liquidity_repair_preview.json"
    path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    return path
