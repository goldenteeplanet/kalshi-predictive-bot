from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.synthetic_markets.contracts import (
    RUN_CANDIDATE_DISCOVERY,
    SyntheticMarketsResult,
)
from kalshi_predictor.synthetic_markets.engine import run_synthetic_markets


def generate_synthetic_markets_report(
    session: Session,
    *,
    input_file: str | Path | None = None,
    candidates: list[dict[str, Any]] | None = None,
    run_type: str = RUN_CANDIDATE_DISCOVERY,
    estimate_as_of: datetime | str | None = None,
    output_path: str | Path = Path("reports/synthetic_markets_report.md"),
    json_output_path: str | Path | None = Path("reports/synthetic_markets_report.json"),
    settings: Settings | None = None,
    force: bool = False,
) -> SyntheticMarketsResult:
    return run_synthetic_markets(
        session,
        input_file=input_file,
        candidates=candidates,
        run_type=run_type,
        estimate_as_of=estimate_as_of,
        output_path=output_path,
        json_output_path=json_output_path,
        settings=settings or get_settings(),
        force=force,
    )
