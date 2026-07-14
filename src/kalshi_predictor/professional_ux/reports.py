from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.professional_ux.service import (
    build_ux_audit,
    phase_3x_status_card,
    write_phase_3x_artifacts,
)


def generate_phase_3x_report(
    session: Session,
    *,
    output_dir: Path | str = "docs/phase_3x",
    settings: Settings | None = None,
) -> dict[str, Any]:
    return write_phase_3x_artifacts(
        session,
        output_dir=Path(output_dir),
        settings=settings or get_settings(),
    )


def phase_3x_audit_payload(
    session: Session,
    *,
    settings: Settings | None = None,
) -> dict[str, Any]:
    return build_ux_audit(session, settings=settings or get_settings())


def phase_3x_card(
    session: Session,
    *,
    settings: Settings | None = None,
) -> dict[str, Any]:
    return phase_3x_status_card(session, settings=settings or get_settings())

