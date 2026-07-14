from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.self_evaluation.engine import SelfEvaluationResult, run_self_evaluation


def generate_self_evaluation_report(
    session: Session,
    *,
    output_path: str | Path = Path("reports/self_evaluation_journal.md"),
    json_output_path: str | Path | None = Path("reports/self_evaluation_journal.json"),
    session_date: str | date | None = None,
    evaluation_as_of: datetime | str | None = None,
    settings: Settings | None = None,
) -> SelfEvaluationResult:
    return run_self_evaluation(
        session,
        settings=settings or get_settings(),
        session_date=session_date,
        evaluation_as_of=evaluation_as_of,
        output_path=output_path,
        json_output_path=json_output_path,
    )
