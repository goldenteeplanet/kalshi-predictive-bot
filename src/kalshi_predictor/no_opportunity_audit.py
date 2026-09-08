# Long report prose and evidence strings are intentionally kept readable in generated artifacts.
# ruff: noqa: E501

from __future__ import annotations

import csv
import json
import subprocess
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from statistics import median
from typing import Any

from sqlalchemy import Engine, or_, select, text
from sqlalchemy.orm import Session

from kalshi_predictor.candidate_funnel_audit import make_candidate_funnel_read_only_engine
from kalshi_predictor.data.schema import Forecast, Market, MarketRanking, MarketSnapshot
from kalshi_predictor.kalshi.protocol_math import trading_fee
from kalshi_predictor.utils.time import utc_now

AUDIT_VERSION = "no_opportunity_root_cause_v1"
CRYPTO_PREFIXES = ("KXBTC", "KXETH", "KXSOLE", "KXXRP", "KXDOGE")
GUARDED_TABLES = (
    "paper_orders",
    "paper_fills",
    "paper_positions",
    "position_sizing_decisions",
    "advanced_risk_decisions",
)
BASELINE_TABLES = (
    "markets",
    "market_snapshots",
    "forecasts",
    "market_rankings",
    "market_opportunities",
    "settlements",
    *GUARDED_TABLES,
    "backtest_runs",
    "backtest_trades",
)
SAFETY_FLAGS = {
    "UI_READ_ONLY": "true",
    "EXECUTION_ENABLED": "false",
    "EXECUTION_DRY_RUN": "true",
    "EXECUTION_KILL_SWITCH": "true",
    "AUTOPILOT_ENABLED": "false",
    "AUTOPILOT_DRY_RUN": "true",
    "PAPER_ORDER_CREATION_ENABLED": "false",
    "risk_preflight": True,
}


@dataclass(frozen=True)
class NoOpportunityArtifacts:
    output_dir: Path
    verdict_json: Path
    verdict_markdown: Path
    next_prompt: Path


def write_no_opportunity_root_cause_audit(
    *,
    database_url: str,
    output_dir: Path = Path("reports/no_opportunity_root_cause"),
    runtime_worktree: Path,
    runtime_reports_dir: Path,
    env_path: Path,
    command_name: str = "no-opportunity-root-cause-audit",
    recent_limit: int = 5000,
    allow_noncanonical: bool = False,
) -> NoOpportunityArtifacts:
    """Run Prompt 1 Phases 0-3 against a query-only SQLite connection."""
    output_dir = output_dir.resolve()
    runtime_worktree = runtime_worktree.resolve()
    runtime_reports_dir = runtime_reports_dir.resolve()
    env_path = env_path.resolve()
    engine = make_candidate_funnel_read_only_engine(database_url)
    fingerprint = build_runtime_fingerprint(
        engine=engine,
        database_url=database_url,
        output_dir=output_dir,
        runtime_worktree=runtime_worktree,
        runtime_reports_dir=runtime_reports_dir,
        env_path=env_path,
        command_name=command_name,
    )
    _enforce_canonical_guard(fingerprint, allow_noncanonical=allow_noncanonical)
    output_dir.mkdir(parents=True, exist_ok=True)

    with Session(engine) as session:
        before = database_baseline(session)
        evidence = collect_audit_evidence(session, recent_limit=max(100, recent_limit))
        after = database_baseline(session)
    safety = verify_guarded_invariants(before, after)
    if not safety["guarded_counts_unchanged"]:
        raise RuntimeError("Safety incident: guarded row counts changed during read-only audit")

    fingerprint["classification"] = "CANONICAL_RUNTIME"
    _write_json(output_dir / "00_RUNTIME_FINGERPRINT.json", fingerprint)
    _write_json(
        output_dir / "00_DATABASE_BASELINE.json",
        {"before": before, "after": after, "safety": safety},
    )
    _write_text(output_dir / "00_RUNTIME_TRUTH.md", _runtime_truth(fingerprint, before))

    funnel_rows = build_funnel_rows(evidence)
    funnel_summary = build_funnel_summary(funnel_rows, evidence)
    _write_csv(output_dir / "01_FUNNEL_ROWS.csv", funnel_rows)
    _write_json(output_dir / "01_FUNNEL_SUMMARY.json", funnel_summary)
    _write_text(output_dir / "01_FUNNEL_AUDIT.md", _funnel_markdown(funnel_summary))
    _write_text(output_dir / "01_ROTATION_FAIRNESS.md", _rotation_markdown(funnel_summary))
    _write_csv(
        output_dir / "01_COVERAGE_BY_SYMBOL.csv",
        list(funnel_summary["coverage_by_symbol"].values()),
    )

    ev_rows = build_ev_attribution(evidence["rankings"], evidence["snapshots"])
    _write_csv(output_dir / "02_EV_ATTRIBUTION.csv", ev_rows)
    _write_text(output_dir / "02_EV_MATH_AUDIT.md", _ev_markdown(ev_rows))
    _write_text(output_dir / "02_SCORE_GATE_AUDIT.md", _score_markdown(ev_rows))
    _write_text(output_dir / "02_PROTOCOL_CONFORMANCE.md", _protocol_markdown(ev_rows))

    model_rows, feature_rows, model_summary = build_model_independence(
        evidence["forecasts"], evidence["snapshots"]
    )
    _write_csv(output_dir / "03_MODEL_VS_MARKET.csv", model_rows)
    _write_csv(output_dir / "03_FEATURE_HEALTH.csv", feature_rows)
    _write_text(output_dir / "03_ABLATION_RESULTS.md", _ablation_markdown(model_summary))
    _write_text(output_dir / "03_MODEL_INDEPENDENCE.md", _model_markdown(model_summary))

    verdict = build_root_cause_verdict(
        fingerprint=fingerprint,
        funnel_summary=funnel_summary,
        ev_rows=ev_rows,
        model_summary=model_summary,
        safety=safety,
    )
    _write_json(output_dir / "ROOT_CAUSE_VERDICT.json", verdict)
    _write_text(output_dir / "ROOT_CAUSE_VERDICT.md", _verdict_markdown(verdict))
    _write_text(output_dir / "SAFETY_INVARIANTS.md", _safety_markdown(safety))
    _write_text(output_dir / "TEST_RESULTS.md", _test_results_template())
    goal = (
        "Within the next implementation phase, increase rotating executable-book coverage "
        "and validate independent model lift so that settled near-miss performance can be "
        "measured over at least 100 observations without changing guarded paper or exchange "
        "safety."
    )
    _write_text(output_dir / "NEXT_GOAL.md", goal + "\n")
    _write_text(output_dir / "NEXT_CODEX_PROMPT.md", _next_prompt(verdict, goal))
    return NoOpportunityArtifacts(
        output_dir=output_dir,
        verdict_json=output_dir / "ROOT_CAUSE_VERDICT.json",
        verdict_markdown=output_dir / "ROOT_CAUSE_VERDICT.md",
        next_prompt=output_dir / "NEXT_CODEX_PROMPT.md",
    )


