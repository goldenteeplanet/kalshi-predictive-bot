# ruff: noqa: E501
"""Weather settlement-lineage and shadow-performance validation.

The audit is deliberately query-only. Authoritative settlement ingestion remains owned by the
existing serialized ``sync-settlements`` writer; this module never manufactures outcomes.
"""

from __future__ import annotations

import csv
import json
import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from kalshi_predictor.candidate_funnel_audit import make_candidate_funnel_read_only_engine
from kalshi_predictor.data.repositories import decode_json
from kalshi_predictor.data.schema import (
    Forecast,
    ForecastSkipLog,
    Market,
    MarketSnapshot,
    Settlement,
    WeatherFeature,
    WeatherForecast,
    WeatherMarketLink,
)
from kalshi_predictor.kalshi.protocol_math import trading_fee
from kalshi_predictor.no_opportunity_audit import database_baseline, verify_guarded_invariants
from kalshi_predictor.utils.time import parse_datetime, utc_now

VALID_EXACT_SETTLEMENT = "VALID_EXACT_TICKER_SETTLEMENT"
MARKET_RESULT_MISSING_ROW = "MARKET_RESULT_AVAILABLE_SETTLEMENT_ROW_MISSING"
UNRESOLVED = "UNRESOLVED_OR_UNEXPIRED_MARKET"
IDENTITY_MISMATCH = "TICKER_IDENTITY_MISMATCH"
INVALID_RESULT = "INVALID_OR_AMBIGUOUS_RESULT"
DUPLICATE_WINDOW = "DUPLICATE_FORECAST_WINDOW"
WEATHER_MODELS = ("weather_v1", "weather_v2")


@dataclass(frozen=True)
class WeatherValidationArtifacts:
    output_dir: Path
    gap_audit: Path
    readiness: Path
    next_prompt: Path


def write_weather_alpha_validation(
    *, database_url: str, output_dir: Path = Path("reports/weather_alpha_validation")
) -> WeatherValidationArtifacts:
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    engine = make_candidate_funnel_read_only_engine(database_url)
    with Session(engine) as session:
        before = database_baseline(session)
        gap_rows = classify_weather_forecasts(session)
        ledger = build_weather_shadow_ledger(gap_rows)
        runtime_health = collect_runtime_health(session)
        after = database_baseline(session)
    safety = verify_guarded_invariants(before, after)
    if not safety["guarded_counts_unchanged"]:
        raise RuntimeError("Safety incident: guarded counts changed during weather validation")
    performance = performance_summary(ledger)
    calibration = calibration_rows(ledger)
    by_location = grouped_performance(ledger, "location_key")
    by_contract = grouped_performance(ledger, "contract_type")
    by_horizon = grouped_performance(ledger, "horizon_bucket")
    health = pipeline_health(gap_rows, ledger, runtime_health=runtime_health)
    readiness = paper_readiness(performance, safety)

    _write_csv(output_dir / "SETTLEMENT_GAP_ROWS.csv", gap_rows)
    _write_text(output_dir / "SETTLEMENT_GAP_AUDIT.md", gap_markdown(gap_rows))
    _write_csv(output_dir / "WEATHER_SHADOW_LEDGER.csv", ledger)
    _write_json(output_dir / "WEATHER_SHADOW_LEDGER.json", ledger)
    _write_text(output_dir / "WALK_FORWARD_RESULTS.md", walk_forward_markdown(ledger, performance))
    _write_csv(output_dir / "CALIBRATION_BY_BUCKET.csv", calibration)
    _write_csv(output_dir / "PERFORMANCE_BY_LOCATION.csv", by_location)
    _write_csv(output_dir / "PERFORMANCE_BY_CONTRACT.csv", by_contract)
    _write_csv(output_dir / "PERFORMANCE_BY_HORIZON.csv", by_horizon)
    _write_text(output_dir / "PIPELINE_HEALTH.md", health_markdown(health))
    _write_text(
        output_dir / "COLLECTION_PLAN.md",
        collection_plan_markdown(performance, health),
    )
    _write_text(output_dir / "PAPER_READINESS.md", readiness_markdown(readiness))
    _write_text(output_dir / "SAFETY_INVARIANTS.md", safety_markdown(safety))
    _write_text(output_dir / "TEST_RESULTS.md", "# Test Results\n\nPending final verification.\n")
    _write_text(output_dir / "NEXT_GOAL.md", next_goal(performance))
    _write_text(output_dir / "NEXT_CODEX_PROMPT.md", next_prompt(performance))
    return WeatherValidationArtifacts(
        output_dir,
        output_dir / "SETTLEMENT_GAP_AUDIT.md",
        output_dir / "PAPER_READINESS.md",
        output_dir / "NEXT_CODEX_PROMPT.md",
    )


