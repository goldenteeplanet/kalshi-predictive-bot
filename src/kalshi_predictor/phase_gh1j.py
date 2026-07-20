from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from kalshi_predictor.utils.time import utc_now


def write_gh1j_report(*, gh1i_report: Path, database_path: Path, output_dir: Path) -> Path:
    calibration = json.loads(gh1i_report.read_text(encoding="utf-8"))
    qualified = calibration.get("two_sided_books", [])
    rows: list[dict[str, Any]] = []
    connection = sqlite3.connect(f"file:{database_path.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        for public in qualified:
            ticker = str(public["ticker"])
            snapshot = _latest(connection, "market_snapshots", ticker, "captured_at")
            ranking = _latest(connection, "market_rankings", ticker, "ranked_at")
            opportunity = _latest(connection, "market_opportunities", ticker, "detected_at")
            risk = _latest(connection, "advanced_risk_decisions", ticker, "decision_timestamp")
            first_break = _first_break(public, snapshot, ranking, opportunity, risk)
            rows.append(
                {
                    "ticker": ticker,
                    "category": public.get("category"),
                    "calibration_ranking_advance": public["calibration"]["ranking_advance"],
                    "calibration_risk_executable_advance": public["calibration"]["risk_executable_advance"],
                    "local": {
                        "snapshot_present": snapshot is not None,
                        "snapshot_at": _value(snapshot, "captured_at"),
                        "snapshot_spread": _value(snapshot, "spread"),
                        "snapshot_has_orderbook": _has_orderbook(snapshot),
                        "ranking_present": ranking is not None,
                        "ranked_at": _value(ranking, "ranked_at"),
                        "ranking_spread": _value(ranking, "spread"),
                        "ranking_liquidity": _value(ranking, "liquidity"),
                        "opportunity_score": _value(ranking, "opportunity_score"),
                        "opportunity_present": opportunity is not None,
                        "opportunity_at": _value(opportunity, "detected_at"),
                        "opportunity_status": _value(opportunity, "status"),
                        "risk_present": risk is not None,
                        "risk_at": _value(risk, "decision_timestamp"),
                        "risk_action": _value(risk, "action"),
                    },
                    "first_funnel_break": first_break,
                }
            )
    finally:
        connection.close()
    report = {
        "phase": "GH-1J",
        "generated_at": utc_now().isoformat(),
        "mode": "SQLITE_READ_ONLY_JOIN_AUDIT",
        "execution_enabled": False,
        "database_writes": 0,
        "source_calibration_report": str(gh1i_report),
        "ticker_audits": rows,
        "summary": {
            "tickers_audited": len(rows),
            "calibration_ranking_qualified": sum(row["calibration_ranking_advance"] for row in rows),
            "local_snapshots": sum(row["local"]["snapshot_present"] for row in rows),
            "local_rankings": sum(row["local"]["ranking_present"] for row in rows),
            "local_opportunities": sum(row["local"]["opportunity_present"] for row in rows),
            "local_risk_decisions": sum(row["local"]["risk_present"] for row in rows),
            "first_break_counts": _counts(row["first_funnel_break"] for row in rows),
        },
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "gh1j_liquidity_truth_ranking_wiring_audit.json"
    path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    return path


def _latest(connection: sqlite3.Connection, table: str, ticker: str, timestamp: str) -> sqlite3.Row | None:
    return connection.execute(
        f"SELECT * FROM {table} WHERE ticker = ? ORDER BY {timestamp} DESC LIMIT 1", (ticker,)
    ).fetchone()


def _first_break(public: dict[str, Any], snapshot: sqlite3.Row | None, ranking: sqlite3.Row | None,
                 opportunity: sqlite3.Row | None, risk: sqlite3.Row | None) -> str:
    if not public["calibration"]["ranking_advance"]:
        return "PUBLIC_LIQUIDITY_THRESHOLD"
    if snapshot is None:
        return "LOCAL_SNAPSHOT_MISSING"
    if not _has_orderbook(snapshot):
        return "LOCAL_ORDERBOOK_MISSING"
    if ranking is None:
        return "RANKING_MISSING"
    if opportunity is None:
        return "OPPORTUNITY_MISSING"
    if risk is None:
        return "RISK_DECISION_MISSING"
    return "FUNNEL_EVIDENCE_COMPLETE"


def _has_orderbook(row: sqlite3.Row | None) -> bool:
    if row is None or "raw_orderbook_json" not in row.keys():
        return False
    try:
        payload = json.loads(row["raw_orderbook_json"] or "{}")
    except json.JSONDecodeError:
        return False
    container = payload.get("orderbook_fp", payload)
    return bool(container.get("yes_dollars") or container.get("no_dollars") or container.get("yes") or container.get("no"))


def _value(row: sqlite3.Row | None, key: str) -> Any:
    return row[key] if row is not None and key in row.keys() else None


def _counts(values: Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        counts[str(value)] = counts.get(str(value), 0) + 1
    return counts
