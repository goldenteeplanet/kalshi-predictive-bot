from collections import defaultdict
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from kalshi_predictor.tournament.engine import TournamentResult, run_model_tournament
from kalshi_predictor.utils.decimals import to_decimal
from kalshi_predictor.utils.time import utc_now


def generate_tournament_report(
    session: Session,
    *,
    days: int,
    output_path: str | Path,
    name: str | None = None,
    generate_weights: bool = True,
) -> tuple[Path, TournamentResult]:
    result = run_model_tournament(
        session,
        days=days,
        name=name,
        generate_weights=generate_weights,
        persist=True,
    )
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(_render_tournament_report(result), encoding="utf-8")
    return output, result


def generate_model_diagnostics_report(
    session: Session,
    *,
    days: int,
    output_path: str | Path,
) -> tuple[Path, TournamentResult]:
    result = run_model_tournament(
        session,
        days=days,
        name=f"model_diagnostics_{days}d",
        generate_weights=False,
        persist=True,
    )
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(_render_diagnostics_report(result), encoding="utf-8")
    return output, result


def generate_model_weights_report(
    session: Session,
    *,
    days: int,
    output_path: str | Path,
) -> tuple[Path, TournamentResult]:
    result = run_model_tournament(
        session,
        days=days,
        name=f"model_weights_{days}d",
        generate_weights=True,
        persist=True,
    )
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(_render_weights_report(result), encoding="utf-8")
    return output, result


def _render_tournament_report(result: TournamentResult) -> str:
    rows = sorted(result.rows, key=lambda row: (row["category"], row["overall_rank"] or 999))
    insufficient = [row for row in rows if row["status"] == "INSUFFICIENT_DATA"]
    lines = [
        "# Model Tournament",
        "",
        f"- Generated at: {utc_now().isoformat()}",
        f"- Tournament: {result.name}",
        f"- Lookback window: {result.days} days",
        "",
        "## Overall Leaderboard",
        "",
        "| Category | Rank | Model | Forecasts | Evaluated | Trades | Brier | ROI | P&L | "
        "Max DD | Status |",
        "|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for row in rows:
        lines.append(_tournament_row(row))
    lines.extend(
        [
            "",
            "## Category Winners",
            "",
            _category_winners(rows),
            "",
            "## Calibration Rankings",
            "",
            _ranking_list(rows, "calibration_rank", "brier_score"),
            "",
            "## P&L Rankings",
            "",
            _ranking_list(rows, "pnl_rank", "roi_on_exposure"),
            "",
            "## Models With Insufficient Data",
            "",
            _insufficient_list(insufficient),
            "",
            "## Diagnostics Summary",
            "",
            _diagnostics_summary(result.diagnostics),
            "",
            "## Generated Weights",
            "",
            _weights_table(result.weights),
            "",
            "## Recommended Next Action",
            "",
            "Collect more settled forecasts and backtest trades, then rerun the tournament before "
            "considering Phase 3 readiness.",
            "",
        ]
    )
    return "\n".join(lines)


def _render_diagnostics_report(result: TournamentResult) -> str:
    lines = [
        "# Model Diagnostics",
        "",
        f"- Generated at: {utc_now().isoformat()}",
        f"- Lookback window: {result.days} days",
        "",
        "| Model | Category | Diagnostic type | Metric | Value | Notes | Recommended action |",
        "|---|---|---|---|---:|---|---|",
    ]
    for diagnostic in result.diagnostics:
        lines.append(
            "| "
            f"{diagnostic['model_name']} | "
            f"{diagnostic['category']} | "
            f"{diagnostic['diagnostic_type']} | "
            f"{diagnostic['metric_name']} | "
            f"{_metric(diagnostic.get('metric_value'))} | "
            f"{diagnostic['notes']} | "
            f"{_recommended_action(diagnostic['notes'])} |"
        )
    lines.append("")
    return "\n".join(lines)


def _render_weights_report(result: TournamentResult) -> str:
    lines = [
        "# Model Weights",
        "",
        f"- Generated at: {utc_now().isoformat()}",
        f"- Lookback window: {result.days} days",
        "",
        "| Category | Model | Weight | Method | Lookback days | Notes |",
        "|---|---|---:|---|---:|---|",
    ]
    for weight in sorted(result.weights, key=lambda row: (row["category"], row["model_name"])):
        lines.append(
            "| "
            f"{weight['category']} | "
            f"{weight['model_name']} | "
            f"{weight['weight']} | "
            f"{weight['method']} | "
            f"{weight['lookback_days']} | "
            f"{weight.get('notes', '')} |"
        )
    lines.append("")
    return "\n".join(lines)


def _tournament_row(row: dict[str, Any]) -> str:
    return (
        "| "
        f"{row['category']} | "
        f"{row.get('overall_rank') or ''} | "
        f"{row['model_name']} | "
        f"{row['forecast_count']} | "
        f"{row['evaluated_forecast_count']} | "
        f"{row['settled_trade_count']} | "
        f"{_metric(row.get('brier_score'))} | "
        f"{_metric(row.get('roi_on_exposure'))} | "
        f"{_metric(row.get('total_pnl'))} | "
        f"{_metric(row.get('max_drawdown'))} | "
        f"{row['status']} |"
    )


def _category_winners(rows: list[dict[str, Any]]) -> str:
    winners = [
        row
        for row in rows
        if row.get("overall_rank") == 1 and row.get("status") != "INSUFFICIENT_DATA"
    ]
    if not winners:
        return "No category has a sufficient-data winner yet."
    return "\n".join(f"- {row['category']}: {row['model_name']}" for row in winners)


def _ranking_list(rows: list[dict[str, Any]], rank_field: str, metric: str) -> str:
    ranked = sorted(rows, key=lambda row: (row["category"], row.get(rank_field) or 999))
    return "\n".join(
        f"- {row['category']} #{row.get(rank_field) or 'n/a'}: {row['model_name']} "
        f"({metric}={_metric(row.get(metric))})"
        for row in ranked
    )


def _insufficient_list(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "No insufficient-data rows."
    return "\n".join(f"- {row['model_name']} / {row['category']}: {row['notes']}" for row in rows)


def _diagnostics_summary(diagnostics: list[dict[str, Any]]) -> str:
    counts: dict[str, int] = defaultdict(int)
    for diagnostic in diagnostics:
        counts[str(diagnostic["diagnostic_type"])] += 1
    if not counts:
        return "No diagnostics generated."
    return "\n".join(f"- {key}: {value}" for key, value in sorted(counts.items()))


def _weights_table(weights: list[dict[str, Any]]) -> str:
    if not weights:
        return "No weights generated."
    lines = ["| Category | Model | Weight | Method |", "|---|---|---:|---|"]
    for weight in sorted(weights, key=lambda row: (row["category"], row["model_name"])):
        lines.append(
            f"| {weight['category']} | {weight['model_name']} | "
            f"{weight['weight']} | {weight['method']} |"
        )
    return "\n".join(lines)


def _metric(value: Any) -> str:
    if value is None:
        return "n/a"
    parsed = to_decimal(value)
    if parsed is not None:
        return str(parsed)
    return str(value)


def _recommended_action(notes: str) -> str:
    lowered = notes.lower()
    if "not enough" in lowered:
        return "Collect more settled forecasts."
    if "negative" in lowered:
        return "Review losses before increasing weight."
    if "overconfidence" in lowered or "weak calibration" in lowered:
        return "Check calibration and reduce confidence."
    return "Monitor in the next tournament run."
