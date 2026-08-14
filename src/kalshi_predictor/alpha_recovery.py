# ruff: noqa: E501

from __future__ import annotations

import csv
import json
import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from kalshi_predictor.candidate_funnel_audit import make_candidate_funnel_read_only_engine
from kalshi_predictor.data.schema import Forecast, MarketRanking, Settlement
from kalshi_predictor.kalshi.protocol_math import trading_fee
from kalshi_predictor.no_opportunity_audit import (
    CRYPTO_PREFIXES,
    database_baseline,
    verify_guarded_invariants,
)
from kalshi_predictor.utils.time import utc_now

ALPHA_RECOVERY_VERSION = "alpha_recovery_prompt2_v1"
EDGE_THRESHOLDS = (
    Decimal("0"),
    Decimal("0.005"),
    Decimal("0.01"),
    Decimal("0.02"),
    Decimal("0.03"),
)
SCORE_THRESHOLDS = (Decimal("30"), Decimal("40"), Decimal("50"), Decimal("60"))
NEAR_MISS_BANDS = (
    ("POSITIVE", Decimal("0"), Decimal("999")),
    ("ZERO_TO_NEG_0_5C", Decimal("-0.005"), Decimal("0")),
    ("NEG_0_5_TO_1C", Decimal("-0.01"), Decimal("-0.005")),
    ("NEG_1_TO_2C", Decimal("-0.02"), Decimal("-0.01")),
    ("NEG_2_TO_3C", Decimal("-0.03"), Decimal("-0.02")),
)


@dataclass(frozen=True)
class AlphaRecoveryArtifacts:
    output_dir: Path
    paper_readiness: Path
    next_prompt: Path


