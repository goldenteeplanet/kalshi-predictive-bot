from __future__ import annotations

from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.reinforcement_learning.engine import run_rl_evaluation


def generate_rl_policy_report(
    session: Session,
    *,
    run_type: str = "EVALUATE",
    training_as_of: datetime | str | None = None,
    output_path: str | Path = Path("reports/rl_policy_report.md"),
    json_output_path: str | Path | None = Path("reports/rl_policy_report.json"),
    settings: Settings | None = None,
    force: bool = False,
):
    return run_rl_evaluation(
        session,
        run_type=run_type,
        training_as_of=training_as_of,
        output_path=output_path,
        json_output_path=json_output_path,
        settings=settings or get_settings(),
        force=force,
    )
