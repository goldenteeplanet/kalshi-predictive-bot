# ruff: noqa: E501
"""Phase 7 read-only selection and settlement scoring for an independent domain."""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from kalshi_predictor.candidate_funnel_audit import make_candidate_funnel_read_only_engine
from kalshi_predictor.data.schema import (
    EconomicEvent,
    EconomicFeature,
    EconomicMarketLink,
    Forecast,
    MarketSnapshot,
    Settlement,
    SportsFeature,
    SportsGame,
    SportsMarketLink,
    SportsOdds,
    WeatherFeature,
    WeatherMarketLink,
)
from kalshi_predictor.kalshi.protocol_math import trading_fee
from kalshi_predictor.no_opportunity_audit import database_baseline, verify_guarded_invariants

PHASE7_VERSION = "independent_domain_phase7_v1"
DOMAIN_WEIGHTS = {
    "external_data_quality": Decimal("0.20"),
    "price_independence": Decimal("0.20"),
    "settlement_frequency": Decimal("0.15"),
    "historical_coverage": Decimal("0.15"),
    "liquidity": Decimal("0.10"),
    "executable_books": Decimal("0.10"),
    "modelability": Decimal("0.05"),
    "license_safety": Decimal("0.05"),
}
BASE_SCORES = {
    "weather": (9, 10, 8, 8, 6, 7, 9, 9),
    "sports": (8, 7, 10, 8, 9, 9, 8, 5),
    "economic": (9, 9, 5, 7, 8, 8, 8, 9),
}


@dataclass(frozen=True)
class Phase7Artifacts:
    output_dir: Path
    domain_ranking: Path
    readiness: Path
    next_prompt: Path


def write_independent_domain_experiment(
    *, database_url: str, output_dir: Path = Path("reports/independent_domain_experiment")
) -> Phase7Artifacts:
    """Rank supported domains and score the selected one without any database writes."""
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    engine = make_candidate_funnel_read_only_engine(database_url)
    with Session(engine) as session:
        before = database_baseline(session)
        evidence = collect_domain_evidence(session)
        ranking = rank_domains(evidence)
        selected = ranking[0]["domain"]
        ledger = score_domain_forecasts(session, selected)
        after = database_baseline(session)
    safety = verify_guarded_invariants(before, after)
    if not safety["guarded_counts_unchanged"]:
        raise RuntimeError("Safety incident: guarded counts changed during Phase 7")
    summary = summarize_ledger(ledger)
    readiness = build_readiness(selected, summary, safety)
    _write_json(output_dir / "DOMAIN_RANKING.json", {"version": PHASE7_VERSION, "ranking": ranking})
    _write_text(output_dir / "DOMAIN_RANKING.md", _ranking_markdown(ranking, selected))
    _write_csv(output_dir / "INDEPENDENT_DOMAIN_SHADOW_LEDGER.csv", ledger)
    _write_json(output_dir / "INDEPENDENT_DOMAIN_SHADOW_LEDGER.json", ledger)
    _write_text(output_dir / "INDEPENDENT_DOMAIN_EXPERIMENT.md", _experiment_markdown(selected, evidence[selected], summary))
    _write_text(output_dir / "SAFETY_INVARIANTS.md", _safety_markdown(safety))
    _write_text(output_dir / "PAPER_READINESS.md", _readiness_markdown(readiness))
    _write_text(output_dir / "NEXT_GOAL.md", _next_goal(selected, summary))
    _write_text(output_dir / "NEXT_CODEX_PROMPT.md", _next_prompt(selected, summary))
    return Phase7Artifacts(output_dir, output_dir / "DOMAIN_RANKING.md", output_dir / "PAPER_READINESS.md", output_dir / "NEXT_CODEX_PROMPT.md")


def collect_domain_evidence(session: Session) -> dict[str, dict[str, int]]:
    specs = {
        "weather": (WeatherMarketLink, WeatherFeature, None),
        "sports": (SportsMarketLink, SportsFeature, SportsGame),
        "economic": (EconomicMarketLink, EconomicFeature, EconomicEvent),
    }
    result: dict[str, dict[str, int]] = {}
    for domain, (link_model, feature_model, source_model) in specs.items():
        models = (f"{domain}_v1", f"{domain}_v2")
        forecast_count = _count(session, Forecast, Forecast.model_name.in_(models))
        settled_count = session.scalar(
            select(func.count(Forecast.id)).join(Settlement, Settlement.ticker == Forecast.ticker).where(Forecast.model_name.in_(models))
        ) or 0
        tickers = select(link_model.ticker)
        snapshot_count = session.scalar(select(func.count(MarketSnapshot.id)).where(MarketSnapshot.ticker.in_(tickers))) or 0
        result[domain] = {
            "links": _count(session, link_model),
            "features": _count(session, feature_model),
            "source_rows": _count(session, source_model) if source_model is not None else _count(session, WeatherFeature),
            "forecasts": int(forecast_count),
            "settled_forecasts": int(settled_count),
            "snapshots": int(snapshot_count),
            "odds_rows": _count(session, SportsOdds) if domain == "sports" else 0,
        }
    return result