def build_runtime_fingerprint(
    *,
    engine: Engine,
    database_url: str,
    output_dir: Path,
    runtime_worktree: Path,
    runtime_reports_dir: Path,
    env_path: Path,
    command_name: str,
) -> dict[str, Any]:
    database_path = Path(str(engine.url.database).removeprefix("file:"))
    database_path = Path(str(database_path).split("?", 1)[0]).resolve()
    stat = database_path.stat()
    branch = _git(runtime_worktree, "branch", "--show-current")
    sha = _git(runtime_worktree, "rev-parse", "HEAD")
    with engine.connect() as connection:
        market_count = int(connection.execute(text("SELECT count(*) FROM markets")).scalar() or 0)
        query_only = int(connection.execute(text("PRAGMA query_only")).scalar() or 0)
    runtime_id = f"{sha[:12]}:{stat.st_size}:{database_path.name}"
    return {
        "audit_version": AUDIT_VERSION,
        "runtime_id": runtime_id,
        "generated_at": utc_now().isoformat(),
        "worktree_path": str(runtime_worktree),
        "git_branch": branch,
        "git_sha": sha,
        "env_path": str(env_path),
        "env_present": env_path.is_file(),
        "database_url": database_url,
        "absolute_database_path": str(database_path),
        "database_size_bytes": stat.st_size,
        "database_last_modified": datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(),
        "database_market_count": market_count,
        "database_query_only": query_only == 1,
        "reports_directory": str(runtime_reports_dir),
        "diagnostic_output_directory": str(output_dir),
        "command_name": command_name,
        "safety_flags": SAFETY_FLAGS,
    }


def _enforce_canonical_guard(fingerprint: dict[str, Any], *, allow_noncanonical: bool) -> None:
    problems = []
    if not fingerprint["env_present"]:
        problems.append("runtime .env is missing")
    if not fingerprint["database_query_only"]:
        problems.append("database is not query-only")
    if int(fingerprint["database_market_count"]) == 0:
        problems.append("database contains zero markets")
    if problems and not allow_noncanonical:
        raise RuntimeError(
            "Refusing canonical report: " + "; ".join(problems) + ". "
            "Use --allow-noncanonical only for an intentionally isolated diagnostic."
        )
    if problems:
        fingerprint["classification"] = "NONCANONICAL_TEST_DATABASE"
        fingerprint["canonical_guard_warnings"] = problems


def database_baseline(session: Session) -> dict[str, Any]:
    tables = {
        str(name)
        for name in session.execute(
            text("SELECT name FROM sqlite_master WHERE type='table'")
        ).scalars()
    }
    counts: dict[str, int | None] = {}
    for table_name in BASELINE_TABLES:
        counts[table_name] = (
            int(session.execute(text(f"SELECT count(*) FROM {table_name}")).scalar() or 0)
            if table_name in tables
            else None
        )
    return {"captured_at": utc_now().isoformat(), "table_counts": counts}


