from __future__ import annotations

import json
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from kalshi_predictor.data.schema import (
    CanonicalEvaluation,
    Forecast,
    Market,
    MarketRanking,
    ResearchRun,
)
from kalshi_predictor.phase4cd.evidence import (
    evidence_dashboard,
    model_evidence,
    proposed_model_weights,
)
from kalshi_predictor.phase4cd.rights import RIGHTS_GATED_CORPORA, corpus_access_status
from kalshi_predictor.utils.time import utc_now


def ensemble_audit(session: Session, *, model: str = "ensemble_v2") -> dict[str, Any]:
    forecast = session.scalar(
        select(Forecast)
        .where(Forecast.model_name == model)
        .order_by(Forecast.forecasted_at.desc(), Forecast.id.desc())
        .limit(1)
    )
    components: list[dict[str, Any]] = []
    if forecast:
        try:
            payload = json.loads(forecast.feature_json or "{}")
        except json.JSONDecodeError:
            payload = {}
        raw_components = payload.get("components") or payload.get("ensemble_components") or []
        if isinstance(raw_components, dict):
            raw_components = [
                {"model": name, **(value if isinstance(value, dict) else {"probability": value})}
                for name, value in raw_components.items()
            ]
        for row in raw_components:
            probability = _decimal(row.get("probability"))
            weight = _decimal(row.get("weight"))
            included = probability is not None and weight is not None and weight > 0
            contribution = None
            if probability is not None and weight is not None and weight > 0:
                contribution = str(probability * weight)
            components.append(
                {
                    "component_model": row.get("model") or row.get("name") or "unknown",
                    "probability": str(probability) if probability is not None else None,
                    "weight": str(weight) if weight is not None else None,
                    "weighted_contribution": contribution,
                    "reason": "INCLUDED" if included else "MISSING_PROBABILITY_OR_WEIGHT",
                }
            )
    effective = sum(
        1
        for row in components
        if row["reason"] == "INCLUDED" and Decimal(row["weight"]) >= Decimal("0.05")
    )
    market_only = effective <= 1 and any(
        row["component_model"] == "market_implied_v1" and row["reason"] == "INCLUDED"
        for row in components
    )
    return {
        "model": model,
        "forecast_id": forecast.id if forecast else None,
        "components": components,
        "effective_model_count": effective,
        "effectively_market_implied_only": market_only,
        "statement": (
            "The ensemble is effectively only market implied."
            if market_only
            else "No effective ensemble can be claimed from missing components."
            if effective == 0
            else f"The ensemble has {effective} effective components."
        ),
    }


def fast_settlement_candidates(session: Session, *, limit: int = 100) -> dict[str, Any]:
    now = utc_now()
    rows = list(
        session.execute(
            select(Market, MarketRanking)
            .join(MarketRanking, MarketRanking.ticker == Market.ticker)
            .where(Market.close_time > now, Market.close_time <= now + timedelta(hours=72))
            .order_by(MarketRanking.ranked_at.desc())
            .limit(limit * 5)
        )
    )
    seen: set[str] = set()
    candidates = []
    series_counts: defaultdict[str, int] = defaultdict(int)
    for market, ranking in rows:
        if market.ticker in seen:
            continue
        seen.add(market.ticker)
        hours = max((market.close_time - now).total_seconds() / 3600, 0.01)
        net_ev = _decimal(ranking.estimated_edge) or Decimal("0")
        liquidity = _decimal(ranking.liquidity) or Decimal("0")
        evidence_quality = _decimal(ranking.model_confidence_score) or Decimal("0")
        settlement_speed = Decimal(str(1 / hours))
        diversity = Decimal("1") / Decimal(1 + series_counts[market.series_ticker or "UNKNOWN"])
        score = net_ev * liquidity * evidence_quality * settlement_speed * diversity
        if net_ev <= 0 or liquidity <= 0:
            continue
        series_counts[market.series_ticker or "UNKNOWN"] += 1
        candidates.append(
            {
                "ticker": market.ticker,
                "event_ticker": market.event_ticker,
                "series_ticker": market.series_ticker,
                "bucket": _settlement_bucket(hours),
                "hours_to_close": round(hours, 2),
                "net_executable_ev": str(net_ev),
                "liquidity": str(liquidity),
                "evidence_quality": str(evidence_quality),
                "diversity": str(diversity),
                "score": str(score),
            }
        )
    candidates.sort(key=lambda row: Decimal(row["score"]), reverse=True)
    return {"generated_at": now.isoformat(), "candidates": candidates[:limit]}