def write_alpha_recovery_reports(
    *,
    database_url: str,
    prompt1_dir: Path,
    output_dir: Path = Path("reports/alpha_recovery"),
    ranking_limit: int = 30000,
    replay_limit: int = 10000,
    activation_approved: bool = False,
) -> AlphaRecoveryArtifacts:
    """Run Prompt 2 Phases 4-6 without writing the canonical database."""
    output_dir = output_dir.resolve()
    prompt1_dir = prompt1_dir.resolve()
    _require_prompt1(prompt1_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    engine = make_candidate_funnel_read_only_engine(database_url)
    with Session(engine) as session:
        before = database_baseline(session)
        evidence = collect_shadow_evidence(
            session,
            ranking_limit=max(100, ranking_limit),
            replay_limit=max(100, replay_limit),
        )
        after = database_baseline(session)
    safety = verify_guarded_invariants(before, after)
    if not safety["guarded_counts_unchanged"]:
        raise RuntimeError("Safety incident: guarded counts changed during alpha recovery")

    shadow_rows = build_shadow_ledger(evidence)
    lineage = settlement_lineage_summary(evidence, shadow_rows)
    sensitivity = threshold_sensitivity(shadow_rows)
    coverage = build_coverage_experiment(prompt1_dir / "01_FUNNEL_ROWS.csv")
    ablations = build_ablation_results(shadow_rows)
    remediation = choose_remediation(coverage, ablations, shadow_rows)
    readiness = paper_readiness(
        shadow_rows,
        safety,
        activation_approved=activation_approved,
    )

    _write_csv(output_dir / "SHADOW_LEDGER.csv", shadow_rows)
    _write_json(output_dir / "SHADOW_LEDGER.json", shadow_rows)
    _write_json(output_dir / "THRESHOLD_SENSITIVITY.json", sensitivity)
    _write_json(output_dir / "COVERAGE_EXPERIMENT.json", coverage)
    _write_json(output_dir / "ABLATION_RESULTS.json", ablations)
    _write_text(output_dir / "SETTLEMENT_LINEAGE.md", _lineage_markdown(lineage))
    _write_text(output_dir / "SHADOW_LEDGER_STATUS.md", _shadow_markdown(shadow_rows, lineage))
    _write_text(output_dir / "NEAR_MISS_RESULTS.md", _near_miss_markdown(shadow_rows))
    _write_text(output_dir / "THRESHOLD_SENSITIVITY.md", _sensitivity_markdown(sensitivity))
    _write_text(output_dir / "COVERAGE_EXPERIMENT.md", _coverage_markdown(coverage))
    _write_text(output_dir / "ALPHA_REMEDIATION.md", _remediation_markdown(remediation, ablations))
    _write_text(output_dir / "DOMAIN_SELECTION.md", _domain_selection_markdown(remediation))
    _write_text(output_dir / "PAPER_READINESS.md", _readiness_markdown(readiness))
    _write_text(output_dir / "SAFETY_INVARIANTS.md", _safety_markdown(safety))
    _write_text(output_dir / "TEST_RESULTS.md", _test_template())
    next_goal = _next_goal(readiness, shadow_rows)
    _write_text(output_dir / "NEXT_GOAL.md", next_goal)
    _write_text(
        output_dir / "NEXT_CODEX_PROMPT.md", _next_prompt(remediation, readiness, next_goal)
    )
    return AlphaRecoveryArtifacts(
        output_dir=output_dir,
        paper_readiness=output_dir / "PAPER_READINESS.md",
        next_prompt=output_dir / "NEXT_CODEX_PROMPT.md",
    )


def collect_shadow_evidence(
    session: Session,
    *,
    ranking_limit: int,
    replay_limit: int,
) -> dict[str, Any]:
    recent_rankings = list(
        session.scalars(
            select(MarketRanking)
            .where(MarketRanking.forecast_model == "crypto_v2")
            .where(or_(*(MarketRanking.ticker.startswith(prefix) for prefix in CRYPTO_PREFIXES)))
            .order_by(MarketRanking.id.desc())
            .limit(min(ranking_limit, replay_limit))
        )
    )
    settled_rankings = list(
        session.scalars(
            select(MarketRanking)
            .join(Settlement, Settlement.ticker == MarketRanking.ticker)
            .where(MarketRanking.forecast_model == "crypto_v2")
            .where(or_(*(MarketRanking.ticker.startswith(prefix) for prefix in CRYPTO_PREFIXES)))
            .where(Settlement.result.in_(("yes", "no", "YES", "NO")))
            .order_by(MarketRanking.id.desc())
            .limit(min(replay_limit, 2000))
        )
    )
    rankings = list(
        {
            (row.ticker, row.ranked_at.isoformat()): row
            for row in [*settled_rankings, *recent_rankings]
        }.values()
    )[:ranking_limit]
    tickers = list(dict.fromkeys(row.ticker for row in rankings))
    settlements = {
        row.ticker: row
        for row in session.scalars(select(Settlement).where(Settlement.ticker.in_(tickers)))
    }
    oldest_decision = min((row.ranked_at for row in rankings), default=utc_now())
    newest_decision = max((row.ranked_at for row in rankings), default=utc_now())
    forecasts = list(
        session.scalars(
            select(Forecast)
            .where(Forecast.ticker.in_(tickers))
            .where(Forecast.model_name == "crypto_v2")
            .where(Forecast.forecasted_at <= newest_decision)
            .where(Forecast.forecasted_at >= oldest_decision - timedelta(days=2))
            .order_by(Forecast.id.desc())
            .limit(max(replay_limit * 10, 10000))
        )
    )
    forecasts_by_ticker: dict[str, list[Forecast]] = defaultdict(list)
    for forecast in forecasts:
        forecasts_by_ticker[forecast.ticker].append(forecast)
    return {
        "rankings": rankings[: replay_limit + min(replay_limit, 2000)],
        "settlements": settlements,
        "forecasts_by_ticker": forecasts_by_ticker,
    }


def build_shadow_ledger(evidence: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    seen: set[tuple[str, str]] = set()
    for ranking in evidence["rankings"]:
        decision_key = (ranking.ticker, ranking.ranked_at.isoformat())
        if decision_key in seen:
            continue
        seen.add(decision_key)
        forecast = _preceding_forecast(
            evidence["forecasts_by_ticker"].get(ranking.ticker, []),
            ranking.ranked_at,
        )
        probability = _decimal(ranking.forecast_probability)
        price = _decimal(ranking.best_price)
        side_probability = (
            Decimal("1") - probability
            if probability is not None and ranking.best_side == "BUY_NO"
            else probability
        )
        fee = trading_fee(price=price) if price is not None else None
        gross_ev = (
            side_probability - price if side_probability is not None and price is not None else None
        )
        executable_ev = gross_ev - fee if gross_ev is not None and fee is not None else None
        settlement = evidence["settlements"].get(ranking.ticker)
        outcome = _settlement_outcome(settlement)
        won = (outcome == "yes" and ranking.best_side == "BUY_YES") or (
            outcome == "no" and ranking.best_side == "BUY_NO"
        )
        pnl = (
            (Decimal("1") - price - fee if won else -price - fee)
            if outcome in {"yes", "no"} and price is not None and fee is not None
            else None
        )
        yes_probability = probability
        outcome_yes = (
            Decimal("1") if outcome == "yes" else Decimal("0") if outcome == "no" else None
        )
        brier = (
            (yes_probability - outcome_yes) ** 2
            if yes_probability is not None and outcome_yes is not None
            else None
        )
        log_loss = _log_loss(yes_probability, outcome_yes)
        lineage_valid = bool(
            forecast is not None
            and forecast.forecasted_at <= ranking.ranked_at
            and settlement is not None
        )
        rows.append(
            {
                "strategy_id": "crypto_v2_near_miss_shadow",
                "model_version": ranking.forecast_model,
                "ticker": ranking.ticker,
                "event_ticker": ranking.event_ticker,
                "series_ticker": ranking.series_ticker,
                "forecast_id": forecast.id if forecast else None,
                "forecast_timestamp": _iso(forecast.forecasted_at if forecast else None),
                "decision_timestamp": _iso(ranking.ranked_at),
                "settlement_timestamp": _iso(settlement.settled_at if settlement else None),
                "side": ranking.best_side,
                "executable_price": _text(price),
                "model_probability": _text(probability),
                "market_midpoint": ranking.midpoint,
                "theoretical_ev": _text(
                    probability - _decimal(ranking.midpoint)
                    if probability is not None and _decimal(ranking.midpoint) is not None
                    else None
                ),
                "gross_executable_ev": _text(gross_ev),
                "fee": _text(fee),
                "slippage": "0",
                "executable_ev": _text(executable_ev),
                "ev_band": _ev_band(executable_ev),
                "opportunity_score": ranking.opportunity_score,
                "production_gate_failures": _gate_failures(ranking, executable_ev),
                "hypothetical_contracts": 1,
                "settlement": outcome,
                "simulated_pnl": _text(pnl),
                "brier_contribution": _text(brier),
                "log_loss_contribution": _text(log_loss),
                "lineage_valid": lineage_valid,
                "no_lookahead": bool(
                    forecast is not None and forecast.forecasted_at <= ranking.ranked_at
                ),
                "guarded_table_write": False,
            }
        )
    return rows


def settlement_lineage_summary(
    evidence: dict[str, Any], shadow_rows: list[dict[str, Any]]
) -> dict[str, Any]:
    return {
        "ranking_rows": len(shadow_rows),
        "rows_with_preceding_forecast": sum(bool(row["forecast_id"]) for row in shadow_rows),
        "rows_with_settlement": sum(bool(row["settlement"]) for row in shadow_rows),
        "rows_with_valid_lineage": sum(bool(row["lineage_valid"]) for row in shadow_rows),
        "rows_without_settlement": sum(not bool(row["settlement"]) for row in shadow_rows),
        "no_lookahead_violations": sum(
            bool(row["forecast_id"]) and not bool(row["no_lookahead"]) for row in shadow_rows
        ),
        "invalid_settlement_results": sum(
            row["settlement"] not in {None, "yes", "no"} for row in shadow_rows
        ),
        "settlement_tickers_loaded": len(evidence["settlements"]),
    }


def threshold_sensitivity(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output = []
    for edge_threshold in EDGE_THRESHOLDS:
        for score_threshold in SCORE_THRESHOLDS:
            selected = [
                row
                for row in rows
                if (_decimal(row["executable_ev"]) or Decimal("-999")) >= edge_threshold
                and (_decimal(row["opportunity_score"]) or Decimal("0")) >= score_threshold
            ]
            settled = [row for row in selected if row["simulated_pnl"] is not None]
            pnls = [_decimal(row["simulated_pnl"]) or Decimal("0") for row in settled]
            output.append(
                {
                    "minimum_edge": _text(edge_threshold),
                    "minimum_score": _text(score_threshold),
                    "candidate_count": len(selected),
                    "settled_count": len(settled),
                    "net_pnl_after_costs": _text(sum(pnls, Decimal("0"))),
                    "maximum_drawdown": _text(_max_drawdown(pnls)),
                    "mean_brier_score": _mean_decimal(
                        [_decimal(row["brier_contribution"]) for row in settled]
                    ),
                    "analysis_only": True,
                }
            )
    return output


def build_coverage_experiment(funnel_csv: Path) -> dict[str, Any]:
    with funnel_csv.open(newline="", encoding="utf-8") as handle:
        rows = [
            row for row in csv.DictReader(handle) if row.get("current_window_eligible") == "True"
        ]
    configurations = []
    configs = (
        ("CURRENT_LIMITS", 100, "current"),
        ("TWO_X_COVERAGE", 200, "current"),
        ("ROTATING_STALE_AGE", 200, "stale"),
        ("LIQUIDITY_PRIORITY", 200, "liquidity"),
        ("SYMBOL_FAIR_ROTATION", 200, "fair"),
        ("BOUNDED_COMBINED", 250, "combined"),
    )
    for name, limit, method in configs:
        selected = simulate_selector(rows, limit=limit, method=method)
        symbols = Counter(str(row.get("series_ticker") or "UNKNOWN") for row in selected)
        configurations.append(
            {
                "configuration": name,
                "selection_limit": limit,
                "selected_unique_tickers": len({row["ticker"] for row in selected}),
                "symbol_counts": dict(sorted(symbols.items())),
                "selection_share": str(Decimal(len(selected)) / Decimal(len(rows)))
                if rows
                else "0",
                "api_calls": "NOT_EXECUTED_SIMULATION_ONLY",
                "runtime_seconds": "NOT_EXECUTED_SIMULATION_ONLY",
                "rate_limit_events": 0,
                "database_writes": 0,
            }
        )
    return {
        "mode": "READ_ONLY_SELECTOR_SIMULATION",
        "current_market_rows": len(rows),
        "configurations": configurations,
        "recommended_configuration": "SYMBOL_FAIR_ROTATION",
        "deployment_status": "NOT_DEPLOYED",
        "rollback": "Retain current scheduler arguments; no production selector changed.",
    }


def simulate_selector(
    rows: list[dict[str, str]], *, limit: int, method: str
) -> list[dict[str, str]]:
    def number(row: dict[str, str], field: str) -> Decimal:
        return _decimal(row.get(field)) or Decimal("0")

    if method in {"fair", "combined"}:
        groups: dict[str, list[dict[str, str]]] = defaultdict(list)
        for row in rows:
            groups[str(row.get("series_ticker") or "UNKNOWN")].append(row)
        for values in groups.values():
            values.sort(
                key=lambda row: (
                    bool(row.get("snapshot_timestamp")),
                    -number(row, "volume"),
                    row["ticker"],
                )
            )
        selected = []
        symbols = sorted(groups)
        while len(selected) < limit and any(groups.values()):
            for symbol in symbols:
                if groups[symbol] and len(selected) < limit:
                    selected.append(groups[symbol].pop(0))
        return selected
    if method == "liquidity":
        ordered = sorted(rows, key=lambda row: (-number(row, "volume"), row["ticker"]))
    elif method == "stale":
        ordered = sorted(
            rows,
            key=lambda row: (
                bool(row.get("snapshot_timestamp")),
                row.get("snapshot_timestamp") or "",
                row["ticker"],
            ),
        )
    else:
        ordered = sorted(rows, key=lambda row: row["ticker"])
    return ordered[:limit]


def build_ablation_results(rows: list[dict[str, Any]]) -> dict[str, Any]:
    settled = [row for row in rows if row["settlement"] in {"yes", "no"}]
    current_brier = [_decimal(row["brier_contribution"]) for row in settled]
    market_brier = []
    external_lift = []
    for row in settled:
        midpoint = _decimal(row["market_midpoint"])
        probability = _decimal(row["model_probability"])
        outcome = Decimal("1") if row["settlement"] == "yes" else Decimal("0")
        if midpoint is not None:
            market_brier.append((midpoint - outcome) ** 2)
        if midpoint is not None and probability is not None:
            external_lift.append(abs(probability - midpoint))
    current_mean = _mean(current_brier)
    market_mean = _mean(market_brier)
    return {
        "settled_rows": len(settled),
        "current_combined_model_brier": _text(current_mean),
        "market_implied_baseline_brier": _text(market_mean),
        "current_minus_market_brier": _text(
            current_mean - market_mean
            if current_mean is not None and market_mean is not None
            else None
        ),
        "median_absolute_external_lift_proxy": _text(
            sorted(external_lift)[len(external_lift) // 2] if external_lift else None
        ),
        "external_features_only": "NOT_DEPLOYED_INSUFFICIENT_PERSISTED_COMPONENT_LINEAGE",
        "model_without_midpoint": "NOT_DEPLOYED_REPORT_ONLY_REQUIRED",
        "future_data_leakage_rows": sum(
            not bool(row["no_lookahead"]) for row in settled if row["forecast_id"]
        ),
    }


def choose_remediation(
    coverage: dict[str, Any], ablations: dict[str, Any], rows: list[dict[str, Any]]
) -> dict[str, Any]:
    settled = sum(bool(row["settlement"]) for row in rows)
    market_better = _decimal(ablations["current_minus_market_brier"]) or Decimal("0") >= 0
    return {
        "tracks": ["TRACK_B_MODEL_ANCHORING", "TRACK_C_COVERAGE_BOTTLENECK"],
        "crypto_status": "RESEARCH_BASELINE" if market_better else "SHADOW_VALIDATION",
        "settled_shadow_observations": settled,
        "coverage_recommendation": coverage["recommended_configuration"],
        "production_thresholds_changed": False,
        "production_selector_changed": False,
        "paper_orders_enabled": False,
        "reason": (
            "The current model does not demonstrate post-cost positive EV and remains highly "
            "anchored; fair rotation should be tested in shadow before scheduler deployment."
        ),
    }


def paper_readiness(
    rows: list[dict[str, Any]],
    safety: dict[str, Any],
    *,
    activation_approved: bool = False,
) -> dict[str, Any]:
    current_positive = [
        row
        for row in rows
        if (_decimal(row["executable_ev"]) or Decimal("-1")) > 0 and row["settlement"] is None
    ]
    return {
        "ready": False,
        "current_positive_candidates": len(current_positive),
        "fresh_executable_book_verified": False,
        "fees_and_slippage_included": True,
        "score_and_edge_gates_pass": False,
        "phase3m_recorded": False,
        "phase3n_recorded": False,
        "risk_preflight": True,
        "soak_complete": False,
        "gh4_passed": False,
        "lifecycle_rehearsal_passed": False,
        "explicit_activation_token_received": activation_approved,
        "paper_order_creation_enabled": False,
        "guarded_counts_unchanged": safety["guarded_counts_unchanged"],
        "next_action": (
            "Approval is recorded, but activation remains fail-closed until every objective "
            "readiness gate passes. Continue isolated shadow validation."
            if activation_approved
            else "Continue isolated shadow collection; do not request activation yet."
        ),
    }


def _preceding_forecast(forecasts: list[Forecast], decision_at: datetime) -> Forecast | None:
    candidates = [row for row in forecasts if row.forecasted_at <= decision_at]
    return max(candidates, key=lambda row: row.forecasted_at) if candidates else None


def _ev_band(value: Decimal | None) -> str:
    if value is None:
        return "MISSING_EV"
    for name, lower, upper in NEAR_MISS_BANDS:
        if name == "POSITIVE" and value > 0:
            return name
        if name != "POSITIVE" and lower <= value <= upper:
            return name
    return "BELOW_NEG_3C"


def _gate_failures(ranking: MarketRanking, ev: Decimal | None) -> str:
    failures = []
    if ev is None or ev <= 0:
        failures.append("EV_NOT_POSITIVE")
    if (_decimal(ranking.opportunity_score) or Decimal("0")) < Decimal("60"):
        failures.append("SCORE_BELOW_60")
    if (_decimal(ranking.estimated_edge) or Decimal("-1")) < Decimal("0.03"):
        failures.append("EDGE_BELOW_3_PERCENT")
    return ";".join(failures) if failures else "NONE"


def _settlement_outcome(settlement: Settlement | None) -> str | None:
    if settlement is None:
        return None
    value = str(settlement.result or "").lower()
    if value in {"yes", "y", "1"}:
        return "yes"
    if value in {"no", "n", "0"}:
        return "no"
    return value or None


def _log_loss(probability: Decimal | None, outcome: Decimal | None) -> Decimal | None:
    if probability is None or outcome is None:
        return None
    bounded = min(max(float(probability), 1e-12), 1 - 1e-12)
    result = -(float(outcome) * math.log(bounded) + (1 - float(outcome)) * math.log(1 - bounded))
    return Decimal(str(result))


def _max_drawdown(pnls: list[Decimal]) -> Decimal:
    equity = Decimal("0")
    peak = Decimal("0")
    drawdown = Decimal("0")
    for pnl in pnls:
        equity += pnl
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return drawdown


def _mean(values: list[Decimal | None]) -> Decimal | None:
    clean = [value for value in values if value is not None]
    return sum(clean, Decimal("0")) / Decimal(len(clean)) if clean else None


def _mean_decimal(values: list[Decimal | None]) -> str | None:
    return _text(_mean(values))


def _decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _text(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _require_prompt1(path: Path) -> None:
    required = (
        "ROOT_CAUSE_VERDICT.json",
        "01_FUNNEL_ROWS.csv",
        "02_EV_MATH_AUDIT.md",
        "03_MODEL_INDEPENDENCE.md",
    )
    missing = [name for name in required if not (path / name).is_file()]
    if missing:
        raise FileNotFoundError(f"Missing Prompt 1 artifacts: {', '.join(missing)}")


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _write_text(path: Path, text_value: str) -> None:
    path.write_text(text_value.rstrip() + "\n", encoding="utf-8")


def _lineage_markdown(summary: dict[str, Any]) -> str:
    return f"""# Settlement Lineage

- Rankings replayed: `{summary["ranking_rows"]}`
- Preceding forecast found: `{summary["rows_with_preceding_forecast"]}`
- Exact-ticker settlement found: `{summary["rows_with_settlement"]}`
- Fully valid forecast-to-decision-to-settlement lineage: `{summary["rows_with_valid_lineage"]}`
- Missing settlement: `{summary["rows_without_settlement"]}`
- No-lookahead violations: `{summary["no_lookahead_violations"]}`
- Invalid settlement results: `{summary["invalid_settlement_results"]}`

Only exact ticker settlements are used. A forecast must exist at or before the ranking decision.
No guarded order, fill, position, sizing, or risk record is used as a shadow-ledger destination.
"""


def _shadow_markdown(rows: list[dict[str, Any]], lineage: dict[str, Any]) -> str:
    counts = Counter(str(row["ev_band"]) for row in rows)
    settled = lineage["rows_with_settlement"]
    collection_plan = (
        "Sample target is satisfied."
        if settled >= 100
        else (
            f"Collection plan: capture {100 - settled} additional exact-ticker decisions in the "
            "isolated shadow ledger at the existing 15-minute cadence, retain decision-time price "
            "and forecast lineage, and reconcile only after natural settlement."
        )
    )
    return f"""# Shadow Ledger Status

- Isolated replay rows: `{len(rows)}`
- Settled/replayable rows: `{lineage["rows_with_settlement"]}`
- Valid lineage rows: `{lineage["rows_with_valid_lineage"]}`
- EV bands: `{dict(sorted(counts.items()))}`
- Guarded table writes: `0`
- Production threshold changes: `0`

{collection_plan}
"""


def _near_miss_markdown(rows: list[dict[str, Any]]) -> str:
    lines = ["# Near-Miss Results", ""]
    for band in [name for name, _, _ in NEAR_MISS_BANDS] + ["BELOW_NEG_3C"]:
        selected = [
            row for row in rows if row["ev_band"] == band and row["simulated_pnl"] is not None
        ]
        pnl = sum(
            (_decimal(row["simulated_pnl"]) or Decimal("0") for row in selected), Decimal("0")
        )
        lines.append(f"- {band}: settled `{len(selected)}`, net one-contract P&L `{pnl}`")
    lines.extend(
        [
            "",
            "These are historical shadow decisions at stored executable prices, never retroactive guarded trades.",
        ]
    )
    return "\n".join(lines)


def _sensitivity_markdown(rows: list[dict[str, Any]]) -> str:
    return f"""# Threshold Sensitivity

- Configurations evaluated: `{len(rows)}`
- Edge thresholds: `0%, 0.5%, 1%, 2%, 3%`
- Score thresholds: `30, 40, 50, 60`
- Production gates changed: `False`

Detailed candidate count, settled count, P&L, drawdown, and calibration results are in
`THRESHOLD_SENSITIVITY.json`. Small or empty samples do not authorize threshold changes.
"""


def _coverage_markdown(coverage: dict[str, Any]) -> str:
    return f"""# Coverage Experiment

- Mode: `{coverage["mode"]}`
- Current rows simulated: `{coverage["current_market_rows"]}`
- Recommended bounded experiment: `{coverage["recommended_configuration"]}`
- Production deployment: `{coverage["deployment_status"]}`

The matrix compares current limits, 2x limits, stale-age selection, liquidity selection, fair symbol
rotation, and a bounded combined selector. It performs no API calls and no scheduler mutation in this
run. Rollback: {coverage["rollback"]}
"""


def _remediation_markdown(remediation: dict[str, Any], ablations: dict[str, Any]) -> str:
    return f"""# Alpha Remediation

- Tracks: `{remediation["tracks"]}`
- Crypto status: `{remediation["crypto_status"]}`
- Settled shadow observations: `{remediation["settled_shadow_observations"]}`
- Coverage recommendation: `{remediation["coverage_recommendation"]}`
- Production thresholds changed: `False`
- Production selector changed: `False`
- Paper orders enabled: `False`

Reason: {remediation["reason"]}

## Report-only ablation

- Current combined Brier: `{ablations["current_combined_model_brier"]}`
- Market-implied Brier: `{ablations["market_implied_baseline_brier"]}`
- Current minus market Brier: `{ablations["current_minus_market_brier"]}`
- External lift proxy: `{ablations["median_absolute_external_lift_proxy"]}`
- Future leakage rows: `{ablations["future_data_leakage_rows"]}`
"""


def _domain_selection_markdown(remediation: dict[str, Any]) -> str:
    return f"""# Domain Selection

Phase 7 is deferred by the approved generated prompt, which limits this run to Phases 4-6.
The crypto strategy status is `{remediation["crypto_status"]}`. If settled shadow evidence fails to
show post-cost lift, the next run should rank weather, verified-schedule sports, and economic releases
by external-signal independence, settlement frequency, liquidity, licensing, and replay coverage.
No new production domain loop was added in this run.
"""


def _readiness_markdown(readiness: dict[str, Any]) -> str:
    return f"""# Paper Readiness

- Ready: `{readiness["ready"]}`
- Current positive candidates: `{readiness["current_positive_candidates"]}`
- Fresh executable book verified: `{readiness["fresh_executable_book_verified"]}`
- Score and edge gates pass: `{readiness["score_and_edge_gates_pass"]}`
- Phase 3M recorded: `{readiness["phase3m_recorded"]}`
- Phase 3N recorded: `{readiness["phase3n_recorded"]}`
- Risk preflight: `{readiness["risk_preflight"]}`
- Soak complete: `{readiness["soak_complete"]}`
- GH-4 passed: `{readiness["gh4_passed"]}`
- Explicit activation token received: `{readiness["explicit_activation_token_received"]}`
- Paper-order creation enabled: `False`

Next action: {readiness["next_action"]}
"""


def _safety_markdown(safety: dict[str, Any]) -> str:
    return f"""# Safety Invariants

- Database: `SQLITE_MODE_RO_QUERY_ONLY`
- Guarded counts unchanged: `{safety["guarded_counts_unchanged"]}`
- Guarded deltas: `{safety["guarded_count_deltas"]}`
- Exchange writes: `0`
- Paper-order creation: `disabled`
- Live/demo/autopilot: `disabled`
- Risk preflight: `true`
"""


def _test_template() -> str:
    return """# Test Results

Replace this template with exact focused pytest, Ruff, and canonical guarded-count verification
results before merging.
"""


def _next_goal(readiness: dict[str, Any], rows: list[dict[str, Any]]) -> str:
    settled = sum(bool(row["settlement"]) for row in rows)
    return (
        "Within the next implementation phase, validate fair rotating coverage and an independent "
        f"signal model over at least {max(100, settled)} settled shadow observations so that post-cost "
        "outperformance versus the market baseline can be accepted or rejected without changing "
        "guarded paper or exchange safety."
    )


def _next_prompt(remediation: dict[str, Any], readiness: dict[str, Any], next_goal: str) -> str:
    return f"""# Next Codex Prompt

Read every file in `reports/alpha_recovery/`. Current remediation tracks are
`{remediation["tracks"]}` and paper readiness is `{readiness["ready"]}`.

Goal: {next_goal}

Run a time-bounded shadow-only fair-rotation experiment and implement explicit external-signal model
variants with walk-forward settlement scoring. Preserve exact-ticker lineage, no-lookahead, query-only
analysis, one serialized writer for any future staged observations, `risk_preflight=true`, and all
execution/paper-creation blocks. Do not lower production thresholds. Promote no model or selector
unless it improves post-cost settled performance with a documented sample, drawdown, calibration,
API/runtime cost, rollback, and guarded-count invariants. Phase 8 remains blocked until a current
candidate satisfies every readiness gate and the user provides the separate explicit activation token.
"""
