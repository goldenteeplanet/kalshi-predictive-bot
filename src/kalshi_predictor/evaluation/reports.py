from dataclasses import asdict
from pathlib import Path
from typing import Any

import pandas as pd
from sqlalchemy.orm import Session

from kalshi_predictor.data.repositories import get_forecasts_with_settlements
from kalshi_predictor.evaluation.calibration import calibration_bins
from kalshi_predictor.evaluation.metrics import accuracy_at_threshold, brier_score, log_loss
from kalshi_predictor.utils.decimals import to_decimal
from kalshi_predictor.utils.time import utc_now


def generate_calibration_report(
    model_name: str,
    output_path: str | Path,
    *,
    session: Session | None = None,
) -> Path:
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    csv_output = output.with_suffix(".csv")

    rows = _evaluated_rows(model_name, session=session)
    frame = pd.DataFrame(rows)
    frame.to_csv(csv_output, index=False)

    if frame.empty:
        report_text = _empty_report(model_name, csv_output)
    else:
        y_true = [int(value) for value in frame["y_true"].tolist()]
        y_prob = [float(value) for value in frame["yes_probability"].tolist()]
        bins = calibration_bins(y_true, y_prob)
        report_text = _report_text(
            model_name=model_name,
            evaluated_count=len(frame),
            brier=brier_score(y_true, y_prob),
            loss=log_loss(y_true, y_prob),
            accuracy=accuracy_at_threshold(y_true, y_prob),
            bins=bins,
            csv_output=csv_output,
        )

    output.write_text(report_text, encoding="utf-8")
    return output


def _evaluated_rows(model_name: str, *, session: Session | None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for forecast, settlement in get_forecasts_with_settlements(model_name, session=session):
        y_true = _settlement_to_y_true(settlement.result, settlement.yes_settlement_value)
        yes_probability = to_decimal(forecast.yes_probability)
        if y_true is None or yes_probability is None:
            continue
        rows.append(
            {
                "ticker": forecast.ticker,
                "forecasted_at": forecast.forecasted_at.isoformat(),
                "model_name": forecast.model_name,
                "yes_probability": float(yes_probability),
                "y_true": y_true,
                "settled_at": settlement.settled_at.isoformat()
                if settlement.settled_at is not None
                else None,
                "result": settlement.result,
            }
        )
    return rows


def _settlement_to_y_true(result: str | None, yes_settlement_value: str | None) -> int | None:
    if result:
        normalized = result.strip().lower()
        if normalized in {"yes", "y", "1", "true"}:
            return 1
        if normalized in {"no", "n", "0", "false"}:
            return 0

    value = to_decimal(yes_settlement_value)
    if value is None:
        return None
    if value == 1:
        return 1
    if value == 0:
        return 0
    return None


def _empty_report(model_name: str, csv_output: Path) -> str:
    generated_at = utc_now().isoformat()
    return (
        f"# Calibration Report: {model_name}\n\n"
        f"- Generated at: {generated_at}\n"
        "- Evaluated forecasts: 0\n"
        f"- Row-level CSV: `{csv_output}`\n\n"
        "No evaluated forecasts were available. Run collection, settlement sync, and then "
        "generate the report again after markets have settled.\n"
    )


def _report_text(
    *,
    model_name: str,
    evaluated_count: int,
    brier: float,
    loss: float,
    accuracy: float,
    bins: list[Any],
    csv_output: Path,
) -> str:
    generated_at = utc_now().isoformat()
    lines = [
        f"# Calibration Report: {model_name}",
        "",
        f"- Generated at: {generated_at}",
        f"- Evaluated forecasts: {evaluated_count}",
        f"- Brier score: {brier:.6f}",
        f"- Log loss: {loss:.6f}",
        f"- Accuracy at 0.5: {accuracy:.6f}",
        f"- Row-level CSV: `{csv_output}`",
        "",
        "## Calibration Table",
        "",
        "| Bin start | Bin end | Count | Avg predicted probability | Observed frequency |",
        "|---:|---:|---:|---:|---:|",
    ]
    for calibration_bin in bins:
        row = asdict(calibration_bin)
        lines.append(
            "| "
            f"{row['bin_start']:.2f} | "
            f"{row['bin_end']:.2f} | "
            f"{row['count']} | "
            f"{row['avg_predicted_probability']:.6f} | "
            f"{row['observed_frequency']:.6f} |"
        )
    lines.extend(
        [
            "",
            "## Notes",
            "",
            "- Phase 1 uses read-only public market data only.",
            "- Evaluation joins stored forecasts to locally synced settlement records.",
            "- Calibration quality depends on the breadth and timing of collected snapshots.",
        ]
    )
    return "\n".join(lines) + "\n"

