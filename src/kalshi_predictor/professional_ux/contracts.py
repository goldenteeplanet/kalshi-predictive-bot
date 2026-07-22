from __future__ import annotations

from typing import Any

PHASE_3X_VERSION = "phase-3x-professional-ux-v1"
PREFERENCE_SCHEMA_VERSION = "phase-3x-ui-preferences-v1"

DECISION_GO = "GO"
DECISION_CONDITIONAL_GO = "CONDITIONAL_GO"
DECISION_NO_GO = "NO_GO"
DECISION_INCOMPLETE = "INCOMPLETE"

PHASE_3X_MODES = {"audit_only", "preview", "staging", "production"}
THEME_MODES = {"light", "dark", "system"}
DENSITY_MODES = {"comfortable", "compact"}

NAV_ITEMS: tuple[dict[str, str], ...] = (
    {"label": "Today", "href": "/today", "family": "today"},
    {"label": "Opportunities", "href": "/opportunities", "family": "opportunities"},
    {"label": "Markets", "href": "/markets", "family": "markets"},
    {"label": "Links", "href": "/links/coverage", "family": "markets"},
    {"label": "Portfolio", "href": "/portfolio", "family": "portfolio"},
    {"label": "Risk", "href": "/risk", "family": "risk"},
    {"label": "Trades", "href": "/trades", "family": "trades"},
    {"label": "Models", "href": "/models", "family": "models"},
    {"label": "Journal", "href": "/journal", "family": "journal"},
    {"label": "Research", "href": "/research", "family": "research"},
    {"label": "System", "href": "/system", "family": "system"},
    {"label": "Refresh", "href": "/system/refresh-readiness", "family": "system"},
    {"label": "Settings", "href": "/settings", "family": "settings"},
)

COMMAND_ITEMS: tuple[dict[str, str], ...] = NAV_ITEMS + (
    {"label": "System health", "href": "/system/health", "family": "system"},
    {"label": "Refresh & readiness", "href": "/system/refresh-readiness", "family": "system"},
    {
        "label": "System certification",
        "href": "/system/certification",
        "family": "system",
    },
    {"label": "Live readiness", "href": "/live-readiness", "family": "system"},
    {"label": "Learning", "href": "/learning", "family": "system"},
    {"label": "Control center", "href": "/control-center", "family": "system"},
    {"label": "Signals", "href": "/signals", "family": "research"},
    {"label": "Link coverage", "href": "/links/coverage", "family": "markets"},
)

ROUTE_INVENTORY: tuple[dict[str, str], ...] = (
    {
        "route": "/today",
        "primary_job": "Answer what to focus on today without hiding no-trade states.",
        "source_authority": "Phase 3U/3T read models and existing dashboard services",
        "action_authority": "Read-only; no domain mutations",
        "disposition": "add",
        "risk": "Requires parity evidence before default production rollout",
    },
    {
        "route": "/",
        "primary_job": "Legacy trader cockpit landing route",
        "source_authority": "DecisionUiService.dashboard",
        "action_authority": "Existing guarded action routes only",
        "disposition": "retain",
        "risk": "Legacy density and terminology remain until route migration",
    },
    {
        "route": "/opportunities",
        "primary_job": "Scan and investigate ranked opportunities",
        "source_authority": "MarketRanking, Forecast, MarketSnapshot",
        "action_authority": "Read by default; existing review route for actions",
        "disposition": "refine",
        "risk": "Must keep fixed route ordering before /opportunities/{ticker}",
    },
    {
        "route": "/opportunities/{ticker}",
        "primary_job": "Inspect lineage, probabilities, risk, and paper history",
        "source_authority": "Market, MarketRanking, Forecast, Paper ledger",
        "action_authority": "Existing execution review route",
        "disposition": "refine",
        "risk": "Market and model probability labels must remain distinct",
    },
    {
        "route": "/links/coverage",
        "primary_job": "Inspect parsed market legs and specialized link coverage",
        "source_authority": "MarketLeg and crypto/weather/economic/sports/news link tables",
        "action_authority": "Read-only diagnostics; CLI owns parsing mutations",
        "disposition": "add",
        "risk": "Coverage evidence must not imply live-trading readiness",
    },
    {
        "route": "/institutional",
        "primary_job": "Read-only institutional dashboard snapshot",
        "source_authority": "Phase 3T dashboard snapshot service",
        "action_authority": "Read-only Phase 3T boundary",
        "disposition": "retain",
        "risk": "Cannot grow write controls",
    },
    {
        "route": "/portfolio",
        "primary_job": "Inspect paper positions and portfolio P&L",
        "source_authority": "Paper ledger and workstation repositories",
        "action_authority": "Read-only investigation",
        "disposition": "refine",
        "risk": "Gross, net, realized, and projected values must stay labeled",
    },
    {
        "route": "/models",
        "primary_job": "Review model readiness and performance",
        "source_authority": "Forecasts, confidence, model status",
        "action_authority": "Read-only; no model promotion",
        "disposition": "refine",
        "risk": "Predictive accuracy and economic profitability can be confused",
    },
    {
        "route": "/research",
        "primary_job": "Research-only opportunity explanations and evidence",
        "source_authority": "Research assistant and reports",
        "action_authority": "Questions/report generation only",
        "disposition": "retain",
        "risk": "Research candidates must not look tradable by default",
    },
    {
        "route": "/personal-trader",
        "primary_job": "Phase 3U structured recommendation brief",
        "source_authority": "Phase 3U recommendation service",
        "action_authority": "No direct orders; existing guarded workflows only",
        "disposition": "refine",
        "risk": "LLM narrative must remain subordinate to structured evidence",
    },
    {
        "route": "/live-readiness",
        "primary_job": "Phase 3V readiness evidence and certificate state",
        "source_authority": "Phase 3V readiness service",
        "action_authority": "Review/audit only; no execution enablement",
        "disposition": "retain",
        "risk": "Expired or incomplete certificates must block success claims",
    },
    {
        "route": "/system-certification",
        "primary_job": "Phase 3W end-to-end certification evidence",
        "source_authority": "Phase 3W certification service",
        "action_authority": "Audit-only by default",
        "disposition": "retain",
        "risk": "SYSTEM_INCOMPLETE must not look like production readiness",
    },
    {
        "route": "/system",
        "primary_job": "Unified system health and release-readiness overview",
        "source_authority": "DB, Phase 3V, Phase 3W, Phase 3X status cards",
        "action_authority": "Read-only overview and report links",
        "disposition": "add",
        "risk": "Shell rendering alone cannot imply healthy data",
    },
    {
        "route": "/settings",
        "primary_job": "Presentation and database settings visibility",
        "source_authority": "Settings and database status",
        "action_authority": "Presentation preferences only in Phase 3X",
        "disposition": "retain",
        "risk": "Preferences must never change domain thresholds",
    },
)

