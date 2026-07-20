from __future__ import annotations

import json
import sqlite3
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

from kalshi_predictor.config import Settings
from kalshi_predictor.opportunities.payout_scoring import calculate_payout_metrics
from kalshi_predictor.opportunities.scoring import score_edge
from kalshi_predictor.utils.decimals import to_decimal
from kalshi_predictor.utils.time import utc_now

ZERO = Decimal("0")
MIN_EXECUTABLE_LIQUIDITY_SCORE = Decimal("30")


def attribute_ranking(row: Mapping[str, Any], *, settings: Settings) -> dict[str, Any]:
    metrics = calculate_payout_metrics(
        side=row.get("best_side"), yes_probability=row.get("forecast_probability"),
        cost=row.get("best_price"), edge=row.get("estimated_edge"),
        liquidity_score=row.get("liquidity_score"), spread_score=row.get("spread_score"),
        confidence_score=row.get("model_confidence_score"), time_score=row.get("time_score"),
    )
    edge = to_decimal(row.get("estimated_edge")) or ZERO
    score = to_decimal(row.get("opportunity_score")) or ZERO
    liquidity = to_decimal(row.get("liquidity_score")) or ZERO
    spread = to_decimal(row.get("spread"))
    components = {
        "expected_value": (metrics.expected_value_score * Decimal("0.30")),
        "edge": (score_edge(edge) * Decimal("0.25")),
        "liquidity": (liquidity * Decimal("0.15")),
        "spread": ((to_decimal(row.get("spread_score")) or ZERO) * Decimal("0.15")),
        "confidence": ((to_decimal(row.get("model_confidence_score")) or ZERO) * Decimal("0.10")),
        "time": ((to_decimal(row.get("time_score")) or ZERO) * Decimal("0.05")),
    }
    blockers: list[str] = []
    if edge < settings.opportunity_min_edge:
        blockers.append("EDGE_BELOW_MINIMUM")
    if score < settings.opportunity_min_score:
        blockers.append("OPPORTUNITY_SCORE_BELOW_MINIMUM")
    if spread is not None and spread > settings.opportunity_max_spread:
        blockers.append("SPREAD_ABOVE_MAXIMUM")
    if liquidity < MIN_EXECUTABLE_LIQUIDITY_SCORE:
        blockers.append("LIQUIDITY_SCORE_BELOW_EXECUTABLE")
    if not row.get("best_side") or not row.get("best_price"):
        blockers.append("EXECUTABLE_SIDE_OR_PRICE_MISSING")
    gate_pass = not blockers
    return {
        "ticker": row.get("ticker"),
        "stored_opportunity_score": str(score),
        "recomposed_score": str(sum(components.values(), ZERO)),
        "expected_value": str(metrics.expected_value) if metrics.expected_value is not None else None,
        "estimated_edge": str(edge),
        "liquidity_score": str(liquidity),
        "spread": str(spread) if spread is not None else None,
        "components": {key: str(value) for key, value in components.items()},
        "blockers": blockers,
        "first_blocker": blockers[0] if blockers else None,
        "opportunity_gate_pass": gate_pass,
        "risk_gate_pass": gate_pass,
    }


def write_gh1m_report(*, gh1j_report: Path, database_path: Path, settings: Settings,
                      output_dir: Path) -> Path:
    audit = json.loads(gh1j_report.read_text(encoding="utf-8"))
    tickers = [row["ticker"] for row in audit["ticker_audits"] if row["calibration_ranking_advance"]]
    attributed: list[dict[str, Any]] = []
    connection = sqlite3.connect(f"file:{database_path.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        for ticker in tickers:
            row = connection.execute(
                "SELECT * FROM market_rankings WHERE ticker=? ORDER BY ranked_at DESC LIMIT 1", (ticker,)
            ).fetchone()
            if row is not None:
                attributed.append(attribute_ranking(dict(row), settings=settings))
    finally:
        connection.close()
    report = {
        "phase": "GH-1M", "generated_at": utc_now().isoformat(),
        "mode": "SQLITE_READ_ONLY_GATE_RECHECK", "execution_enabled": False,
        "database_writes": 0, "thresholds_changed": False,
        "thresholds": {"min_edge": str(settings.opportunity_min_edge),
                       "min_score": str(settings.opportunity_min_score),
                       "max_spread": str(settings.opportunity_max_spread),
                       "min_executable_liquidity_score": str(MIN_EXECUTABLE_LIQUIDITY_SCORE)},
        "ticker_attribution": attributed,
        "summary": {"tickers_attributed": len(attributed),
                    "opportunity_gate_pass": sum(row["opportunity_gate_pass"] for row in attributed),
                    "risk_gate_pass": sum(row["risk_gate_pass"] for row in attributed),
                    "first_blocker_counts": _counts(row["first_blocker"] for row in attributed)},
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "gh1m_opportunity_score_attribution.json"
    path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    return path


def _counts(values: Any) -> dict[str, int]:
    result: dict[str, int] = {}
    for value in values:
        key = str(value or "NONE")
        result[key] = result.get(key, 0) + 1
    return result
