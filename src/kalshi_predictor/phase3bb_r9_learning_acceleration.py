from __future__ import annotations

import csv
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import desc, func, or_, select
from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.data.schema import (
    BacktestRun,
    BacktestTrade,
    Forecast,
    LearningMetric,
    LearningOpportunity,
    LearningPaperTrade,
    MarketRanking,
    MarketSnapshot,
    PaperOrder,
    Settlement,
)
from kalshi_predictor.learning.safety import learning_status
from kalshi_predictor.paper.models import ORDER_FILLED
from kalshi_predictor.phase3bb_acceleration import (
    _metadata,
    _metadata_lines,
    _safety_flags,
    _write_manifest,
    historical_replay_rules,
)
from kalshi_predictor.utils.decimals import to_decimal
from kalshi_predictor.utils.time import utc_now

PHASE3BB_R9_VERSION = "phase3bb_r9_learning_acceleration_v1"
DEFAULT_OUTPUT_DIR = Path("reports/phase3bb_r9")
DEFAULT_REPORTS_DIR = Path("reports")
DEFAULT_LIMIT = 10000

REPLAY_CANDIDATE_FIELDS = [
    "category",
    "model_name",
    "forecast_rows",
    "settled_forecast_rows",
    "snapshot_linked_forecast_tickers",
    "ranking_rows",
    "backtest_only_rows",
    "brier_score",
    "accuracy",
    "avg_model_confidence",
    "positive_edge_rows",
    "positive_edge_win_rate",
    "avg_positive_edge",
    "replay_readiness",
    "learning_count_policy",
    "next_action",
]


@dataclass(frozen=True)
class Phase3BBR9LearningAccelerationArtifacts:
    output_dir: Path
    executive_summary_path: Path
    markdown_path: Path
    replay_candidates_csv_path: Path
    model_calibration_path: Path
    manifest_path: Path


