from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.system_certification.service import (
    SystemCertificationService,
    certification_status,
)


def generate_system_certification_report(
    session: Session,
    *,
    output_dir: str | Path = "reports/system_certification",
    settings: Settings | None = None,
    mode: str | None = None,
    run_contract_tests: bool = False,
    run_golden_trace: bool = False,
    database_profile: str = "local",
    runtime_url: str | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    service = SystemCertificationService(session, settings=settings or get_settings())
    return service.write_artifacts(
        output_dir=output_dir,
        mode=mode,
        run_contract_tests=run_contract_tests,
        run_golden_trace=run_golden_trace,
        database_profile=database_profile,
        runtime_url=runtime_url,
        persist=persist,
    )


def system_certification_card(
    session: Session,
    *,
    settings: Settings | None = None,
) -> dict[str, Any]:
    return certification_status(session, settings=settings or get_settings())
