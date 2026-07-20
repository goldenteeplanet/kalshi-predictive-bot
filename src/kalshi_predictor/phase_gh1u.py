from pathlib import Path
from typing import Any, Callable

from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings
from kalshi_predictor.phase_gh1t import run_atomic_activation


def run_lead_time_atomic_activation(
    *, session_factory: Callable[[], Session], settings: Settings, verified_backup: Path,
    writer_monitor_fn: Callable[[], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return run_atomic_activation(
        session_factory=session_factory, settings=settings, verified_backup=verified_backup,
        max_markets_per_category=20, writer_monitor_fn=writer_monitor_fn,
        minimum_close_minutes=settings.opportunity_min_time_to_close_minutes,
        immediate_edge_evaluation=True, phase_name="GH-1U",
    )
