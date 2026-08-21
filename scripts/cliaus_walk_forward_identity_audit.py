#!/usr/bin/env python3
"""Score historical CLIAUS rain forecasts and verify live contract identity."""

from __future__ import annotations

import argparse
import json
import math
import re
from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import desc, select

from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.repositories import decode_json, encode_json
from kalshi_predictor.data.schema import Forecast, MarketRanking, WeatherMarketLink
from kalshi_predictor.kalshi.client import KalshiClient
from kalshi_predictor.phase3ap import RAW_EV_COST_BUFFER
from kalshi_predictor.utils.decimals import to_decimal
from kalshi_predictor.weather.monthly_rain import apply_regularized_isotonic, fit_isotonic


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--historical", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    historical = json.loads(args.historical.read_text(encoding="utf-8"))
    scoring = score_samples(historical.get("samples", []))
    engine = init_db()
    client = KalshiClient()
    try:
        with get_session_factory(engine)() as session:
            candidates = _positive_rain_candidates(session)
            audits = []
            for candidate in candidates:
                market = client.get_market(candidate["ticker"])
                audit = audit_market(candidate, market)
                audits.append(audit)
                if audit["passed"]:
                    _persist_verification(session, audit, market)
            session.commit()
    finally:
        client.close()
        engine.dispose()
    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "scoring": scoring,
        "positive_ev_candidates": audits,
        "all_candidate_identities_passed": bool(audits) and all(row["passed"] for row in audits),
    }
    _write_atomic(args.output, payload)
    print(json.dumps(payload))
    return 0 if payload["all_candidate_identities_passed"] else 1


def score_samples(samples: list[dict]) -> dict:
    thresholds = tuple(Decimal(value) for value in range(1, 8))
    residuals: list[float] = []
    rows = []
    by_threshold = defaultdict(list)
    calibration_history = defaultdict(list)
    for sample in samples:
        predicted = Decimal(sample["predicted_total_inches"])
        actual = Decimal(sample["actual_total_inches"])
        bias = sum(residuals) / len(residuals) if residuals else 0.0
        sigma = _regularized_sigma(residuals)
        for threshold in thresholds:
            z = (float(threshold - predicted) - bias) / sigma
            raw_probability = max(0.001, min(0.999, 0.5 * math.erfc(z / math.sqrt(2))))
            outcome = 1 if actual > threshold else 0
            prior = calibration_history[str(threshold)]
            probability = (
                apply_regularized_isotonic(
                    raw_probability,
                    fit_isotonic(prior),
                    sample_count=len(prior),
                    prior_weight=320,
                )
                if len(prior) >= 12
                else raw_probability
            )
            brier = (probability - outcome) ** 2
            raw_brier = (raw_probability - outcome) ** 2
            log_loss = -(
                outcome * math.log(probability) + (1 - outcome) * math.log(1 - probability)
            )
            record = {
                "month": sample["month"],
                "threshold": str(threshold),
                "probability": probability,
                "raw_probability": raw_probability,
                "outcome": outcome,
                "brier": brier,
                "raw_brier": raw_brier,
                "log_loss": log_loss,
                "inside_80_interval": abs(float(actual - predicted) - bias) <= 1.281552 * sigma,
            }
            rows.append(record)
            by_threshold[str(threshold)].append(record)
            prior.append((raw_probability, outcome))
        residuals.append(float(actual - predicted))
    summaries = {}
    for threshold, values in by_threshold.items():
        summaries[threshold] = _metrics(values)
    bands = []
    for lower in (0.0, 0.2, 0.4, 0.6, 0.8):
        values = [row for row in rows if lower <= row["probability"] < lower + 0.2]
        if values:
            bands.append(
                {
                    "band": f"{lower:.1f}-{lower + 0.2:.1f}",
                    "n": len(values),
                    "mean_probability": sum(v["probability"] for v in values) / len(values),
                    "observed_rate": sum(v["outcome"] for v in values) / len(values),
                }
            )
    return {
        "sample_count": len(samples),
        "scored_binary_rows": len(rows),
        "no_leakage": True,
        "distribution": "PREQUENTIAL_REGULARIZED_GAUSSIAN_RESIDUAL_V1",
        "recalibration": "EXPANDING_PAV_ISOTONIC_320_PRIOR_WEIGHT_AFTER_12_SAMPLES",
        "overall": _metrics(rows),
        "by_threshold": summaries,
        "reliability_bands": bands,
    }