STATUS_GRAMMAR: dict[str, dict[str, str]] = {
    "healthy": {
        "label": "Healthy",
        "class": "status-healthy",
        "icon": "OK",
        "description": "The source reports current usable data.",
    },
    "degraded": {
        "label": "Degraded",
        "class": "status-degraded",
        "icon": "WARN",
        "description": "The source is available with warnings or partial data.",
    },
    "failed": {
        "label": "Failed",
        "class": "status-failed",
        "icon": "FAIL",
        "description": "The source failed and must not be treated as current.",
    },
    "incomplete": {
        "label": "Incomplete",
        "class": "status-incomplete",
        "icon": "INC",
        "description": "Required evidence is missing.",
    },
    "fresh": {
        "label": "Fresh",
        "class": "status-healthy",
        "icon": "OK",
        "description": "Data is inside the fresh window.",
    },
    "stale": {
        "label": "Stale",
        "class": "status-degraded",
        "icon": "STALE",
        "description": "Data is outside the fresh window.",
    },
    "unknown": {
        "label": "Unknown",
        "class": "status-incomplete",
        "icon": "UNK",
        "description": "No authoritative source value is available.",
    },
    "paper": {
        "label": "Paper",
        "class": "status-paper",
        "icon": "PAPER",
        "description": "Paper-only simulated trading context.",
    },
    "live": {
        "label": "Live",
        "class": "status-failed",
        "icon": "LIVE",
        "description": "Live-capital context. Requires explicit authorization.",
    },
    "blocked": {
        "label": "Blocked",
        "class": "status-failed",
        "icon": "BLOCK",
        "description": "A gate prevents the action or recommendation path.",
    },
    "no_trade": {
        "label": "No trade",
        "class": "status-incomplete",
        "icon": "NO",
        "description": "No candidate currently clears the required gates.",
    },
}

PROHIBITED_TERMS = (
    "sure thing",
    "guaranteed",
    "safe trade",
    "can't miss",
    "free money",
    "lock",
    "strong play",
    "act now",
)

BOUNDARY_ASSERTIONS: tuple[dict[str, Any], ...] = (
    {
        "name": "presentation_only",
        "passed": True,
        "detail": "Phase 3X adds shell, routing, reports, and presentation adapters only.",
    },
    {
        "name": "no_direct_exchange_client",
        "passed": True,
        "detail": "No Phase 3X frontend route calls exchange write clients.",
    },
    {
        "name": "phase_3t_read_only",
        "passed": True,
        "detail": "Institutional dashboard routes remain read-only.",
    },
    {
        "name": "preferences_are_presentation_only",
        "passed": True,
        "detail": "Allowed preferences exclude thresholds, model weights, and risk limits.",
    },
)

DEFAULT_PRESENTATION_PREFERENCES: dict[str, Any] = {
    "schema_version": PREFERENCE_SCHEMA_VERSION,
    "user_scope": "local",
    "theme": "system",
    "density": "comfortable",
    "timezone": "America/Chicago",
    "default_route": "/today",
    "chart_table_preference": "table_first",
    "tables": {},
    "saved_views": [],
}
