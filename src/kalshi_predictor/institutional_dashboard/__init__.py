"""Phase 3T read-only institutional dashboard."""

from kalshi_predictor.institutional_dashboard.service import (
    build_dashboard_snapshot,
    dashboard_panel_response,
)

__all__ = ["build_dashboard_snapshot", "dashboard_panel_response"]