def rank_domains(evidence: dict[str, dict[str, int]]) -> list[dict[str, Any]]:
    keys = tuple(DOMAIN_WEIGHTS)
    ranked = []
    for domain, base in BASE_SCORES.items():
        row = evidence[domain]
        evidence_bonus = min(Decimal("1"), Decimal(row["settled_forecasts"]) / Decimal("100"))
        evidence_bonus += min(Decimal("0.5"), Decimal(row["links"]) / Decimal("200"))
        scores = dict(zip(keys, map(Decimal, base), strict=True))
        weighted = sum(scores[key] * DOMAIN_WEIGHTS[key] for key in keys) + evidence_bonus
        ranked.append({"domain": domain, "score": float(weighted.quantize(Decimal("0.001"))), "component_scores": {k: int(v) for k, v in scores.items()}, "canonical_evidence": row})
    return sorted(ranked, key=lambda row: (-row["score"], row["domain"]))


def score_domain_forecasts(session: Session, domain: str) -> list[dict[str, Any]]:
    models = (f"{domain}_v1", f"{domain}_v2")
    forecasts = list(session.scalars(select(Forecast).join(Settlement, Settlement.ticker == Forecast.ticker).where(Forecast.model_name.in_(models)).order_by(Forecast.forecasted_at, Forecast.id)))
    rows = []
    for forecast in forecasts:
        settlement = session.get(Settlement, forecast.ticker)
        outcome = _outcome(settlement.result if settlement else None)
        if outcome is None:
            continue
        snapshot = session.scalar(select(MarketSnapshot).where(MarketSnapshot.ticker == forecast.ticker, MarketSnapshot.captured_at <= forecast.forecasted_at).order_by(MarketSnapshot.captured_at.desc(), MarketSnapshot.id.desc()).limit(1))
        probability = _decimal(forecast.yes_probability)
        midpoint = _decimal(forecast.market_mid_probability)
        ask = _decimal(forecast.best_yes_ask)
        if probability is None or not Decimal("0") < probability < Decimal("1"):
            continue
        side, price, edge = _executable_decision(probability, snapshot, ask)
        pnl = None
        if side and price is not None:
            fee = trading_fee(price=price, contracts=1)
            won = (side == "YES" and outcome == 1) or (side == "NO" and outcome == 0)
            pnl = (Decimal("1") if won else Decimal("0")) - price - fee
        rows.append({
            "ticker": forecast.ticker,
            "forecast_id": forecast.id,
            "model": forecast.model_name,
            "forecasted_at": forecast.forecasted_at.isoformat(),
            "snapshot_at": snapshot.captured_at.isoformat() if snapshot else None,
            "no_lookahead": bool(snapshot and snapshot.captured_at <= forecast.forecasted_at),
            "probability": str(probability),
            "market_midpoint": str(midpoint) if midpoint is not None else None,
            "outcome": outcome,
            "model_brier": float((probability - outcome) ** 2),
            "market_brier": float((midpoint - outcome) ** 2) if midpoint is not None else None,
            "side": side,
            "executable_price": str(price) if price is not None else None,
            "executable_edge": str(edge) if edge is not None else None,
            "one_contract_pnl_after_fee": str(pnl) if pnl is not None else None,
        })
    return rows


def summarize_ledger(rows: list[dict[str, Any]]) -> dict[str, Any]:
    model_brier = [Decimal(str(row["model_brier"])) for row in rows]
    market_brier = [Decimal(str(row["market_brier"])) for row in rows if row["market_brier"] is not None]
    pnl = [Decimal(row["one_contract_pnl_after_fee"]) for row in rows if row["one_contract_pnl_after_fee"] is not None]
    return {
        "settled_observations": len(rows),
        "no_lookahead_violations": sum(not row["no_lookahead"] for row in rows),
        "executable_shadow_trades": len(pnl),
        "net_pnl_after_fee": str(sum(pnl, Decimal("0"))),
        "model_brier": float(sum(model_brier) / len(model_brier)) if model_brier else None,
        "market_brier": float(sum(market_brier) / len(market_brier)) if market_brier else None,
        "outperforms_market": bool(model_brier and market_brier and (sum(model_brier) / len(model_brier)) < (sum(market_brier) / len(market_brier))),
        "positive_post_cost": bool(pnl and sum(pnl) > 0),
    }


def build_readiness(domain: str, summary: dict[str, Any], safety: dict[str, Any]) -> dict[str, Any]:
    evidence_pass = summary["settled_observations"] >= 100 and summary["no_lookahead_violations"] == 0
    performance_pass = summary["outperforms_market"] and summary["positive_post_cost"]
    return {"domain": domain, "phase7_evidence_pass": evidence_pass, "phase7_performance_pass": performance_pass, "guarded_counts_unchanged": safety["guarded_counts_unchanged"], "paper_order_creation_enabled": False, "phase8_ready": False}