def write_phase4cd_reports(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase4cd"),
    baseline: dict[str, Any] | None = None,
) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    dashboard = evidence_dashboard(session)
    models = model_evidence(session)
    ensemble = ensemble_audit(session)
    fast = fast_settlement_candidates(session)
    weights = proposed_model_weights(session)
    runs = list(session.scalars(select(ResearchRun).order_by(ResearchRun.started_at.desc())))
    replay = {
        "runs": [_run_payload(row) for row in runs],
        "historical": dashboard["historical"],
    }
    crypto = _category_evidence(session, "crypto")
    weather = _category_evidence(session, "weather")
    shadow = dashboard["shadow"]
    throughput = _throughput(runs, dashboard)
    rights = {name: corpus_access_status(name) for name in sorted(RIGHTS_GATED_CORPORA)}
    payloads = {
        "baseline.md": baseline or {"status": "BASELINE_NOT_SUPPLIED"},
        "historical_replay.md": replay,
        "crypto_evidence.md": crypto,
        "weather_evidence.md": weather,
        "shadow_evidence.md": shadow,
        "model_evidence.md": {"models": models, "weights": weights},
        "ensemble_audit.md": ensemble,
        "fast_settlement.md": fast,
        "training_throughput.md": throughput,
    }
    paths = []
    for name, payload in payloads.items():
        path = output_dir / name
        path.write_text(_markdown(name.removesuffix(".md"), payload), encoding="utf-8")
        paths.append(path)
    final = output_dir / "FINAL_PHASE_4CD_REPORT.md"
    final.write_text(
        _markdown(
            "FINAL_PHASE_4CD_REPORT",
            {
                "evidence": dashboard,
                "models": models,
                "ensemble": ensemble,
                "throughput": throughput,
                "rights": rights,
                "safety": {
                    "live_execution": "DISABLED",
                    "automatic_paper_cohort": "DISABLED",
                    "gh2_soak": "UNMODIFIED",
                    "rights_pending_corpora": "NOT_INGESTED",
                },
            },
        ),
        encoding="utf-8",
    )
    paths.append(final)
    return paths


def _category_evidence(session: Session, category: str) -> dict[str, Any]:
    rows = list(
        session.scalars(
            select(CanonicalEvaluation).where(
                CanonicalEvaluation.series_ticker.ilike(f"%{category}%")
            )
        )
    )
    by_model: defaultdict[str, list[CanonicalEvaluation]] = defaultdict(list)
    for row in rows:
        by_model[row.model].append(row)
    return {
        "category": category,
        "models": {
            model: {
                "evaluated": len(values),
                "independent_event_n": len({row.independent_event_id for row in values}),
                "brier": str(
                    sum((Decimal(row.brier_contribution) for row in values), Decimal("0"))
                    / len(values)
                ),
                "net_executable_result": str(
                    sum(
                        (Decimal(row.realized_or_simulated_pnl) for row in values),
                        Decimal("0"),
                    )
                ),
            }
            for model, values in by_model.items()
        },
        "failure_labels": [
            "MODEL_MISCALIBRATION",
            "SPREAD_CONSUMES_EDGE",
            "FEES_CONSUME_EDGE",
            "WEAK_SEGMENT",
            "INSUFFICIENT_SAMPLE",
            "BOOK_ALIGNMENT",
            "FORECAST_CAPTURE_LATENCY",
        ],
    }


def _throughput(runs: list[ResearchRun], dashboard: dict[str, Any]) -> dict[str, Any]:
    seconds = sum(max((row.updated_at - row.started_at).total_seconds(), 0) for row in runs)
    evaluated = sum(row.evaluated for row in runs)
    return {
        "replay_evaluations_per_min": evaluated / (seconds / 60) if seconds else None,
        "replay_independent_events_per_hour": (
            dashboard["historical"]["independent_events"] / (seconds / 3600) if seconds else None
        ),
        "shadow_decisions_per_day": dashboard["shadow"].get("decisions", 0),
        "shadow_settlements_per_day": dashboard["shadow"].get("settled", 0),
        "fast_settlement_candidates_per_day": None,
    }


def _run_payload(row: ResearchRun) -> dict[str, Any]:
    return {
        "run_id": row.run_id,
        "model": row.model,
        "category": row.category,
        "status": row.status,
        "processed": row.processed,
        "evaluated": row.evaluated,
        "skipped": row.skipped,
        "errors": row.errors,
        "started_at": row.started_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
    }


def _markdown(title: str, payload: Any) -> str:
    heading = title.replace("_", " ").title()
    body = json.dumps(payload, indent=2, default=str)
    return f"# {heading}\n\n```json\n{body}\n```\n"


def _decimal(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value)) if value is not None else None
    except (InvalidOperation, ValueError):
        return None


def _settlement_bucket(hours: float) -> str:
    if hours <= 6:
        return "<=6h"
    if hours <= 12:
        return "6-12h"
    if hours <= 24:
        return "12-24h"
    if hours <= 48:
        return "24-48h"
    return "48-72h"