def classify_weather_forecasts(session: Session) -> list[dict[str, Any]]:
    forecasts = list(
        session.scalars(
            select(Forecast)
            .where(Forecast.model_name.in_(WEATHER_MODELS))
            .order_by(Forecast.forecasted_at, Forecast.id)
        )
    )
    seen_windows: set[tuple[str, str, datetime]] = set()
    rows: list[dict[str, Any]] = []
    for forecast in forecasts:
        market = session.get(Market, forecast.ticker)
        settlement = session.get(Settlement, forecast.ticker)
        link = session.scalar(
            select(WeatherMarketLink)
            .where(
                WeatherMarketLink.ticker == forecast.ticker,
                WeatherMarketLink.detected_at <= forecast.forecasted_at,
            )
            .order_by(WeatherMarketLink.detected_at.desc(), WeatherMarketLink.id.desc())
            .limit(1)
        )
        snapshot = session.scalar(
            select(MarketSnapshot)
            .where(
                MarketSnapshot.ticker == forecast.ticker,
                MarketSnapshot.captured_at <= forecast.forecasted_at,
            )
            .order_by(MarketSnapshot.captured_at.desc(), MarketSnapshot.id.desc())
            .limit(1)
        )
        feature, source_timestamp = _feature_lineage(session, forecast, link)
        key = (forecast.ticker, forecast.model_name, forecast.forecasted_at)
        classification = classify_lineage(
            duplicate=key in seen_windows,
            has_market=market is not None,
            has_link=link is not None,
            settlement_result=settlement.result if settlement else None,
            market_result=market.result if market else None,
        )
        seen_windows.add(key)
        source_at = parse_datetime(source_timestamp)
        feature_at = parse_datetime(feature.generated_at if feature else None)
        snapshot_at = parse_datetime(snapshot.captured_at if snapshot else None)
        forecast_at = parse_datetime(forecast.forecasted_at)
        lineage_ok = timestamps_in_order(source_at, feature_at, snapshot_at, forecast_at)
        rows.append(
            {
                "forecast_id": forecast.id,
                "ticker": forecast.ticker,
                "model": forecast.model_name,
                "forecasted_at": forecast.forecasted_at.isoformat(),
                "market_status": market.status if market else None,
                "market_result": market.result if market else None,
                "market_close_time": market.close_time.isoformat()
                if market and market.close_time
                else None,
                "settlement_result": settlement.result if settlement else None,
                "settled_at": settlement.settled_at.isoformat()
                if settlement and settlement.settled_at
                else None,
                "classification": classification,
                "authoritative_repair_candidate": classification == MARKET_RESULT_MISSING_ROW,
                "location_key": link.location_key if link else None,
                "contract_type": link.weather_metric if link else None,
                "target_operator": link.target_operator if link else None,
                "target_time": link.target_time.isoformat() if link and link.target_time else None,
                "weather_link_id": link.id if link else None,
                "weather_feature_id": feature.id if feature else None,
                "source_timestamp": source_at.isoformat() if source_at else None,
                "feature_timestamp": feature_at.isoformat() if feature_at else None,
                "snapshot_id": snapshot.id if snapshot else None,
                "snapshot_timestamp": snapshot_at.isoformat() if snapshot_at else None,
                "lineage_timestamps_valid": lineage_ok,
                "lineage_blocker": lineage_blocker(source_at, feature_at, snapshot_at, forecast_at),
                "yes_probability": forecast.yes_probability,
                "market_midpoint": forecast.market_mid_probability,
                "yes_bid": snapshot.best_yes_bid if snapshot else forecast.best_yes_bid,
                "yes_ask": snapshot.best_yes_ask if snapshot else forecast.best_yes_ask,
                "no_bid": snapshot.best_no_bid if snapshot else None,
                "no_ask": snapshot.best_no_ask if snapshot else None,
                "spread": snapshot.spread if snapshot else None,
            }
        )
    return rows