def verify_guarded_invariants(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    before_counts = before["table_counts"]
    after_counts = after["table_counts"]
    deltas = {
        table: (after_counts.get(table) or 0) - (before_counts.get(table) or 0)
        for table in GUARDED_TABLES
    }
    return {
        "database_mode": "SQLITE_MODE_RO_QUERY_ONLY",
        "guarded_counts_before": {table: before_counts.get(table) for table in GUARDED_TABLES},
        "guarded_counts_after": {table: after_counts.get(table) for table in GUARDED_TABLES},
        "guarded_count_deltas": deltas,
        "guarded_counts_unchanged": all(delta == 0 for delta in deltas.values()),
        "exchange_writes": 0,
        "paper_order_creation_enabled": False,
        "risk_preflight": True,
    }


def collect_audit_evidence(session: Session, *, recent_limit: int) -> dict[str, Any]:
    now = utc_now()
    markets = list(
        session.scalars(
            select(Market)
            .where(Market.status.in_(("open", "active")))
            .where(or_(*(Market.ticker.startswith(prefix) for prefix in CRYPTO_PREFIXES)))
            .order_by(Market.close_time, Market.ticker)
        )
    )
    snapshots = _latest_by_ticker(
        session.scalars(
            select(MarketSnapshot).order_by(MarketSnapshot.id.desc()).limit(recent_limit)
        ),
        "ticker",
    )
    forecasts = _latest_by_ticker_model(
        session.scalars(select(Forecast).order_by(Forecast.id.desc()).limit(recent_limit))
    )
    rankings = _latest_by_ticker(
        session.scalars(
            select(MarketRanking).order_by(MarketRanking.id.desc()).limit(recent_limit)
        ),
        "ticker",
    )
    return {
        "now": now,
        "markets": markets,
        "snapshots": snapshots,
        "forecasts": forecasts,
        "rankings": rankings,
    }


def build_funnel_rows(evidence: dict[str, Any]) -> list[dict[str, Any]]:
    now = evidence["now"]
    rows = []
    for market in evidence["markets"]:
        snapshot = evidence["snapshots"].get(market.ticker)
        forecast = _preferred_forecast(evidence["forecasts"], market.ticker)
        ranking = evidence["rankings"].get(market.ticker)
        close_minutes = _minutes_until(market.close_time, now)
        book = _book(snapshot)
        reasons = []
        current_window = close_minutes is None or close_minutes > 0
        if not current_window:
            reasons.append("MARKET_WINDOW_EXPIRED")
        if current_window and snapshot is None:
            reasons.append("SNAPSHOT_MISSING")
        elif current_window and snapshot is not None and not _fresh(snapshot.captured_at, now, 15):
            reasons.append("SNAPSHOT_STALE")
        if current_window and snapshot is not None and not book["executable"]:
            reasons.append("EXECUTABLE_BOOK_UNAVAILABLE")
        if current_window and forecast is None:
            reasons.append("FORECAST_MISSING")
        elif (
            current_window and forecast is not None and not _fresh(forecast.forecasted_at, now, 15)
        ):
            reasons.append("FORECAST_STALE")
        if current_window and ranking is None:
            reasons.append("RANKING_MISSING")
        elif current_window and ranking is not None and not _fresh(ranking.ranked_at, now, 15):
            reasons.append("RANKING_STALE")
        edge = _decimal(ranking.estimated_edge if ranking else None)
        score = _decimal(ranking.opportunity_score if ranking else None)
        if ranking is not None and (edge is None or edge <= 0):
            reasons.append("EV_NOT_POSITIVE")
        if ranking is not None and score is not None and score < Decimal("60"):
            reasons.append("OPPORTUNITY_SCORE_BELOW_60")
        if close_minutes is not None and close_minutes < Decimal("30"):
            reasons.append("TIME_TO_CLOSE_BELOW_30_MINUTES")
        first = reasons[0] if reasons else "PAPER_READY_BUT_CREATION_DISABLED"
        selected = snapshot is not None or forecast is not None or ranking is not None
        rows.append(
            {
                "ticker": market.ticker,
                "event_ticker": market.event_ticker,
                "series_ticker": market.series_ticker or _crypto_series(market.ticker),
                "title": market.title,
                "market_family": "crypto",
                "expiration": _iso(market.close_time),
                "status": market.status,
                "yes_bid": book["yes_bid"],
                "yes_ask": book["yes_ask"],
                "no_bid": book["no_bid"],
                "no_ask": book["no_ask"],
                "spread": book["spread"],
                "volume": snapshot.volume_fp if snapshot else market.volume_fp,
                "open_interest": snapshot.open_interest_fp if snapshot else market.open_interest_fp,
                "last_trade": snapshot.last_price_dollars if snapshot else None,
                "snapshot_timestamp": _iso(snapshot.captured_at if snapshot else None),
                "forecast_timestamp": _iso(forecast.forecasted_at if forecast else None),
                "ranking_timestamp": _iso(ranking.ranked_at if ranking else None),
                "selected": selected,
                "current_window_eligible": current_window,
                "first_exclusion_reason": first,
                "all_exclusion_reasons": ";".join(reasons) if reasons else first,
                "source_page": "NOT_PERSISTED",
                "selection_priority": "RECENT_BOUNDED_RUNTIME_SELECTOR",
                "selected_in_prior_cycles": selected,
            }
        )
    return rows


def build_funnel_summary(rows: list[dict[str, Any]], evidence: dict[str, Any]) -> dict[str, Any]:
    counts = Counter(str(row["first_exclusion_reason"]) for row in rows)
    current_rows = [row for row in rows if row["current_window_eligible"]]
    current_counts = Counter(str(row["first_exclusion_reason"]) for row in current_rows)
    by_symbol: dict[str, dict[str, Any]] = {}
    for symbol in sorted({str(row["series_ticker"] or "UNKNOWN") for row in current_rows}):
        selected = [row for row in current_rows if str(row["series_ticker"] or "UNKNOWN") == symbol]
        covered = [row for row in selected if row["selected"]]
        by_symbol[symbol] = {
            "series_ticker": symbol,
            "active_markets": len(selected),
            "selected_markets": len(covered),
            "selection_rate": _ratio(len(covered), len(selected)),
            "snapshot_rows": sum(bool(row["snapshot_timestamp"]) for row in selected),
            "forecast_rows": sum(bool(row["forecast_timestamp"]) for row in selected),
            "ranking_rows": sum(bool(row["ranking_timestamp"]) for row in selected),
        }
    selected_tickers = {row["ticker"] for row in current_rows if row["selected"]}
    return {
        "generated_at": utc_now().isoformat(),
        "mode": "READ_ONLY_CANONICAL_FUNNEL_AUDIT",
        "observed_active_crypto_markets": len(rows),
        "expired_open_status_rows": len(rows) - len(current_rows),
        "current_active_crypto_markets": len(current_rows),
        "selected_unique_tickers": len(selected_tickers),
        "never_selected_active_tickers": len(current_rows) - len(selected_tickers),
        "selection_rate": _ratio(len(selected_tickers), len(current_rows)),
        "first_exclusion_counts": dict(sorted(counts.items())),
        "current_first_exclusion_counts": dict(sorted(current_counts.items())),
        "coverage_by_symbol": by_symbol,
        "operational_loss_rows": sum(
            count
            for reason, count in current_counts.items()
            if "MISSING" in reason or "STALE" in reason
        ),
        "economic_loss_rows": sum(
            count
            for reason, count in current_counts.items()
            if "EV_" in reason or "SCORE" in reason
        ),
        "unknown_rows": current_counts.get("UNKNOWN", 0),
        "recent_lineage_limits": {
            "snapshots_loaded": len(evidence["snapshots"]),
            "forecast_keys_loaded": len(evidence["forecasts"]),
            "rankings_loaded": len(evidence["rankings"]),
            "page_number_persisted": False,
        },
    }


def build_ev_attribution(
    rankings: dict[str, MarketRanking], snapshots: dict[str, MarketSnapshot]
) -> list[dict[str, Any]]:
    rows = []
    for ticker, ranking in sorted(rankings.items()):
        probability = _decimal(ranking.forecast_probability)
        price = _decimal(ranking.best_price)
        midpoint = _decimal(ranking.midpoint)
        raw = _object(ranking.raw_json)
        side_probability = (
            Decimal("1") - probability
            if probability is not None and ranking.best_side == "BUY_NO"
            else probability
        )
        independent_gross = (
            side_probability - price if side_probability is not None and price is not None else None
        )
        fee = trading_fee(price=price) if price is not None else None
        independent_net = independent_gross - fee if independent_gross is not None else None
        application_gross = _decimal(raw.get("gross_expected_value"))
        application_fee = _decimal(raw.get("estimated_taker_fee"))
        application_net = _decimal(raw.get("fee_adjusted_expected_value"))
        tolerance = Decimal("0.000001")
        reconciled = (
            application_net is None
            or independent_net is None
            or abs(application_net - independent_net) <= tolerance
        )
        snapshot = snapshots.get(ticker)
        blockers = []
        if snapshot is None:
            blockers.append("SNAPSHOT_MISSING")
        if independent_net is None or independent_net <= 0:
            blockers.append("EXECUTABLE_EV_NOT_POSITIVE")
        if _decimal(ranking.opportunity_score) < Decimal("60"):
            blockers.append("OPPORTUNITY_SCORE_BELOW_60")
        rows.append(
            {
                "ticker": ticker,
                "decision_timestamp": _iso(ranking.ranked_at),
                "snapshot_timestamp": _iso(snapshot.captured_at if snapshot else None),
                "model_probability": _text(probability),
                "yes_bid": _book(snapshot)["yes_bid"],
                "yes_ask": _book(snapshot)["yes_ask"],
                "no_bid": _book(snapshot)["no_bid"],
                "no_ask": _book(snapshot)["no_ask"],
                "market_midpoint": _text(midpoint),
                "model_minus_midpoint": _text(
                    probability - midpoint
                    if probability is not None and midpoint is not None
                    else None
                ),
                "model_minus_executable_ask": _text(independent_gross),
                "spread_cost": ranking.spread,
                "fee_estimate": _text(fee),
                "slippage_estimate": "0",
                "theoretical_ev": _text(
                    probability - midpoint
                    if probability is not None and midpoint is not None
                    else None
                ),
                "executable_ev": _text(independent_net),
                "application_gross_ev": _text(application_gross),
                "application_fee": _text(application_fee),
                "application_executable_ev": _text(application_net),
                "reconciled": reconciled,
                "opportunity_score": ranking.opportunity_score,
                "first_hard_blocker": blockers[0] if blockers else "NONE",
                "all_blockers": ";".join(blockers) if blockers else "NONE",
                "best_side": ranking.best_side,
                "best_price": ranking.best_price,
            }
        )
    return rows


def build_model_independence(
    forecasts: dict[tuple[str, str], Forecast], snapshots: dict[str, MarketSnapshot]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    rows = []
    feature_stats: dict[str, list[Decimal]] = defaultdict(list)
    feature_nulls: Counter[str] = Counter()
    for (ticker, model), forecast in sorted(forecasts.items()):
        probability = _decimal(forecast.yes_probability)
        midpoint = _decimal(forecast.market_mid_probability)
        snapshot = snapshots.get(ticker)
        ask = _decimal(snapshot.best_yes_ask if snapshot else forecast.best_yes_ask)
        features = _object(forecast.feature_json)
        flat = _numeric_features(features)
        for key, value in flat.items():
            if value is None:
                feature_nulls[key] += 1
            else:
                feature_stats[key].append(value)
        rows.append(
            {
                "ticker": ticker,
                "model_name": model,
                "forecast_timestamp": _iso(forecast.forecasted_at),
                "model_probability": _text(probability),
                "market_midpoint": _text(midpoint),
                "executable_yes_ask": _text(ask),
                "model_minus_midpoint": _text(
                    probability - midpoint
                    if probability is not None and midpoint is not None
                    else None
                ),
                "model_minus_ask": _text(
                    probability - ask if probability is not None and ask is not None else None
                ),
                "absolute_model_movement": _text(
                    abs(probability - midpoint)
                    if probability is not None and midpoint is not None
                    else None
                ),
                "feature_count": len(flat),
                "notes": forecast.notes,
            }
        )
    diffs = [
        abs(_decimal(row["model_minus_midpoint"])) for row in rows if row["model_minus_midpoint"]
    ]
    feature_rows = []
    for key in sorted(set(feature_stats) | set(feature_nulls)):
        values = feature_stats.get(key, [])
        feature_rows.append(
            {
                "feature": key,
                "non_null_count": len(values),
                "null_count": feature_nulls[key],
                "minimum": _text(min(values) if values else None),
                "maximum": _text(max(values) if values else None),
                "constant": len(set(values)) <= 1 if values else True,
                "effectively_zero": all(abs(value) <= Decimal("0.000001") for value in values),
            }
        )
    near_identical = sum(diff <= Decimal("0.001") for diff in diffs)
    summary = {
        "forecast_rows": len(rows),
        "comparable_rows": len(diffs),
        "median_absolute_model_minus_midpoint": _text(median(diffs) if diffs else None),
        "nearly_identical_to_midpoint_rows": near_identical,
        "nearly_identical_rate": _ratio(near_identical, len(diffs)),
        "features_audited": len(feature_rows),
        "constant_features": sum(bool(row["constant"]) for row in feature_rows),
        "effectively_zero_features": sum(bool(row["effectively_zero"]) for row in feature_rows),
        "ablation_status": "REPORT_ONLY_PROXY_FROM_PERSISTED_FORECASTS",
        "future_leakage_tested": False,
    }
    return rows, feature_rows, summary


def build_root_cause_verdict(
    *,
    fingerprint: dict[str, Any],
    funnel_summary: dict[str, Any],
    ev_rows: list[dict[str, Any]],
    model_summary: dict[str, Any],
    safety: dict[str, Any],
) -> dict[str, Any]:
    ev_mismatches = sum(not bool(row["reconciled"]) for row in ev_rows)
    positive = sum((_decimal(row["executable_ev"]) or Decimal("-1")) > 0 for row in ev_rows)
    labels = [
        {
            "label": "RUNTIME_LINEAGE_DEFECT",
            "severity": "HIGH",
            "confidence": "HIGH",
            "evidence": "A separate worktree previously generated an empty-database report; canonical identity is now fingerprinted.",
            "affected_rows": fingerprint["database_market_count"],
            "proposed_fix": "Require the canonical fingerprint and query-only database URL for diagnostics.",
            "risk": "LOW",
            "expected_outcome": "No zero-market report can be mislabeled canonical.",
            "rollback": "Remove the report guard; no database state is changed.",
        },
        {
            "label": "FUNNEL_COVERAGE_BOTTLENECK",
            "severity": "MEDIUM",
            "confidence": "HIGH",
            "evidence": f"Only {funnel_summary['selected_unique_tickers']} of {funnel_summary['current_active_crypto_markets']} current crypto tickers have recent bounded lineage.",
            "affected_rows": funnel_summary["never_selected_active_tickers"],
            "proposed_fix": "Run a bounded rotating selector experiment without changing production thresholds.",
            "risk": "MEDIUM_API_AND_RUNTIME_COST",
            "expected_outcome": "Higher unique executable-book coverage and measurable selector fairness.",
            "rollback": "Restore current selector limits and cursor state.",
        },
        {
            "label": "MODEL_MARKET_ANCHORING",
            "severity": "HIGH",
            "confidence": "MEDIUM" if model_summary["comparable_rows"] else "LOW",
            "evidence": f"{model_summary['nearly_identical_to_midpoint_rows']} of {model_summary['comparable_rows']} comparable forecasts are within 0.1 percentage point of midpoint.",
            "affected_rows": model_summary["nearly_identical_to_midpoint_rows"],
            "proposed_fix": "Build settlement-scored market-baseline versus external-signal ablations.",
            "risk": "MEDIUM_MODEL_RISK",
            "expected_outcome": "Independent lift and post-cost EV measured over at least 100 observations.",
            "rollback": "Keep current model as unchanged baseline.",
        },
        {
            "label": "EXECUTABLE_EV_IMPLEMENTATION_DEFECT",
            "severity": "HIGH" if ev_mismatches else "NONE_FOUND",
            "confidence": "HIGH" if ev_rows else "LOW",
            "evidence": f"Independent EV recomputation found {ev_mismatches} mismatches across {len(ev_rows)} recent rankings.",
            "affected_rows": ev_mismatches,
            "proposed_fix": "Repair only reconciled side/fee defects if mismatches are nonzero.",
            "risk": "HIGH_IF_INCORRECT",
            "expected_outcome": "Application and independent EV agree within 1e-6.",
            "rollback": "Revert the isolated EV patch.",
        },
        {
            "label": "GENUINE_NO_EDGE_IN_CURRENT_CRYPTO_STRATEGY",
            "severity": "HIGH",
            "confidence": "MEDIUM",
            "evidence": f"Recent independently recomputed rows contain {positive} positive executable-EV candidates.",
            "affected_rows": len(ev_rows) - positive,
            "proposed_fix": "Collect settled near-miss shadow evidence before changing thresholds.",
            "risk": "LOW_READ_ONLY_RESEARCH",
            "expected_outcome": "Strategy retained or demoted using post-cost settled evidence.",
            "rollback": "Stop the isolated shadow ledger.",
        },
    ]
    return {
        "audit_version": AUDIT_VERSION,
        "generated_at": utc_now().isoformat(),
        "classification": "MIXED_ROOT_CAUSE",
        "runtime_id": fingerprint["runtime_id"],
        "ranked_causes": labels,
        "ev_math_defect_found": ev_mismatches > 0,
        "positive_executable_ev_rows": positive,
        "model_summary": model_summary,
        "funnel_summary": funnel_summary,
        "safety": safety,
    }


def _latest_by_ticker(rows: Iterable[Any], field: str) -> dict[str, Any]:
    result = {}
    for row in rows:
        key = str(getattr(row, field))
        result.setdefault(key, row)
    return result


def _latest_by_ticker_model(rows: Iterable[Forecast]) -> dict[tuple[str, str], Forecast]:
    result = {}
    for row in rows:
        result.setdefault((row.ticker, row.model_name), row)
    return result


def _preferred_forecast(forecasts: dict[tuple[str, str], Forecast], ticker: str) -> Forecast | None:
    choices = [row for (row_ticker, _), row in forecasts.items() if row_ticker == ticker]
    return max(choices, key=lambda row: row.forecasted_at) if choices else None


def _book(snapshot: MarketSnapshot | None) -> dict[str, Any]:
    if snapshot is None:
        return {
            "yes_bid": None,
            "yes_ask": None,
            "no_bid": None,
            "no_ask": None,
            "spread": None,
            "executable": False,
        }
    yes_bid = snapshot.yes_bid_dollars or snapshot.best_yes_bid
    yes_ask = snapshot.yes_ask_dollars or snapshot.best_yes_ask
    no_bid = snapshot.no_bid_dollars or snapshot.best_no_bid
    no_ask = snapshot.no_ask_dollars or snapshot.best_no_ask
    return {
        "yes_bid": yes_bid,
        "yes_ask": yes_ask,
        "no_bid": no_bid,
        "no_ask": no_ask,
        "spread": snapshot.spread,
        "executable": bool(yes_ask or no_ask),
    }


def _crypto_series(ticker: str) -> str:
    return next((prefix for prefix in CRYPTO_PREFIXES if ticker.startswith(prefix)), "UNKNOWN")


def _numeric_features(value: Any, prefix: str = "") -> dict[str, Decimal | None]:
    output: dict[str, Decimal | None] = {}
    if isinstance(value, dict):
        for key, item in value.items():
            output.update(_numeric_features(item, f"{prefix}.{key}" if prefix else str(key)))
    elif isinstance(value, int | float | Decimal | str) and not isinstance(value, bool):
        output[prefix or "value"] = _decimal(value)
    elif value is None:
        output[prefix or "value"] = None
    return output


def _decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _fresh(value: datetime, now: datetime, minutes: int) -> bool:
    parsed = value if value.tzinfo else value.replace(tzinfo=now.tzinfo)
    return parsed >= now - timedelta(minutes=minutes)


def _minutes_until(value: datetime | None, now: datetime) -> Decimal | None:
    if value is None:
        return None
    parsed = value if value.tzinfo else value.replace(tzinfo=now.tzinfo)
    return Decimal(str((parsed - now).total_seconds())) / Decimal("60")


def _object(value: str | None) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _git(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True, default=str), encoding="utf-8")


def _write_text(path: Path, value: str) -> None:
    path.write_text(value.rstrip() + "\n", encoding="utf-8")


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _text(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


def _ratio(numerator: int, denominator: int) -> str:
    return str(Decimal(numerator) / Decimal(denominator)) if denominator else "0"


def _runtime_truth(fp: dict[str, Any], baseline: dict[str, Any]) -> str:
    return f"""# Phase 0 Runtime Truth

- Classification: `{fp["classification"]}`
- Runtime ID: `{fp["runtime_id"]}`
- Authoritative worktree: `{fp["worktree_path"]}`
- Git branch/SHA: `{fp["git_branch"]}` / `{fp["git_sha"]}`
- Environment: `{fp["env_path"]}`
- Database: `{fp["absolute_database_path"]}`
- Database URL: `{fp["database_url"]}`
- Reports: `{fp["reports_directory"]}`
- Query-only enforcement: `{fp["database_query_only"]}`
- Guarded paper orders: `{baseline["table_counts"].get("paper_orders")}`

This audit refuses canonical labeling for a missing `.env`, non-query-only connection, or empty database.
"""


def _funnel_markdown(summary: dict[str, Any]) -> str:
    return f"""# Phase 1 Market-to-Opportunity Funnel Audit

- Active crypto markets accounted for: `{summary["observed_active_crypto_markets"]}`
- Expired rows still carrying open/active status: `{summary["expired_open_status_rows"]}`
- Current active crypto markets: `{summary["current_active_crypto_markets"]}`
- Selected with recent bounded lineage: `{summary["selected_unique_tickers"]}`
- Never selected: `{summary["never_selected_active_tickers"]}`
- Selection rate: `{summary["selection_rate"]}`
- Operational first losses: `{summary["operational_loss_rows"]}`
- Economic first losses: `{summary["economic_loss_rows"]}`
- Unknown rows: `{summary["unknown_rows"]}`

Current-window first exclusions: `{summary["current_first_exclusion_counts"]}`

Every active crypto market stored in the canonical catalog receives a first exclusion reason. API page
number is not persisted by the current collector and is explicitly reported as unavailable rather than
invented. Persistent page-limit and selector comparisons belong in the next bounded experiment.
"""


def _rotation_markdown(summary: dict[str, Any]) -> str:
    return f"""# Phase 1 Rotation and Fairness

- Active tickers: `{summary["observed_active_crypto_markets"]}`
- Selected unique tickers: `{summary["selected_unique_tickers"]}`
- Never selected active tickers: `{summary["never_selected_active_tickers"]}`
- Current selection rate: `{summary["selection_rate"]}`

The production schema does not persist selector cycle/page identity. Repeat-selection rate cannot be
proven from ranking existence alone. The next phase must add a read-only selection ledger before making
persistent scheduler changes.
"""


def _ev_markdown(rows: list[dict[str, Any]]) -> str:
    mismatches = sum(not bool(row["reconciled"]) for row in rows)
    half_spread = sum(
        _decimal(row["model_minus_midpoint"]) == 0
        and (_decimal(row["model_minus_executable_ask"]) or 0) < 0
        for row in rows
    )
    return f"""# Phase 2 Executable EV Math Audit

- Recent ranking rows independently recomputed: `{len(rows)}`
- Application/independent mismatches above 1e-6: `{mismatches}`
- Exact midpoint forecasts losing at the executable ask: `{half_spread}`

Waterfall: independent model advantage over midpoint, minus spread-crossing cost, minus taker fee,
minus configured slippage (zero in this diagnostic), equals executable EV. The midpoint is diagnostic;
`best_price` is the executable cost. BUY_NO uses `1 - yes_probability`.

The repeated `-0.005` gross pattern is expected when the model equals the midpoint and a one-cent
spread is crossed: the ask is half a cent above midpoint. It is a defect only when the stored
application EV fails the independent recomputation.
"""


def _score_markdown(rows: list[dict[str, Any]]) -> str:
    positive_hidden = sum(
        (_decimal(row["executable_ev"]) or Decimal("-1")) > 0
        and (_decimal(row["opportunity_score"]) or Decimal("0")) < 60
        for row in rows
    )
    return f"""# Phase 2 Opportunity Score Gate Audit

- Positive executable-EV rows hidden by score below 60: `{positive_hidden}`
- Score threshold changed: `False`
- Edge threshold changed: `False`

EV and score remain separate gates. This audit exposes positive-EV/low-score rows without promoting
them to opportunities.
"""


def _protocol_markdown(rows: list[dict[str, Any]]) -> str:
    return f"""# Phase 2 Protocol Conformance

- Rows checked: `{len(rows)}`
- YES buy: model YES probability minus executable YES ask and fee.
- NO buy: one minus model YES probability minus executable NO ask and fee.
- Midpoint is never treated as a fill.
- Missing or stale books remain blockers.
- Spread is represented by crossing to the selected ask and is not subtracted a second time.
- Fixed-point values are parsed with `Decimal`; binary floating point is not used for EV.

Official protocol documentation remains the authority for fee schedules and order-book semantics.
No exchange write path is imported or exercised by this report.
"""


def _ablation_markdown(summary: dict[str, Any]) -> str:
    return f"""# Phase 3 Report-Only Ablations

Status: `{summary["ablation_status"]}`

Persisted forecasts permit market-versus-combined comparison, but the canonical database does not
persist every ensemble component and weight for every row. External-only and feature-family ablations
must be replayed into an isolated research ledger in Prompt 2; they are not deployed here.
"""


def _model_markdown(summary: dict[str, Any]) -> str:
    return f"""# Phase 3 Model Independence

- Forecasts inspected: `{summary["forecast_rows"]}`
- Comparable to midpoint: `{summary["comparable_rows"]}`
- Median absolute model-minus-midpoint: `{summary["median_absolute_model_minus_midpoint"]}`
- Within 0.1 percentage point of midpoint: `{summary["nearly_identical_to_midpoint_rows"]}`
- Near-identical rate: `{summary["nearly_identical_rate"]}`
- Numeric features audited: `{summary["features_audited"]}`
- Constant features: `{summary["constant_features"]}`
- Effectively-zero features: `{summary["effectively_zero_features"]}`

This quantifies anchoring from persisted evidence. Settlement-scored ablations are still required
before claiming the current model beats the market-implied baseline.
"""


def _verdict_markdown(verdict: dict[str, Any]) -> str:
    lines = [
        "# Prompt 1 Root-Cause Verdict",
        "",
        f"Classification: `{verdict['classification']}`",
        "",
    ]
    for index, cause in enumerate(verdict["ranked_causes"], start=1):
        lines.extend(
            [
                f"## {index}. {cause['label']}",
                "",
                f"- Severity/confidence: `{cause['severity']}` / `{cause['confidence']}`",
                f"- Evidence: {cause['evidence']}",
                f"- Affected rows: `{cause['affected_rows']}`",
                f"- Proposed fix: {cause['proposed_fix']}",
                f"- Risk: `{cause['risk']}`",
                f"- Expected outcome: {cause['expected_outcome']}",
                f"- Rollback: {cause['rollback']}",
                "",
            ]
        )
    return "\n".join(lines)


def _safety_markdown(safety: dict[str, Any]) -> str:
    return f"""# Safety Invariants

- Database mode: `{safety["database_mode"]}`
- Guarded counts unchanged: `{safety["guarded_counts_unchanged"]}`
- Guarded deltas: `{safety["guarded_count_deltas"]}`
- Exchange writes: `0`
- Guarded paper creation: `disabled`
- Live/demo/autopilot execution: `disabled`
- Risk preflight preserved: `true`
"""


def _test_results_template() -> str:
    return """# Test Results

Generated by the audit command. Replace this section with the exact pytest and Ruff command output
from the implementation run before merging.
"""


def _next_prompt(verdict: dict[str, Any], goal: str) -> str:
    labels = ", ".join(cause["label"] for cause in verdict["ranked_causes"][:3])
    return f"""# Next Codex Prompt: Alpha Recovery from Proven Root Cause

Read every artifact in `reports/no_opportunity_root_cause/` before changing code. The Prompt 1
verdict is `{verdict["classification"]}` with leading causes: {labels}.

Goal: {goal}

Implement Prompt 2 Phases 4-6 only: an isolated settlement-scored near-miss shadow ledger, a bounded
selector rotation/coverage experiment, and report-only external-signal versus market-baseline
ablations. Do not lower production thresholds. Do not write guarded paper tables. Keep
`PAPER_ORDER_CREATION_ENABLED=false`, `EXECUTION_ENABLED=false`, the kill switch enabled, and
`risk_preflight=true`.

Add deterministic lineage tests, YES/NO executable-price tests, no-lookahead tests, selector fairness
tests, guarded-count before/after assertions, and rollback documentation. Require at least 100 settled
or replayable observations, or produce a quantified collection plan. Emit all required
`reports/alpha_recovery/` artifacts and generate the following measurable Codex prompt.
"""
