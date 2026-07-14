from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from kalshi_predictor.confidence.engine import run_model_confidence_engine
from kalshi_predictor.confidence.repository import confidence_rows_for_ui
from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.utils.time import utc_now


def generate_model_confidence_report(
    session: Session,
    *,
    output_path: Path = Path("reports/model_confidence.md"),
    settings: Settings | None = None,
    days: int = 30,
    refresh: bool = True,
) -> Path:
    resolved_settings = settings or get_settings()
    if refresh:
        run_model_confidence_engine(
            session,
            settings=resolved_settings,
            days=days,
            persist=True,
            update_weights=True,
        )
    rows = confidence_rows_for_ui(session)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(render_model_confidence_report(rows, days=days), encoding="utf-8")
    return output_path


def render_model_confidence_report(rows: list[dict[str, Any]], *, days: int) -> str:
    lines = [
        "# Model Confidence Report",
        "",
        f"- Generated at: {utc_now().isoformat()}",
        f"- Lookback days: {days}",
        "- Mode: paper outcomes and settled forecasts only",
        "",
        "## Current Scores",
        "",
        "| Category | Model | Settled | Brier | Win rate | ROI | Score | Label | Notes |",
        "|---|---|---:|---:|---:|---:|---:|---|---|",
    ]
    if not rows:
        lines.append("| _No model confidence scores yet_ |  |  |  |  |  |  |  |  |")
    for row in rows:
        lines.append(
            "| "
            f"{row['category']} | {row['model_name']} | {row['settled_trade_count']} | "
            f"{row['brier_score'] or ''} | {row['win_rate'] or ''} | "
            f"{row['roi_on_exposure'] or ''} | {row['confidence_score']} | "
            f"{row['confidence_label']} | {row['notes']} |"
        )
    lines.extend(
        [
            "",
            "## How To Use",
            "",
            (
                "Confidence weights are written to `model_weights` with method "
                "`model_confidence_v1`; `ensemble_v2` uses the latest stored weights."
            ),
            "",
        ]
    )
    return "\n".join(lines)