def classify_lineage(
    *,
    duplicate: bool,
    has_market: bool,
    has_link: bool,
    settlement_result: str | None,
    market_result: str | None,
) -> str:
    if duplicate:
        return DUPLICATE_WINDOW
    if not has_market or not has_link:
        return IDENTITY_MISMATCH
    settlement = _outcome(settlement_result)
    if settlement is not None:
        return VALID_EXACT_SETTLEMENT
    if settlement_result not in (None, ""):
        return INVALID_RESULT
    market = _outcome(market_result)
    if market is not None:
        return MARKET_RESULT_MISSING_ROW
    if market_result not in (None, ""):
        return INVALID_RESULT
    return UNRESOLVED


def timestamps_in_order(
    source: datetime | None,
    feature: datetime | None,
    snapshot: datetime | None,
    forecast: datetime | None,
) -> bool:
    if None in (source, feature, snapshot, forecast):
        return False
    assert source and feature and snapshot and forecast
    return source <= feature <= snapshot <= forecast


def lineage_blocker(
    source: datetime | None,
    feature: datetime | None,
    snapshot: datetime | None,
    forecast: datetime | None,
) -> str | None:
    if source is None:
        return "SOURCE_TIMESTAMP_MISSING"
    if feature is None:
        return "FEATURE_TIMESTAMP_MISSING"
    if snapshot is None:
        return "PRE_FORECAST_SNAPSHOT_MISSING"
    if forecast is None:
        return "FORECAST_TIMESTAMP_MISSING"
    if source > feature:
        return "SOURCE_AFTER_FEATURE"
    if feature > snapshot:
        return "FEATURE_AFTER_SNAPSHOT"
    if snapshot > forecast:
        return "SNAPSHOT_AFTER_FORECAST"
    return None