def write_phase3bb_r9_learning_acceleration_report(
    session: Session,
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    reports_dir: Path = DEFAULT_REPORTS_DIR,
    settings: Settings | None = None,
    command_args: list[str] | None = None,
    limit: int = DEFAULT_LIMIT,
) -> Phase3BBR9LearningAccelerationArtifacts:
    payload = build_phase3bb_r9_learning_acceleration(
        session,
        output_dir=output_dir,
        reports_dir=reports_dir,
        settings=settings,
        command_args=command_args,
        limit=limit,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    executive_summary_path = output_dir / "EXECUTIVE_SUMMARY.md"
    markdown_path = output_dir / "learning_acceleration.md"
    replay_candidates_csv_path = output_dir / "replay_candidates.csv"
    model_calibration_path = output_dir / "model_calibration.md"
    manifest_path = output_dir / "MANIFEST.sha256"

    executive_summary_path.write_text(_render_executive_summary(payload), encoding="utf-8")
    markdown_path.write_text(_render_learning_acceleration(payload), encoding="utf-8")
    _write_csv(replay_candidates_csv_path, payload["replay_candidates"], REPLAY_CANDIDATE_FIELDS)
    model_calibration_path.write_text(_render_model_calibration(payload), encoding="utf-8")
    _write_manifest(
        manifest_path,
        [
            executive_summary_path,
            markdown_path,
            replay_candidates_csv_path,
            model_calibration_path,
        ],
    )
    return Phase3BBR9LearningAccelerationArtifacts(
        output_dir=output_dir,
        executive_summary_path=executive_summary_path,
        markdown_path=markdown_path,
        replay_candidates_csv_path=replay_candidates_csv_path,
        model_calibration_path=model_calibration_path,
        manifest_path=manifest_path,
    )


def build_phase3bb_r9_learning_acceleration(
    session: Session,
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    reports_dir: Path = DEFAULT_REPORTS_DIR,
    settings: Settings | None = None,
    command_args: list[str] | None = None,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    resolved = settings or get_settings()
    now = utc_now()
    metadata = _metadata(
        session,
        settings=resolved,
        generated_at=now.isoformat(),
        command_args=command_args or [],
        output_dir=output_dir,
    )
    metadata["command_arguments"] = {
        "command": "kalshi-bot phase3bb-r9-learning-acceleration",
        "argv": command_args or [],
    }
    learning = learning_status(session, settings=resolved)
    paper_counts = _paper_learning_counts(session, learning)
    replay_counts = _replay_counts(session)
    calibration = _model_calibration(session, limit=limit)
    candidates = _replay_candidates(session, calibration=calibration)
    summary = _summary(
        learning=learning,
        paper_counts=paper_counts,
        replay_counts=replay_counts,
        candidates=candidates,
        calibration=calibration,
        limit=limit,
    )
    safety = {
        **_safety_flags(),
        "paper_only": True,
        "diagnostic_only": True,
        "creates_paper_trades": False,
        "creates_paper_orders": False,
        "places_exchange_orders": False,
        "submits_cancels_replaces_orders": False,
        "fabricates_trades": False,
        "fabricates_settlements": False,
        "counts_replay_as_real_paper_learning": False,
        "historical_replay_label_required": "HISTORICAL_REPLAY",
        "db_writes_performed": 0,
    }
    return {
        **metadata,
        "phase": "3BB-R9-LEARNING-ACCELERATION",
        "phase_version": PHASE3BB_R9_VERSION,
        "mode": "PAPER_READ_ONLY_LEARNING_DIAGNOSTIC",
        "reports_dir": str(reports_dir),
        "limit": limit,
        "learning_status": learning,
        "paper_learning_counts": paper_counts,
        "historical_replay_rules": historical_replay_rules(),
        "replay_counts": replay_counts,
        "model_calibration": calibration,
        "replay_candidates": candidates,
        "summary": summary,
        "safety_flags": safety,
        "live_or_demo_execution": False,
        "order_submission": False,
        "order_cancel_replace": False,
        "paper_trade_creation": False,
        "thresholds_lowered": False,
    }


def _paper_learning_counts(session: Session, learning: dict[str, Any]) -> dict[str, Any]:
    paper_orders = int(session.scalar(select(func.count()).select_from(PaperOrder)) or 0)
    filled_paper_orders = int(
        session.scalar(
            select(func.count()).select_from(PaperOrder).where(PaperOrder.status == ORDER_FILLED)
        )
        or 0
    )
    learning_paper_rows = int(
        session.scalar(select(func.count()).select_from(LearningPaperTrade)) or 0
    )
    learning_opportunity_rows = int(
        session.scalar(select(func.count()).select_from(LearningOpportunity)) or 0
    )
    latest_metric = session.scalar(
        select(LearningMetric)
        .order_by(desc(LearningMetric.generated_at), desc(LearningMetric.id))
        .limit(1)
    )
    return {
        "real_paper_order_rows": paper_orders,
        "filled_paper_order_rows": filled_paper_orders,
        "learning_paper_trade_rows": learning_paper_rows,
        "learning_opportunity_rows": learning_opportunity_rows,
        "settled_paper_trades": int(learning.get("settled_paper_trades") or 0),
        "target_settled_trades": int(learning.get("target_settled_trades") or 0),
        "remaining_settled_trades": int(learning.get("remaining_settled_trades") or 0),
        "progress_percent": learning.get("progress_percent") or "0.0%",
        "latest_learning_metric": (
            {
                "generated_at": latest_metric.generated_at.isoformat(),
                "window_days": latest_metric.window_days,
                "opportunities_found": latest_metric.opportunities_found,
                "paper_trades_created": latest_metric.paper_trades_created,
                "settled_trade_count": latest_metric.settled_trade_count,
                "learning_confidence": latest_metric.learning_confidence,
            }
            if latest_metric
            else None
        ),
    }


def _replay_counts(session: Session) -> dict[str, Any]:
    backtest_runs = int(session.scalar(select(func.count()).select_from(BacktestRun)) or 0)
    backtest_trades = int(session.scalar(select(func.count()).select_from(BacktestTrade)) or 0)
    settled_forecasts = int(
        session.scalar(
            select(func.count())
            .select_from(Forecast)
            .join(Settlement, Forecast.ticker == Settlement.ticker)
            .where(_settlement_is_known())
        )
        or 0
    )
    return {
        "historical_replay_candidate_rows": settled_forecasts,
        "backtest_run_rows": backtest_runs,
        "backtest_only_rows": backtest_trades,
        "rows_counted_as_real_paper_learning": 0,
        "counts_toward_learning_target": False,
        "required_label": "HISTORICAL_REPLAY",
    }


def _model_calibration(session: Session, *, limit: int) -> dict[str, Any]:
    rows = session.execute(
        select(Forecast, Settlement)
        .join(Settlement, Forecast.ticker == Settlement.ticker)
        .where(_settlement_is_known())
        .order_by(desc(Forecast.forecasted_at), desc(Forecast.id))
        .limit(limit)
    )
    per_model: dict[str, dict[str, Any]] = {}
    for forecast, settlement in rows:
        probability = _probability(forecast.yes_probability)
        outcome = _settlement_outcome(settlement)
        if probability is None or outcome is None:
            continue
        model = forecast.model_name or "unknown"
        stats = per_model.setdefault(model, _empty_model_stats(model))
        stats["evaluable_forecast_rows"] += 1
        error = probability - Decimal(outcome)
        stats["_brier_sum"] += error * error
        predicted_yes = probability >= Decimal("0.5")
        stats["_correct"] += int(predicted_yes == bool(outcome))
        stats["_confidence_sum"] += abs(probability - Decimal("0.5")) * Decimal("2")

    _add_ev_calibration(session, per_model, limit=limit)
    model_rows = []
    for model, stats in sorted(per_model.items()):
        evaluable = stats["evaluable_forecast_rows"]
        positive_edge = stats["positive_edge_rows"]
        model_rows.append(
            {
                "model_name": model,
                "category": _category_for_model(model),
                "evaluable_forecast_rows": evaluable,
                "brier_score": _decimal_average(stats["_brier_sum"], evaluable),
                "accuracy": _ratio(stats["_correct"], evaluable),
                "avg_model_confidence": _decimal_average(stats["_confidence_sum"], evaluable),
                "positive_edge_rows": positive_edge,
                "positive_edge_win_rate": _ratio(stats["_positive_edge_wins"], positive_edge),
                "avg_positive_edge": _decimal_average(stats["_positive_edge_sum"], positive_edge),
                "calibration_status": _calibration_status(evaluable),
            }
        )
    return {
        "calibration_rows_scanned_limit": limit,
        "model_rows": model_rows,
        "model_count": len(model_rows),
        "best_brier_model": _best_brier_model(model_rows),
        "honesty_policy": (
            "Calibration rows can improve model diagnostics only; they do not create "
            "or settle paper trades."
        ),
    }


def _add_ev_calibration(
    session: Session,
    per_model: dict[str, dict[str, Any]],
    *,
    limit: int,
) -> None:
    rows = session.execute(
        select(MarketRanking, Settlement)
        .join(Settlement, MarketRanking.ticker == Settlement.ticker)
        .where(_settlement_is_known())
        .where(MarketRanking.estimated_edge.is_not(None))
        .order_by(desc(MarketRanking.ranked_at), desc(MarketRanking.id))
        .limit(limit)
    )
    for ranking, settlement in rows:
        edge = to_decimal(ranking.estimated_edge)
        outcome = _settlement_outcome(settlement)
        if edge is None or edge <= 0 or outcome is None:
            continue
        model = ranking.forecast_model or "unknown"
        stats = per_model.setdefault(model, _empty_model_stats(model))
        stats["positive_edge_rows"] += 1
        stats["_positive_edge_sum"] += edge
        side = (ranking.best_side or "").strip().lower()
        if (side == "yes" and outcome == 1) or (side == "no" and outcome == 0):
            stats["_positive_edge_wins"] += 1


def _replay_candidates(
    session: Session,
    *,
    calibration: dict[str, Any],
) -> list[dict[str, Any]]:
    forecast_counts = _group_count(session, Forecast.model_name, Forecast)
    ranking_counts = _group_count(session, MarketRanking.forecast_model, MarketRanking)
    settled_counts = dict(
        session.execute(
            select(Forecast.model_name, func.count(Forecast.id))
            .select_from(Forecast)
            .join(Settlement, Forecast.ticker == Settlement.ticker)
            .where(_settlement_is_known())
            .group_by(Forecast.model_name)
        ).all()
    )
    snapshot_linked_counts = dict(
        session.execute(
            select(Forecast.model_name, func.count(func.distinct(Forecast.ticker)))
            .select_from(Forecast)
            .join(MarketSnapshot, Forecast.ticker == MarketSnapshot.ticker)
            .group_by(Forecast.model_name)
        ).all()
    )
    backtest_counts = dict(
        session.execute(
            select(BacktestRun.model_name, func.count(BacktestTrade.id))
            .select_from(BacktestTrade)
            .join(BacktestRun, BacktestTrade.backtest_run_id == BacktestRun.id)
            .group_by(BacktestRun.model_name)
        ).all()
    )
    calibration_by_model = {
        row["model_name"]: row for row in calibration.get("model_rows", [])
    }
    models = sorted(
        set(forecast_counts)
        | set(ranking_counts)
        | set(settled_counts)
        | set(snapshot_linked_counts)
        | set(backtest_counts)
        | set(calibration_by_model)
    )
    candidates = []
    for model in models:
        settled = int(settled_counts.get(model) or 0)
        backtest_only = int(backtest_counts.get(model) or 0)
        calibration_row = calibration_by_model.get(model, {})
        readiness = _replay_readiness(settled, backtest_only)
        candidates.append(
            {
                "category": _category_for_model(str(model)),
                "model_name": str(model),
                "forecast_rows": int(forecast_counts.get(model) or 0),
                "settled_forecast_rows": settled,
                "snapshot_linked_forecast_tickers": int(snapshot_linked_counts.get(model) or 0),
                "ranking_rows": int(ranking_counts.get(model) or 0),
                "backtest_only_rows": backtest_only,
                "brier_score": calibration_row.get("brier_score") or "",
                "accuracy": calibration_row.get("accuracy") or "",
                "avg_model_confidence": calibration_row.get("avg_model_confidence") or "",
                "positive_edge_rows": int(calibration_row.get("positive_edge_rows") or 0),
                "positive_edge_win_rate": calibration_row.get("positive_edge_win_rate") or "",
                "avg_positive_edge": calibration_row.get("avg_positive_edge") or "",
                "replay_readiness": readiness,
                "learning_count_policy": "BACKTEST_ONLY_NOT_REAL_PAPER",
                "next_action": _candidate_next_action(readiness, str(model)),
            }
        )
    return sorted(
        candidates,
        key=lambda row: (
            int(row["settled_forecast_rows"]),
            int(row["backtest_only_rows"]),
            int(row["ranking_rows"]),
        ),
        reverse=True,
    )


def _summary(
    *,
    learning: dict[str, Any],
    paper_counts: dict[str, Any],
    replay_counts: dict[str, Any],
    candidates: list[dict[str, Any]],
    calibration: dict[str, Any],
    limit: int,
) -> dict[str, Any]:
    settled = int(paper_counts["settled_paper_trades"])
    target = int(paper_counts["target_settled_trades"])
    top_candidate = candidates[0] if candidates else None
    historical_rows = int(replay_counts["historical_replay_candidate_rows"])
    return {
        "status": "LEARNING_ACCELERATION_REPORT_READY",
        "learning_enabled": bool(learning.get("enabled")),
        "real_settled_paper_trades": settled,
        "target_settled_paper_trades": target,
        "remaining_real_settled_paper_trades": max(0, target - settled),
        "learning_progress_percent": paper_counts["progress_percent"],
        "historical_replay_candidate_rows": historical_rows,
        "backtest_only_rows": int(replay_counts["backtest_only_rows"]),
        "replay_rows_counted_as_real_paper_learning": 0,
        "model_calibration_rows_limited_to": limit,
        "model_calibration_model_count": int(calibration["model_count"]),
        "top_replay_candidate_model": (top_candidate or {}).get("model_name"),
        "top_replay_candidate_category": (top_candidate or {}).get("category"),
        "top_replay_candidate_status": (top_candidate or {}).get("replay_readiness"),
        "primary_blocker": (
            "NO_SETTLED_FORECASTS_FOR_REPLAY"
            if historical_rows <= 0
            else "REAL_PAPER_TRADE_SAMPLE_TOO_SMALL"
        ),
        "recommended_next_action": (
            "Build/run a backtest-only replay harness for the top settled-forecast model; "
            "keep results out of paper order counts."
            if historical_rows > 0
            else "Keep collecting current paper-ready rows before attempting replay calibration."
        ),
        "paper_trade_creation": False,
        "live_demo_order_creation": False,
    }


def _group_count(session: Session, column: Any, model: Any) -> dict[str, int]:
    return {
        str(key): int(value or 0)
        for key, value in session.execute(
            select(column, func.count()).select_from(model).group_by(column)
        ).all()
        if key is not None
    }


def _settlement_is_known() -> Any:
    return or_(
        Settlement.result.in_(("yes", "no", "YES", "NO")),
        Settlement.yes_settlement_value.is_not(None),
    )


def _settlement_outcome(settlement: Settlement) -> int | None:
    result = (settlement.result or "").strip().lower()
    if result == "yes":
        return 1
    if result == "no":
        return 0
    value = to_decimal(settlement.yes_settlement_value)
    if value is None:
        return None
    return 1 if value >= Decimal("0.5") else 0


def _probability(value: Any) -> Decimal | None:
    probability = to_decimal(value)
    if probability is None:
        return None
    if probability > 1 and probability <= 100:
        probability = probability / Decimal("100")
    if probability < 0 or probability > 1:
        return None
    return probability


def _empty_model_stats(model: str) -> dict[str, Any]:
    return defaultdict(
        lambda: Decimal("0"),
        {
            "model_name": model,
            "evaluable_forecast_rows": 0,
            "_brier_sum": Decimal("0"),
            "_correct": 0,
            "_confidence_sum": Decimal("0"),
            "positive_edge_rows": 0,
            "_positive_edge_sum": Decimal("0"),
            "_positive_edge_wins": 0,
        },
    )


def _decimal_average(total: Any, count: int) -> str:
    if count <= 0:
        return ""
    return _format_decimal(Decimal(total) / Decimal(count))


def _ratio(numerator: int, denominator: int) -> str:
    if denominator <= 0:
        return ""
    return _format_decimal(Decimal(numerator) / Decimal(denominator))


def _format_decimal(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.0001")), "f")


def _best_brier_model(rows: list[dict[str, Any]]) -> str | None:
    eligible = [row for row in rows if row.get("brier_score")]
    if not eligible:
        return None
    return min(eligible, key=lambda row: Decimal(str(row["brier_score"])))["model_name"]


def _calibration_status(evaluable_rows: int) -> str:
    if evaluable_rows >= 100:
        return "ENOUGH_FOR_CALIBRATION_TREND"
    if evaluable_rows >= 30:
        return "ENOUGH_FOR_PRELIMINARY_CALIBRATION"
    if evaluable_rows > 0:
        return "SMALL_SAMPLE_DIAGNOSTIC_ONLY"
    return "NO_SETTLED_FORECASTS"


def _replay_readiness(settled_forecasts: int, backtest_only_rows: int) -> str:
    if settled_forecasts >= 100:
        return "READY_FOR_BACKTEST_CALIBRATION"
    if settled_forecasts >= 30:
        return "READY_FOR_PRELIMINARY_REPLAY"
    if settled_forecasts > 0:
        return "SMALL_SAMPLE_REPLAY_ONLY"
    if backtest_only_rows > 0:
        return "BACKTEST_ROWS_EXIST_BUT_NO_SETTLED_FORECASTS"
    return "NO_REPLAY_DATA"


def _candidate_next_action(readiness: str, model_name: str) -> str:
    if readiness in {"READY_FOR_BACKTEST_CALIBRATION", "READY_FOR_PRELIMINARY_REPLAY"}:
        return (
            f"Run a backtest-only calibration replay for {model_name}; label all rows "
            "HISTORICAL_REPLAY and do not increment paper learning counts."
        )
    if readiness == "SMALL_SAMPLE_REPLAY_ONLY":
        return (
            f"Use {model_name} only for small-sample diagnostics while collecting more "
            "real paper-ready rows."
        )
    if readiness == "BACKTEST_ROWS_EXIST_BUT_NO_SETTLED_FORECASTS":
        return f"Reconcile {model_name} backtest rows with settlement evidence before replay."
    return f"Keep {model_name} in normal source/forecast collection."


def _category_for_model(model_name: str) -> str:
    lowered = model_name.lower()
    if "weather" in lowered:
        return "weather"
    if "crypto" in lowered or lowered in {"btc", "eth", "sol", "xrp", "doge"}:
        return "crypto"
    if any(token in lowered for token in ("economic", "cpi", "fed", "gdp", "jobs")):
        return "economic"
    if any(token in lowered for token in ("sports", "mlb", "nba", "nfl", "nhl", "soccer")):
        return "sports"
    if any(token in lowered for token in ("agri", "usda", "commodity")):
        return "agriculture_general"
    if "news" in lowered:
        return "news"
    return "general"


def _render_executive_summary(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# Phase 3BB-R9 Learning Acceleration")
    summary = payload["summary"]
    lines.extend(
        [
            "",
            "## Summary",
            "",
            f"- Status: `{summary['status']}`",
            "- Real settled paper trades: "
            f"`{summary['real_settled_paper_trades']} / "
            f"{summary['target_settled_paper_trades']}`",
            "- Historical replay candidate rows: "
            f"`{summary['historical_replay_candidate_rows']}`",
            f"- Backtest-only rows: `{summary['backtest_only_rows']}`",
            "- Replay rows counted as real paper learning: "
            f"`{summary['replay_rows_counted_as_real_paper_learning']}`",
            f"- Top replay candidate: `{summary['top_replay_candidate_model'] or 'none'}`",
            f"- Primary blocker: `{summary['primary_blocker']}`",
            "",
            "## Next Action",
            "",
            summary["recommended_next_action"],
            "",
            "## Safety",
            "",
            "- No paper trades were created.",
            "- No live/demo exchange orders were submitted, canceled, replaced, or amended.",
            (
                "- Historical replay remains backtest-only unless an operator explicitly "
                "authorizes a separate labeled lane."
            ),
        ]
    )
    return "\n".join(lines) + "\n"


def _render_learning_acceleration(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# Phase 3BB-R9 Learning Acceleration Detail")
    summary = payload["summary"]
    paper_counts = payload["paper_learning_counts"]
    replay_counts = payload["replay_counts"]
    learning = payload["learning_status"]
    lines.extend(
        [
            "",
            "## Honest Learning Progress",
            "",
            f"- Learning enabled: `{learning.get('enabled')}`",
            f"- Real paper order rows: `{paper_counts['real_paper_order_rows']}`",
            f"- Filled paper order rows: `{paper_counts['filled_paper_order_rows']}`",
            (
                "- Settled paper trades counted toward target: "
                f"`{paper_counts['settled_paper_trades']}`"
            ),
            f"- Target settled paper trades: `{paper_counts['target_settled_trades']}`",
            f"- Remaining real settled trades: `{paper_counts['remaining_settled_trades']}`",
            f"- Progress: `{paper_counts['progress_percent']}`",
            f"- Expected completion: `{learning.get('expected_completion')}`",
            "",
            "## Replay Separation",
            "",
            (
                "- Historical replay candidate rows: "
                f"`{replay_counts['historical_replay_candidate_rows']}`"
            ),
            f"- Backtest run rows: `{replay_counts['backtest_run_rows']}`",
            f"- Backtest-only trade rows: `{replay_counts['backtest_only_rows']}`",
            "- Backtest/replay rows counted toward real paper target: "
            f"`{replay_counts['rows_counted_as_real_paper_learning']}`",
            f"- Required replay label: `{replay_counts['required_label']}`",
            "",
            "## Recommendation",
            "",
            summary["recommended_next_action"],
            "",
            "## Top Replay Candidates",
            "",
            (
                "| Category | Model | Settled Forecasts | Backtest-Only Rows | "
                "Brier | Accuracy | Status |"
            ),
            "| --- | --- | ---: | ---: | ---: | ---: | --- |",
        ]
    )
    for row in payload["replay_candidates"][:10]:
        lines.append(
            "| {category} | {model_name} | {settled_forecast_rows} | "
            "{backtest_only_rows} | {brier_score} | {accuracy} | {replay_readiness} |".format(
                **row
            )
        )
    if not payload["replay_candidates"]:
        lines.append("| n/a | n/a | 0 | 0 |  |  | NO_REPLAY_DATA |")
    return "\n".join(lines) + "\n"


def _render_model_calibration(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# Phase 3BB-R9 Model Calibration")
    calibration = payload["model_calibration"]
    lines.extend(
        [
            "",
            "## Policy",
            "",
            calibration["honesty_policy"],
            "",
            "## Calibration Rows",
            "",
            f"- Scan limit: `{calibration['calibration_rows_scanned_limit']}`",
            f"- Model count: `{calibration['model_count']}`",
            f"- Best Brier model: `{calibration['best_brier_model'] or 'none'}`",
            "",
            (
                "| Category | Model | Rows | Brier | Accuracy | Confidence | "
                "Positive Edge Rows | Positive Edge Win Rate | Status |"
            ),
            "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
        ]
    )
    for row in calibration["model_rows"]:
        lines.append(
            "| {category} | {model_name} | {evaluable_forecast_rows} | {brier_score} | "
            "{accuracy} | {avg_model_confidence} | {positive_edge_rows} | "
            "{positive_edge_win_rate} | {calibration_status} |".format(**row)
        )
    if not calibration["model_rows"]:
        lines.append("| n/a | n/a | 0 |  |  |  | 0 |  | NO_SETTLED_FORECASTS |")
    return "\n".join(lines) + "\n"


def _write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