def _regularized_sigma(residuals: list[float]) -> float:
    prior_sigma, prior_weight = 1.5, 6
    if not residuals:
        return prior_sigma
    center = sum(residuals) / len(residuals)
    variance = sum((value - center) ** 2 for value in residuals) / max(1, len(residuals))
    return math.sqrt(
        (prior_weight * prior_sigma**2 + len(residuals) * variance)
        / (prior_weight + len(residuals))
    )


def _metrics(rows: list[dict]) -> dict:
    if not rows:
        return {"n": 0}
    return {
        "n": len(rows),
        "brier": sum(row["brier"] for row in rows) / len(rows),
        "raw_brier": sum(row["raw_brier"] for row in rows) / len(rows),
        "log_loss": sum(row["log_loss"] for row in rows) / len(rows),
        "interval_80_coverage": sum(row["inside_80_interval"] for row in rows) / len(rows),
    }


def _positive_rain_candidates(session) -> list[dict]:
    latest = {}
    for ranking in session.scalars(
        select(MarketRanking)
        .where(
            MarketRanking.ticker.like("KXRAINAUSM-%"), MarketRanking.forecast_model == "weather_v2"
        )
        .order_by(desc(MarketRanking.ranked_at), desc(MarketRanking.id))
    ):
        latest.setdefault(ranking.ticker, ranking)
    result = []
    for ticker, ranking in latest.items():
        forecast_id = decode_json(ranking.raw_json).get("forecast_id")
        forecast = session.get(Forecast, forecast_id) if forecast_id else None
        probability = to_decimal(
            forecast.yes_probability if forecast else ranking.forecast_probability
        )
        price = to_decimal(ranking.best_price)
        if probability is None or price is None:
            continue
        side_probability = (
            Decimal("1") - probability if ranking.best_side == "BUY_NO" else probability
        )
        executable_ev = (
            side_probability
            - price
            - (to_decimal(ranking.spread) or Decimal("0"))
            - RAW_EV_COST_BUFFER
        )
        if executable_ev > 0:
            result.append(
                {"ticker": ticker, "executable_ev": str(executable_ev), "ranking_id": ranking.id}
            )
    return sorted(result, key=lambda row: Decimal(row["executable_ev"]), reverse=True)[:2]


def audit_market(candidate: dict, market: dict) -> dict:
    ticker = candidate["ticker"]
    threshold = ticker.rsplit("-", 1)[-1]
    rules = " ".join(str(market.get(key) or "") for key in ("rules_primary", "rules_secondary"))
    checks = {
        "exact_ticker": market.get("ticker") == ticker,
        "exact_event": market.get("event_ticker") == "KXRAINAUSM-26AUG",
        "station_cliaus": "CLIAUS" in rules.upper(),
        "austin": "AUSTIN" in rules.upper(),
        "august_2026": bool(re.search(r"AUG(?:UST)?\s+2026", rules, re.I)),
        "strictly_greater": "STRICTLY GREATER" in rules.upper(),
        "threshold": bool(
            re.search(rf"GREATER THAN\s+{re.escape(threshold)}(?:\.0+)?\s+INCH", rules, re.I)
        ),
        "active": str(market.get("status") or "").lower() in {"active", "open"},
    }
    return {
        **candidate,
        "passed": all(checks.values()),
        "checks": checks,
        "rules_primary": market.get("rules_primary"),
    }


def _persist_verification(session, audit: dict, market: dict) -> None:
    link = session.scalar(
        select(WeatherMarketLink)
        .where(WeatherMarketLink.ticker == audit["ticker"])
        .order_by(desc(WeatherMarketLink.id))
        .limit(1)
    )
    if link is None:
        return
    raw = decode_json(link.raw_json)
    raw["exact_market_identity_verified"] = True
    raw["exact_market_identity_verified_at"] = datetime.now(UTC).isoformat()
    raw["exact_market_identity_checks"] = audit["checks"]
    raw["exact_market_rules_primary"] = market.get("rules_primary")
    link.raw_json = encode_json(raw)


def _write_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(path)


if __name__ == "__main__":
    raise SystemExit(main())