def _executable_decision(probability: Decimal, snapshot: MarketSnapshot | None, forecast_ask: Decimal | None) -> tuple[str | None, Decimal | None, Decimal | None]:
    yes_ask = _decimal(snapshot.best_yes_ask) if snapshot else forecast_ask
    no_ask = _decimal(snapshot.best_no_ask) if snapshot else None
    if no_ask is None and snapshot:
        yes_bid = _decimal(snapshot.best_yes_bid)
        no_ask = Decimal("1") - yes_bid if yes_bid is not None else None
    choices = []
    if yes_ask is not None:
        choices.append(("YES", yes_ask, probability - yes_ask))
    if no_ask is not None:
        choices.append(("NO", no_ask, (Decimal("1") - probability) - no_ask))
    return max(choices, key=lambda item: item[2]) if choices else (None, None, None)


def _count(session: Session, model: Any, where: Any | None = None) -> int:
    statement = select(func.count()).select_from(model)
    return int(session.scalar(statement.where(where) if where is not None else statement) or 0)


def _decimal(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value)) if value is not None else None
    except (InvalidOperation, ValueError):
        return None


def _outcome(value: str | None) -> int | None:
    normalized = (value or "").strip().lower()
    return 1 if normalized in {"yes", "y", "1", "true"} else 0 if normalized in {"no", "n", "0", "false"} else None


def _ranking_markdown(ranking: list[dict[str, Any]], selected: str) -> str:
    lines = ["# Phase 7 Domain Ranking", "", f"Selected domain: `{selected}`", "", "| Rank | Domain | Score | Links | Features | Settled forecasts |", "|---:|---|---:|---:|---:|---:|"]
    for index, row in enumerate(ranking, 1):
        ev = row["canonical_evidence"]
        lines.append(f"| {index} | {row['domain']} | {row['score']:.3f} | {ev['links']} | {ev['features']} | {ev['settled_forecasts']} |")
    lines.extend(["", "Scores combine external-data quality, independence, settlement cadence, history, liquidity, executable books, modelability, licensing, and canonical evidence."])
    return "\n".join(lines) + "\n"


def _experiment_markdown(domain: str, evidence: dict[str, int], summary: dict[str, Any]) -> str:
    return f"""# Independent Domain Experiment\n\n- Domain: `{domain}`\n- Mode: `QUERY ONLY / SHADOW ONLY`\n- Existing domain-specific model reused: yes; crypto model reused: no\n- Links: `{evidence['links']}`\n- Features: `{evidence['features']}`\n- Forecasts: `{evidence['forecasts']}`\n- Settled observations: `{summary['settled_observations']}`\n- No-lookahead violations: `{summary['no_lookahead_violations']}`\n- Executable shadow trades: `{summary['executable_shadow_trades']}`\n- Net one-contract P&L after fees: `{summary['net_pnl_after_fee']}`\n- Model Brier: `{summary['model_brier']}`\n- Market Brier: `{summary['market_brier']}`\n- Outperforms market: `{summary['outperforms_market']}`\n- Positive post-cost performance: `{summary['positive_post_cost']}`\n\nNo model, selector, threshold, scheduler, or execution setting was promoted.\n"""


def _safety_markdown(safety: dict[str, Any]) -> str:
    return f"# Safety Invariants\n\n- Canonical DB query-only: `True`\n- Guarded counts unchanged: `{safety['guarded_counts_unchanged']}`\n- Paper/live/demo/autopilot execution enabled: `False`\n- Paper-order creation enabled: `False`\n"


def _readiness_markdown(readiness: dict[str, Any]) -> str:
    return "# Paper Readiness\n\n" + "\n".join(f"- {key}: `{value}`" for key, value in readiness.items()) + "\n\nPhase 8 remains fail-closed. Prior approval does not bypass objective gates.\n"


def _next_goal(domain: str, summary: dict[str, Any]) -> str:
    missing = max(0, 100 - summary["settled_observations"])
    return f"Collect and walk-forward score at least {missing} additional settled `{domain}` observations (minimum total 100) and accept promotion only if post-cost P&L and market-relative calibration are both positive while guarded counts remain unchanged.\n"


def _next_prompt(domain: str, summary: dict[str, Any]) -> str:
    return f"""# Next Codex Prompt\n\nRead every file in `reports/independent_domain_experiment/`. Phase 7 selected `{domain}` with {summary['settled_observations']} settled observations. Continue shadow collection and walk-forward validation only. Preserve exact-ticker lineage, no-lookahead, query-only analysis, all execution blocks, production thresholds, and guarded-count invariants. Do not enter Phase 8 unless at least 100 settled observations show both positive post-cost P&L and better calibration than the market and every existing readiness gate independently passes.\n"""


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_text(path: Path, payload: str) -> None:
    path.write_text(payload, encoding="utf-8")


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = list(rows[0]) if rows else ["ticker", "forecast_id", "model", "forecasted_at"]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
