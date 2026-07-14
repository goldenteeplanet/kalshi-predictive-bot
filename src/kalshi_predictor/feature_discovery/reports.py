from __future__ import annotations

from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.feature_discovery.contracts import RUN_ON_DEMAND, FeatureDiscoveryResult
from kalshi_predictor.feature_discovery.engine import run_feature_discovery


def generate_feature_discovery_report(
    session: Session,
    *,
    output_path: str | Path = Path("reports/feature_discovery_report.md"),
    json_output_path: str | Path | None = Path("reports/feature_discovery_report.json"),
    run_type: str = RUN_ON_DEMAND,
    training_as_of: datetime | str | None = None,
    settings: Settings | None = None,
    force: bool = False,
) -> FeatureDiscoveryResult:
    return run_feature_discovery(
        session,
        run_type=run_type,
        training_as_of=training_as_of,
        output_path=output_path,
        json_output_path=json_output_path,
        settings=settings or get_settings(),
        force=force,
    )