def build_weather_shadow_ledger(gap_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ledger = []
    for row in gap_rows:
        if row["classification"] != VALID_EXACT_SETTLEMENT or not row["lineage_timestamps_valid"]:
            continue
        probability = _decimal(row["yes_probability"])
        midpoint = _decimal(row["market_midpoint"])
        outcome = _outcome(row["settlement_result"])
        if probability is None or outcome is None or not Decimal("0") < probability < Decimal("1"):
            continue
        side, price, edge = executable_decision(probability, row)
        fee = trading_fee(price=price, contracts=1) if price is not None else None
        pnl = None
        if side and price is not None and fee is not None:
            won = (side == "YES" and outcome == 1) or (side == "NO" and outcome == 0)
            pnl = (Decimal("1") if won else Decimal("0")) - price - fee
        forecast_at = parse_datetime(row["forecasted_at"])
        target_at = parse_datetime(row["target_time"])
        horizon_hours = (
            (target_at - forecast_at).total_seconds() / 3600 if target_at and forecast_at else None
        )
        feature_at = parse_datetime(row["feature_timestamp"])
        feature_age = (
            (forecast_at - feature_at).total_seconds() / 3600
            if forecast_at and feature_at
            else None
        )
        ledger.append(
            {
                **row,
                "strategy_version": "weather_shadow_v1",
                "outcome": outcome,
                "side": side,
                "executable_price": str(price) if price is not None else None,
                "fee": str(fee) if fee is not None else None,
                "executable_edge": str(edge) if edge is not None else None,
                "one_contract_pnl_after_fee": str(pnl) if pnl is not None else None,
                "model_brier": str(brier_score(probability, outcome)),
                "market_brier": str(brier_score(midpoint, outcome))
                if midpoint is not None
                else None,
                "model_log_loss": str(log_loss(probability, outcome)),
                "market_log_loss": str(log_loss(midpoint, outcome))
                if midpoint is not None
                else None,
                "horizon_hours": horizon_hours,
                "horizon_bucket": horizon_bucket(horizon_hours),
                "feature_age_hours": feature_age,
            }
        )
    return ledger


def executable_decision(
    probability: Decimal, row: dict[str, Any]
) -> tuple[str | None, Decimal | None, Decimal | None]:
    yes_ask = _decimal(row.get("yes_ask"))
    no_ask = _decimal(row.get("no_ask"))
    if no_ask is None:
        yes_bid = _decimal(row.get("yes_bid"))
        no_ask = Decimal("1") - yes_bid if yes_bid is not None else None
    choices = []
    if yes_ask is not None:
        choices.append(("YES", yes_ask, probability - yes_ask))
    if no_ask is not None:
        choices.append(("NO", no_ask, Decimal("1") - probability - no_ask))
    return max(choices, key=lambda item: item[2]) if choices else (None, None, None)


def brier_score(probability: Decimal, outcome: int) -> Decimal:
    return (probability - Decimal(outcome)) ** 2


def log_loss(probability: Decimal, outcome: int) -> Decimal:
    bounded = min(Decimal("0.999999"), max(Decimal("0.000001"), probability))
    value = -(math.log(float(bounded)) if outcome else math.log(float(Decimal("1") - bounded)))
    return Decimal(str(value))


def maximum_drawdown(pnls: list[Decimal]) -> Decimal:
    equity = peak = Decimal("0")
    maximum = Decimal("0")
    for pnl in pnls:
        equity += pnl
        peak = max(peak, equity)
        maximum = max(maximum, peak - equity)
    return maximum


def performance_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    pnls = [_decimal(row["one_contract_pnl_after_fee"]) for row in rows]
    valid_pnls = [value for value in pnls if value is not None]
    return {
        "settled_observations": len(rows),
        "no_lookahead_violations": sum(not row["lineage_timestamps_valid"] for row in rows),
        "model_brier": _mean(rows, "model_brier"),
        "market_brier": _mean(rows, "market_brier"),
        "model_log_loss": _mean(rows, "model_log_loss"),
        "market_log_loss": _mean(rows, "market_log_loss"),
        "net_pnl_after_fee": str(sum(valid_pnls, Decimal("0"))),
        "maximum_drawdown": str(maximum_drawdown(valid_pnls)),
        "outperforms_market": _less(_mean(rows, "model_brier"), _mean(rows, "market_brier")),
        "positive_post_cost": bool(valid_pnls and sum(valid_pnls) > 0),
    }


def calibration_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        probability = float(row["yes_probability"])
        low = min(9, int(probability * 10)) * 10
        buckets[f"{low:02d}-{low + 10:02d}%"].append(row)
    return [
        {
            "probability_bucket": key,
            "count": len(group),
            "mean_forecast": sum(float(r["yes_probability"]) for r in group) / len(group),
            "observed_yes_rate": sum(r["outcome"] for r in group) / len(group),
            "model_brier": _mean(group, "model_brier"),
        }
        for key, group in sorted(buckets.items())
    ]


def grouped_performance(rows: list[dict[str, Any]], field: str) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[str(row.get(field) or "UNKNOWN")].append(row)
    return [
        {
            field: key,
            "count": len(group),
            "model_brier": _mean(group, "model_brier"),
            "market_brier": _mean(group, "market_brier"),
            "net_pnl_after_fee": str(
                sum(
                    (_decimal(r["one_contract_pnl_after_fee"]) or Decimal("0") for r in group),
                    Decimal("0"),
                )
            ),
        }
        for key, group in sorted(groups.items())
    ]


def collect_runtime_health(session: Session) -> dict[str, Any]:
    from kalshi_predictor.forecasting.registry import latest_snapshots_for_model

    latest_feature = session.scalar(
        select(WeatherFeature).order_by(WeatherFeature.id.desc()).limit(1)
    )
    latest_source = session.scalar(
        select(WeatherForecast).order_by(WeatherForecast.id.desc()).limit(1)
    )
    recent_skips = list(
        session.scalars(
            select(ForecastSkipLog)
            .where(ForecastSkipLog.model_name == "weather_v2")
            .order_by(ForecastSkipLog.id.desc())
            .limit(100)
        )
    )
    current_verified_snapshots = latest_snapshots_for_model(
        session,
        model_name="weather_v2",
        limit=500,
    )
    skip_families = Counter(
        "VERIFIED_KXTEMPNYCH"
        if row.ticker.startswith("KXTEMPNYCH-")
        else "UNSUPPORTED_WEATHER_FAMILY"
        for row in recent_skips
    )
    return {
        "latest_global_feature_generated_at": (
            latest_feature.generated_at.isoformat() if latest_feature else None
        ),
        "latest_weather_source_generated_at": (
            latest_source.forecast_generated_at.isoformat() if latest_source else None
        ),
        "latest_weather_source_target_time": (
            latest_source.forecast_time.isoformat() if latest_source else None
        ),
        "recent_forecast_skip_reasons": dict(Counter(row.reason for row in recent_skips)),
        "recent_forecast_skip_families": dict(skip_families),
        "current_verified_family_snapshots": len(current_verified_snapshots or []),
    }


def pipeline_health(
    gap_rows: list[dict[str, Any]],
    ledger: list[dict[str, Any]],
    *,
    runtime_health: dict[str, Any] | None = None,
) -> dict[str, Any]:
    now = utc_now()

    def latest(key: str) -> datetime | None:
        values = [parse_datetime(row.get(key)) for row in gap_rows]
        return max((value for value in values if value is not None), default=None)

    def age(value: datetime | None) -> float | None:
        return (now - value).total_seconds() / 3600 if value else None

    counts = Counter(row["classification"] for row in gap_rows)
    return {
        "forecast_count": len(gap_rows),
        "unique_tickers": len({row["ticker"] for row in gap_rows}),
        "settled_shadow_rows": len(ledger),
        "settlement_coverage": len(ledger) / len(gap_rows) if gap_rows else 0,
        "classifications": dict(counts),
        "latest_scored_source_age_hours": age(latest("source_timestamp")),
        "latest_scored_feature_age_hours": age(latest("feature_timestamp")),
        "latest_snapshot_age_hours": age(latest("snapshot_timestamp")),
        "latest_forecast_age_hours": age(latest("forecasted_at")),
        "repair_candidates": counts[MARKET_RESULT_MISSING_ROW],
        **(runtime_health or {}),
    }


def paper_readiness(performance: dict[str, Any], safety: dict[str, Any]) -> dict[str, Any]:
    evidence = (
        performance["settled_observations"] >= 100 and performance["no_lookahead_violations"] == 0
    )
    return {
        "settled_sample_gate": evidence,
        "market_relative_calibration_gate": performance["outperforms_market"],
        "post_cost_pnl_gate": performance["positive_post_cost"],
        "guarded_counts_unchanged": safety["guarded_counts_unchanged"],
        "fresh_current_executable_candidate_verified": False,
        "phase3m_phase3n_soak_gh4_lifecycle_gates": False,
        "paper_order_creation_enabled": False,
        "phase8_ready": False,
    }


def _feature_lineage(
    session: Session, forecast: Forecast, link: WeatherMarketLink | None
) -> tuple[WeatherFeature | None, Any]:
    payload = decode_json(forecast.feature_json)
    feature_id = payload.get("weather_feature_id") or payload.get("feature_snapshot_id")
    feature = (
        session.get(WeatherFeature, int(feature_id)) if str(feature_id or "").isdigit() else None
    )
    if feature is None and link is not None:
        feature = session.scalar(
            select(WeatherFeature)
            .where(
                WeatherFeature.location_key == link.location_key,
                WeatherFeature.generated_at <= forecast.forecasted_at,
            )
            .order_by(WeatherFeature.generated_at.desc(), WeatherFeature.id.desc())
            .limit(1)
        )
    raw = decode_json(feature.raw_json) if feature else {}
    source_timestamp = (
        raw.get("forecast_generated_at")
        or raw.get("source_timestamp")
        or (feature.generated_at if feature else None)
    )
    return feature, source_timestamp


def horizon_bucket(hours: float | None) -> str:
    if hours is None:
        return "UNKNOWN"
    if hours <= 6:
        return "0-6H"
    if hours <= 24:
        return "6-24H"
    if hours <= 72:
        return "24-72H"
    return "72H+"


def gap_markdown(rows: list[dict[str, Any]]) -> str:
    counts = Counter(row["classification"] for row in rows)
    lines = [
        "# Weather Settlement Gap Audit",
        "",
        f"- Forecast rows classified: `{len(rows)}`",
        f"- Unique tickers: `{len({r['ticker'] for r in rows})}`",
        f"- Authoritative repair candidates: `{counts[MARKET_RESULT_MISSING_ROW]}`",
        "",
    ]
    lines.extend(
        f"- {name}: `{counts[name]}`"
        for name in (
            VALID_EXACT_SETTLEMENT,
            MARKET_RESULT_MISSING_ROW,
            UNRESOLVED,
            IDENTITY_MISMATCH,
            INVALID_RESULT,
            DUPLICATE_WINDOW,
        )
    )
    lines.extend(
        [
            "",
            "No settlement is inferred from price. Zero authoritative market results means there is nothing safe to reconcile locally; the existing serialized `sync-settlements` job remains the only settlement writer.",
        ]
    )
    return "\n".join(lines) + "\n"


def walk_forward_markdown(rows: list[dict[str, Any]], summary: dict[str, Any]) -> str:
    dates = Counter(str(row["forecasted_at"])[:10] for row in rows)
    return (
        "# Weather Walk-Forward Results\n\n"
        + "\n".join(f"- {key}: `{value}`" for key, value in summary.items())
        + "\n\n## Settled observations by forecast date\n\n"
        + (
            "\n".join(f"- {date}: `{count}`" for date, count in sorted(dates.items()))
            or "No valid settled observations yet."
        )
        + "\n"
    )


def health_markdown(health: dict[str, Any]) -> str:
    return (
        "# Weather Pipeline Health\n\n"
        + "\n".join(f"- {key}: `{value}`" for key, value in health.items())
        + "\n"
    )


def collection_plan_markdown(performance: dict[str, Any], health: dict[str, Any]) -> str:
    missing = max(0, 100 - performance["settled_observations"])
    snapshots = int(health.get("current_verified_family_snapshots") or 0)
    blocker = (
        "VERIFIED_FAMILY_CURRENT_MARKET_UNIVERSE_EMPTY"
        if snapshots == 0
        else "VERIFIED_FAMILY_FEATURE_ALIGNMENT_OR_FRESHNESS"
    )
    return f"""# Weather Shadow Collection Plan\n\n- Additional settled observations required: `{missing}`\n- Current verified-family snapshots: `{snapshots}`\n- Latest forecast skip reasons: `{health.get("recent_forecast_skip_reasons", {})}`\n- Latest skip families: `{health.get("recent_forecast_skip_families", {})}`\n- Current primary blocker: `{blocker}`\n- Writer: existing shared `flock`-serialized weather refresh and `sync-settlements` commands only\n- Analytics: `kalshi-bot weather-alpha-validation` (query-only)\n- Cadence: catalog/source/features every 15 minutes only while verified KXTEMPNYCH markets exist; authoritative settlement sync after natural resolution; validation after sync\n- Promotion: prohibited until 100+ valid rows, zero lookahead, better market-relative calibration, positive post-cost P&L, acceptable drawdown, and all Phase 8 gates\n- Unsupported hurricane contracts: excluded from weather_v2; never remap them to a city or temperature feature\n- Performance repair: use the indexed current-weather snapshot selector; do not restore the full-history grouped query\n- Rollback: stop the weather shadow timer and revert the verified-family selector commit; no threshold, paper-order, or exchange state is changed\n\nUse `scripts/weather-alpha-shadow-cycle.sh` for the fail-closed collection sequence.\n"""


def readiness_markdown(readiness: dict[str, Any]) -> str:
    return (
        "# Paper Readiness\n\n"
        + "\n".join(f"- {key}: `{value}`" for key, value in readiness.items())
        + "\n\nPhase 8 remains blocked. Prior approval cannot bypass objective gates.\n"
    )


def safety_markdown(safety: dict[str, Any]) -> str:
    return f"# Safety Invariants\n\n- Analytical database mode: `QUERY ONLY`\n- Guarded counts unchanged: `{safety['guarded_counts_unchanged']}`\n- Paper/live/demo/autopilot enabled: `False`\n- Paper-order creation enabled: `False`\n- Retroactive paper orders: `0`\n- Exchange writes: `0`\n"


def next_goal(performance: dict[str, Any]) -> str:
    return f"Collect {max(0, 100 - performance['settled_observations'])} additional exact-ticker settled weather observations and demonstrate zero lookahead, better market-relative calibration, and positive post-cost shadow P&L without changing guarded paper or exchange safety.\n"


def next_prompt(performance: dict[str, Any]) -> str:
    return f"# Next Codex Prompt\n\nRead every file in `reports/weather_alpha_validation/`. Current valid settled weather observations: `{performance['settled_observations']}`. Operate the existing serialized weather shadow collector and authoritative settlement sync, diagnose pipeline-health regressions, and rerun walk-forward scoring. Do not lower thresholds, infer outcomes, create retroactive paper orders, or enable execution. Phase 8 remains blocked until 100+ observations and every performance, freshness, sizing, risk, soak, GH-4, lifecycle, and integrity gate passes.\n"


def _mean(rows: list[dict[str, Any]], key: str) -> float | None:
    values = [_decimal(row.get(key)) for row in rows]
    valid = [value for value in values if value is not None]
    return float(sum(valid) / len(valid)) if valid else None


def _less(left: float | None, right: float | None) -> bool:
    return left is not None and right is not None and left < right


def _outcome(value: str | None) -> int | None:
    normalized = (value or "").strip().lower()
    return (
        1
        if normalized in {"yes", "y", "1", "true"}
        else 0
        if normalized in {"no", "n", "0", "false"}
        else None
    )


def _decimal(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value)) if value is not None else None
    except (InvalidOperation, ValueError):
        return None


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_text(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = list(rows[0]) if rows else ["ticker", "forecast_id"]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
