import json
import logging
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from kalshi_predictor.advanced_risk.reports import (
    advanced_risk_card,
    generate_advanced_risk_report,
)
from kalshi_predictor.autopilot.reports import build_autopilot_status, generate_autopilot_report
from kalshi_predictor.autopilot.runner import run_autopilot_once
from kalshi_predictor.confidence.reports import generate_model_confidence_report
from kalshi_predictor.confidence.repository import confidence_rows_for_ui
from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.control_center.reports import (
    build_control_center,
    generate_control_center_report,
)
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.locks import db_writer_monitor
from kalshi_predictor.data.maintenance import database_status_card
from kalshi_predictor.data.repositories import decode_json
from kalshi_predictor.data.schema import Forecast, Market, MarketLeg, NewsItem, PaperOrder
from kalshi_predictor.forecasting.status import (
    generate_model_readiness_report,
    model_status_rows,
)
from kalshi_predictor.institutional_dashboard.contracts import API_SCHEMA_VERSION
from kalshi_predictor.institutional_dashboard.reports import (
    generate_institutional_dashboard_report,
)
from kalshi_predictor.institutional_dashboard.service import (
    build_dashboard_snapshot,
    dashboard_panel_response,
    export_snapshot_csv,
    panel_data,
)
from kalshi_predictor.learning.diagnostics import generate_learning_diagnostics_report
from kalshi_predictor.learning.reports import (
    build_learning_dashboard,
    generate_learning_report,
    generate_learning_targets_report,
)
from kalshi_predictor.learning.runner import run_learning_once
from kalshi_predictor.learning.safety import learning_daily_cap_status
from kalshi_predictor.live_readiness.reports import generate_live_readiness_report
from kalshi_predictor.live_readiness.service import (
    latest_live_readiness_payload,
    live_readiness_status,
)
from kalshi_predictor.market_legs import link_coverage_dashboard
from kalshi_predictor.memory.reports import generate_memory_report, memory_health
from kalshi_predictor.meta.reports import (
    generate_meta_evaluation_report,
    generate_meta_opportunities_report,
    generate_meta_report,
    meta_detail,
)
from kalshi_predictor.meta.reports import (
    meta_dashboard as build_meta_dashboard,
)
from kalshi_predictor.microstructure.reports import (
    generate_microstructure_backtest_report,
    generate_microstructure_opportunities_report,
    generate_microstructure_report,
    microstructure_detail,
)
from kalshi_predictor.microstructure.reports import (
    microstructure_dashboard as build_microstructure_dashboard,
)
from kalshi_predictor.news.reports import (
    generate_news_report,
    news_opportunity_rows,
)
from kalshi_predictor.news.repository import (
    item_entities,
    news_dashboard_summary,
    news_links_for_item,
)
from kalshi_predictor.opportunities.reports import (
    best_payout_rows,
    generate_best_payouts_report,
)
from kalshi_predictor.overnight.reports import build_overnight_status, generate_overnight_report
from kalshi_predictor.overnight.runner import run_overnight_once
from kalshi_predictor.personal_trader.contracts import API_SCHEMA_VERSION as PHASE_3U_API_SCHEMA
from kalshi_predictor.personal_trader.reports import generate_personal_trader_report
from kalshi_predictor.personal_trader.service import (
    brief_by_id,
    build_personal_trade_brief,
    conversational_response,
    latest_brief,
    recommendation_audit_events,
    recommendation_by_id,
)
from kalshi_predictor.professional_ux.contracts import DECISION_INCOMPLETE, ROUTE_INVENTORY
from kalshi_predictor.professional_ux.reports import (
    generate_phase_3x_report,
    phase_3x_card,
)
from kalshi_predictor.professional_ux.service import (
    DEFAULT_SHELL_STATUS_SNAPSHOT_PATH,
    load_shell_status_context,
)
from kalshi_predictor.provenance.diagnostics import (
    build_market_decision_trace,
    build_provenance_diagnostics,
    build_provenance_drift_alerts,
)
from kalshi_predictor.research.assistant import (
    research_dashboard,
    research_opportunity,
)
from kalshi_predictor.research.questions import answer_research_question
from kalshi_predictor.research.reports import generate_research_report
from kalshi_predictor.research.repository import store_research_question
from kalshi_predictor.self_evaluation.reports import generate_self_evaluation_report
from kalshi_predictor.signals.reports import generate_signal_report
from kalshi_predictor.signals.repository import signal_detail, signal_health, signal_marketplace
from kalshi_predictor.sports.reports import (
    generate_sports_report,
    sports_opportunity_rows,
)
from kalshi_predictor.sports.repository import sports_dashboard_summary, sports_game_detail
from kalshi_predictor.system_certification.reports import (
    generate_system_certification_report,
    system_certification_card,
)
from kalshi_predictor.system_readiness.long_jobs import build_long_job_monitor
from kalshi_predictor.tonight.control import tonight_card
from kalshi_predictor.ui.evidence_viewer import (
    EvidenceRejected,
    get_cached_evidence_catalog,
    load_evidence_artifact,
)
from kalshi_predictor.ui.progress import (
    certification_reports_root,
    get_cached_progress_dashboard,
)
from kalshi_predictor.ui.service import (
    REPORT_LINKS,
    DecisionUiService,
    _opportunity_links_health_summary,
)
from kalshi_predictor.ui.timeline_export_status import timeline_export_path
from kalshi_predictor.utils.time import utc_now
from kalshi_predictor.workspace_guard import build_workspace_consistency_guard
from kalshi_predictor.workstation.reports import (
    generate_analytics_report,
    generate_daily_briefing,
    generate_portfolio_summary_report,
)
from kalshi_predictor.workstation.repository import (
    add_market_to_watchlist,
    alerts_summary,
    analytics_summary,
    market_monitor_rows,
    model_performance_rows,
    paper_liquidity_plan,
    portfolio_summary_fast,
    position_detail,
    remove_market_from_watchlist,
    watchlists_summary,
)

TEMPLATE_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
logger = logging.getLogger(__name__)
LINK_COVERAGE_SNAPSHOT_PATH = Path("reports/market_coverage/link_coverage.json")
LINK_COVERAGE_REPORT_STALE_AFTER_SECONDS = 30 * 60
SHELL_CONTEXT_CACHE_SECONDS = 15
PORTFOLIO_CONTEXT_CACHE_SECONDS = 60


def _link_coverage_for_ui(session: Session) -> dict[str, Any]:
    snapshot = _load_link_coverage_snapshot(LINK_COVERAGE_SNAPSHOT_PATH)
    if snapshot is not None:
        snapshot = dict(snapshot)
        snapshot["data_source"] = "generated_snapshot"
        _annotate_link_coverage_freshness(snapshot)
        return snapshot
    coverage = link_coverage_dashboard(session)
    coverage["data_source"] = "live_database"
    _annotate_link_coverage_freshness(coverage)
    return coverage


def _load_link_coverage_snapshot(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("Could not read link coverage snapshot at %s", path)
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("category_rows"), list):
        return None
    return payload


def _annotate_link_coverage_freshness(payload: dict[str, Any]) -> None:
    generated_at = _parse_link_coverage_generated_at(payload.get("generated_at"))
    source = str(payload.get("data_source") or "unknown")
    if generated_at is None:
        payload["freshness_status"] = "REPORT_UNKNOWN"
        payload["freshness_class"] = "status-incomplete"
        payload["age_label"] = "unknown age"
        payload["freshness_note"] = f"{source} has no generated_at timestamp."
        return
    age_seconds = max(0, int((utc_now() - generated_at).total_seconds()))
    stale = age_seconds > LINK_COVERAGE_REPORT_STALE_AFTER_SECONDS
    payload["freshness_status"] = "REPORT_STALE" if stale else "REPORT_FRESH"
    payload["freshness_class"] = "status-degraded" if stale else "status-healthy"
    payload["age_seconds"] = age_seconds
    payload["age_label"] = _compact_age(age_seconds)
    payload["freshness_note"] = (
        f"{source} generated {_compact_age(age_seconds)} ago; "
        f"stale threshold is {_compact_age(LINK_COVERAGE_REPORT_STALE_AFTER_SECONDS)}."
    )


def _parse_link_coverage_generated_at(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _compact_age(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    if hours < 48:
        return f"{hours}h"
    days = hours // 24
    return f"{days}d"


def _fast_system_health_payload(
    *,
    settings: Settings,
    session: Session,
    shell_context: dict[str, Any],
) -> dict[str, Any]:
    """Bounded status objects for the /system landing page."""

    market_status = shell_context.get("market_freshness")
    market_label = (
        str(market_status.get("label") or "UNKNOWN")
        if isinstance(market_status, dict)
        else "UNKNOWN"
    )
    snapshot_status = shell_context.get("shell_status_snapshot")
    snapshot_label = (
        str(snapshot_status.get("freshness_status") or "UNKNOWN")
        if isinstance(snapshot_status, dict)
        else "UNKNOWN"
    )
    snapshot_age = (
        str(snapshot_status.get("age_label") or "unknown")
        if isinstance(snapshot_status, dict)
        else "unknown"
    )
    db_backend = _db_backend_label(settings.kalshi_db_url)
    workspace_guard = {
        "ui_badge": {"class": "status-healthy", "label": "Build OK"},
        "summary": {
            "status": "SNAPSHOT_BACKED",
            "missing_required_commands": 0,
            "database_backend": db_backend,
            "git_commit": "snapshot",
        },
        "runtime": {
            "repository_root": str(Path.cwd()),
            "current_working_directory": str(Path.cwd()),
            "virtualenv": ".venv",
        },
        "database": {"database_fingerprint": db_backend},
        "findings": [
            {
                "severity": "INFO",
                "code": "BOUNDED_SYSTEM_PAGE",
                "message": (
                    "This landing page uses a fast shell-status snapshot. Open the "
                    "linked reports or APIs for full deep diagnostics."
                ),
                "next_action": "Run kalshi-bot ui-shell-status-refresh before operating.",
            }
        ],
        "next_action": "Use dedicated report buttons for full diagnostics.",
    }
    db_monitor = {
        "status": "SNAPSHOT_ONLY",
        "safe_to_start_write": False,
        "current_writer_pid": None,
        "current_writer_elapsed": "n/a",
        "holder_count": "unknown",
        "long_job_heartbeat_status": snapshot_label,
        "long_job_heartbeat_age": snapshot_age,
        "long_job_stage": "snapshot",
        "long_job_processed": 0,
        "long_job_total": "unknown",
        "current_writer_command": "Use /api/db-writer-monitor for a live read.",
        "recommended_next_command_after_finish": "kalshi-bot db-writer-monitor",
        "recommended_next_action": (
            "This page is bounded for speed; open the monitor API/page for live DB holder detail."
        ),
    }
    provenance = None
    if settings.prov11_dashboard_preview_enabled:
        provenance = build_provenance_diagnostics(
            session,
            event_limit=50,
            execution_enabled=settings.execution_enabled,
        )
    return {
        "shell_context": shell_context,
        "phase_3x_status": {
            "decision": DECISION_INCOMPLETE,
            "mode": settings.phase_3x_mode,
            "route_count": len(ROUTE_INVENTORY),
            "component_count": 0,
            "next_action": "Use the Phase 3X report/API for the full audit.",
            "blockers": [
                "Full release audit is intentionally loaded from reports/APIs, not this fast page."
            ],
        },
        "phase_3w_status": {
            "overall_status": "INCOMPLETE",
        },
        "phase_3v_status": {
            "decision": "NOT_READY",
        },
        "database_status": {
            "status": "SNAPSHOT",
        },
        "workspace_guard": workspace_guard,
        "long_job_monitor": {
            "phase3ay": {
                "status": "SNAPSHOT_ONLY",
                "active_pid": None,
                "elapsed_label": "n/a",
                "budget_state": "n/a",
                "budget_label": "n/a",
                "progress_percent": None,
                "latest_status": snapshot_label,
            },
            "post_refresh_hook": {"status": "SNAPSHOT_ONLY", "pid": None},
            "recommended_next_action": (
                "Open Long jobs for live process detail; this landing page stays bounded."
            ),
        },
        "db_writer_monitor": db_monitor,
        "system_remediation": {
            "report_href": REPORT_LINKS.system_remediation,
            "status": "REPORT_LINKED",
            "paper_only_confirmed": True,
            "freshness_status": market_label,
            "database_status": db_backend,
            "recommendations": [
                "Use the remediation report for full checks.",
                "Run kalshi-bot ui-shell-status-refresh after long jobs finish.",
            ],
            "next_commands": [
                "kalshi-bot ui-shell-status-refresh",
                "kalshi-bot db-writer-monitor",
            ],
        },
        "opportunity_links": _opportunity_links_health_summary(
            session,
            settings=settings,
        ),
        "audit": {
            "routes": list(ROUTE_INVENTORY),
        },
        "provenance_diagnostics": provenance,
    }


def _db_backend_label(database_url: Any) -> str:
    text = str(database_url or "").lower()
    if "sqlite" in text or text.endswith(".db"):
        return "SQLite"
    if "postgres" in text:
        return "Postgres"
    return "unknown"


def _snapshot_matches_market_count(session: Session, snapshot: dict[str, Any]) -> bool:
    cards = snapshot.get("summary_cards") or []
    snapshot_market_count = _snapshot_card_value(cards, "Markets")
    snapshot_leg_count = _snapshot_card_value(cards, "Parsed Legs")
    if snapshot_market_count is None:
        return False
    current_market_count = int(session.scalar(select(func.count()).select_from(Market)) or 0)
    current_leg_count = int(session.scalar(select(func.count()).select_from(MarketLeg)) or 0)
    try:
        market_count_matches = int(snapshot_market_count) == current_market_count
        leg_count_matches = (
            snapshot_leg_count is None or int(snapshot_leg_count) == current_leg_count
        )
        return market_count_matches and leg_count_matches
    except (TypeError, ValueError):
        return False


def _snapshot_card_value(cards: list[Any], label: str) -> Any | None:
    return next(
        (
            card.get("value")
            for card in cards
            if isinstance(card, dict) and card.get("label") == label
        ),
        None,
    )


def _shell_status_snapshot_mtime() -> float:
    try:
        return DEFAULT_SHELL_STATUS_SNAPSHOT_PATH.stat().st_mtime
    except OSError:
        return 0.0


def create_router(
    session_factory: Callable[[], Session] | None = None,
    settings: Settings | None = None,
) -> APIRouter:
    router = APIRouter()
    resolved_settings = settings or get_settings()
    default_shell_context = load_shell_status_context(settings=resolved_settings)
    templates.env.globals["default_shell_context"] = default_shell_context
    if session_factory is None:
        engine = init_db()
        session_factory = get_session_factory(engine)

    def get_session() -> Iterator[Session]:
        session = session_factory()
        try:
            yield session
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def get_service(session: Annotated[Session, Depends(get_session)]) -> DecisionUiService:
        return DecisionUiService(session, settings=resolved_settings)

    shell_context_cache: dict[str, Any] = {
        "created_at": time.monotonic(),
        "snapshot_mtime": _shell_status_snapshot_mtime(),
        "value": default_shell_context,
    }
    portfolio_context_cache: dict[str, Any] = {
        "created_at": 0.0,
        "value": None,
    }

    def shell_context_for(service: DecisionUiService) -> dict[str, Any]:
        now = time.monotonic()
        snapshot_mtime = _shell_status_snapshot_mtime()
        cached = shell_context_cache.get("value")
        created_at = float(shell_context_cache.get("created_at") or 0.0)
        cached_mtime = float(shell_context_cache.get("snapshot_mtime") or 0.0)
        if (
            isinstance(cached, dict)
            and snapshot_mtime == cached_mtime
            and now - created_at <= SHELL_CONTEXT_CACHE_SECONDS
        ):
            return cached
        context = load_shell_status_context(settings=resolved_settings)
        shell_context_cache["created_at"] = now
        shell_context_cache["snapshot_mtime"] = snapshot_mtime
        shell_context_cache["value"] = context
        return context

    def portfolio_context_for(service: DecisionUiService) -> dict[str, Any]:
        now = time.monotonic()
        cached = portfolio_context_cache.get("value")
        created_at = float(portfolio_context_cache.get("created_at") or 0.0)
        if (
            isinstance(cached, dict)
            and now - created_at <= PORTFOLIO_CONTEXT_CACHE_SECONDS
        ):
            context = dict(cached)
            context["server_cache"] = {
                "status": "HIT",
                "age_seconds": max(0, int(now - created_at)),
                "ttl_seconds": PORTFOLIO_CONTEXT_CACHE_SECONDS,
            }
            return context
        context = portfolio_summary_fast(service.session, positions_limit=50, series_limit=8)
        portfolio_context_cache["created_at"] = now
        portfolio_context_cache["value"] = context
        context = dict(context)
        context["server_cache"] = {
            "status": "MISS",
            "age_seconds": 0,
            "ttl_seconds": PORTFOLIO_CONTEXT_CACHE_SECONDS,
        }
        return context

    def dashboard_snapshot_response(
        service: DecisionUiService,
        *,
        payload: dict[str, Any] | None = None,
        panel_id: str = "snapshot",
    ) -> dict[str, Any]:
        payload = _dashboard_payload(payload)
        snapshot = build_dashboard_snapshot(
            service.session,
            settings=resolved_settings,
            filters=payload.get("filters"),
            as_of=payload.get("as_of"),
        )
        data = snapshot if panel_id == "snapshot" else panel_data(snapshot, panel_id)
        return dashboard_panel_response(
            snapshot,
            panel_id=panel_id,
            data=data,
            filters=snapshot["effective_filters"],
            sort=payload.get("sort") or {},
            pagination=payload.get("pagination"),
        )

    def fast_dashboard_snapshot_response() -> dict[str, Any]:
        generated_at = utc_now()
        guard = build_workspace_consistency_guard(settings=resolved_settings)
        summary = dict(guard.get("summary") or {})
        database_fingerprint = str(summary.get("database_fingerprint") or "UNKNOWN")
        monitor = db_writer_monitor(settings=resolved_settings)
        snapshot_id = (
            "bounded-current-"
            f"{database_fingerprint.replace(':', '-')}-"
            f"{int(generated_at.timestamp())}"
        )
        source_watermark = {
            "source_id": "workspace_guard",
            "source_name": "Workspace guard bounded UI snapshot",
            "database_fingerprint": database_fingerprint,
            "watermark_at": generated_at.isoformat(),
            "generated_at": generated_at.isoformat(),
            "required": True,
            "freshness_status": "FRESH",
            "completeness_status": "COMPLETE",
            "lifecycle_status": "ACTIVE",
            "next_command": "kalshi-bot phase3bb-r32-cloud-ui-dashboard-truth-scheduler-status-verification",
        }
        return {
            "schema_version": API_SCHEMA_VERSION,
            "request_id": snapshot_id,
            "dashboard_snapshot_id": snapshot_id,
            "generated_at": generated_at.isoformat(),
            "panel_as_of": generated_at.isoformat(),
            "effective_filters": {},
            "source_watermarks": [source_watermark],
            "freshness_status": "FRESH",
            "completeness_status": "COMPLETE",
            "warnings": [
                "GET /api/dashboard/v1/snapshots/current uses a bounded operator snapshot; "
                "POST panel query endpoints build the full dashboard."
            ],
            "read_only_boundary": {
                "read_only": True,
                "allow_exchange_write_endpoints": False,
                "allow_order_actions": False,
            },
            "data": {
                "snapshot_mode": "BOUNDED_OPERATOR_STATUS",
                "workspace_guard_status": summary.get("status"),
                "missing_required_commands": summary.get("missing_required_commands"),
                "critical_findings": summary.get("critical_findings"),
                "database_fingerprint": database_fingerprint,
                "db_writer_status": monitor.get("status"),
                "db_writer_safe_to_start_write": monitor.get("safe_to_start_write"),
                "current_writer_pid": monitor.get("current_writer_pid"),
                "read_only": True,
            },
        }

    def personal_trader_api_response(
        brief: dict[str, Any],
        *,
        data: Any | None = None,
        warnings: list[str] | None = None,
    ) -> dict[str, Any]:
        return {
            "request_id": brief["query"]["query_id"],
            "trace_id": brief["brief_id"],
            "schema_version": PHASE_3U_API_SCHEMA,
            "as_of": brief["as_of"],
            "server_time": build_personal_trade_brief_time(),
            "timezone": brief["timezone"],
            "snapshot_id": brief["snapshot"]["snapshot_id"],
            "freshness": _phase_3u_freshness(brief),
            "completeness": brief["snapshot"]["consistency_grade"],
            "warnings": warnings or [],
            "data": brief if data is None else data,
        }

    @router.get("/", response_class=HTMLResponse)
    def root_today_workspace(
        request: Request,
        service: Annotated[DecisionUiService, Depends(get_service)],
    ) -> HTMLResponse:
        context = service.today()
        return templates.TemplateResponse(
            request,
            "today.html",
            {"request": request, **context},
        )

    @router.get("/dashboard", response_class=HTMLResponse)
    def dashboard(
        request: Request,
        service: Annotated[DecisionUiService, Depends(get_service)],
    ) -> HTMLResponse:
        context = service.dashboard()
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {"request": request, **context},
        )

    @router.get("/opportunities", response_class=HTMLResponse)
    def opportunities_dashboard(
        request: Request,
        service: Annotated[DecisionUiService, Depends(get_service)],
    ) -> HTMLResponse:
        started_at = time.perf_counter()
        logger.debug("opportunities_dashboard context start")
        context = service.opportunities_dashboard()
        logger.debug(
            "opportunities_dashboard context ready in %.2fs",
            time.perf_counter() - started_at,
        )
        template_name = (
            "opportunities_fast.html" if context.get("fast_bounded") else "opportunities.html"
        )
        response = templates.TemplateResponse(
            request,
            template_name,
            {"request": request, **context},
        )
        logger.debug(
            "opportunities_dashboard response ready in %.2fs",
            time.perf_counter() - started_at,
        )
        response.headers["Cache-Control"] = "no-store, max-age=0"
        response.headers["Pragma"] = "no-cache"
        return response

    @router.get("/today", response_class=HTMLResponse)
    def today_workspace(
        request: Request,
        service: Annotated[DecisionUiService, Depends(get_service)],
    ) -> HTMLResponse:
        started_at = time.perf_counter()
        logger.debug("today_workspace context start")
        context = service.today()
        logger.debug("today_workspace context ready in %.2fs", time.perf_counter() - started_at)
        return templates.TemplateResponse(
            request,
            "today.html",
            {"request": request, **context},
        )

    @router.get("/institutional", response_class=HTMLResponse)
    def institutional_dashboard(
        request: Request,
        service: Annotated[DecisionUiService, Depends(get_service)],
    ) -> HTMLResponse:
        snapshot = build_dashboard_snapshot(service.session, settings=resolved_settings)
        return templates.TemplateResponse(
            request,
            "institutional_dashboard.html",
            {
                "request": request,
                "snapshot": snapshot,
                "shell_context": shell_context_for(service),
                "report_links": REPORT_LINKS,
            },
        )

    @router.get("/live-readiness", response_class=HTMLResponse)
    def live_readiness_dashboard(
        request: Request,
        service: Annotated[DecisionUiService, Depends(get_service)],
    ) -> HTMLResponse:
        status = live_readiness_status(service.session, settings=resolved_settings)
        latest = latest_live_readiness_payload(service.session)
        return templates.TemplateResponse(
            request,
            "live_readiness.html",
            {
                "request": request,
                "status": status,
                "latest": latest,
                "report_links": REPORT_LINKS,
                "shell_context": shell_context_for(service),
            },
        )

    @router.get("/api/live-readiness/status")
    def live_readiness_status_api(
        service: Annotated[DecisionUiService, Depends(get_service)],
    ) -> dict:
        return jsonable_encoder(
            {
                "ok": True,
                "status": live_readiness_status(service.session, settings=resolved_settings),
                "latest": latest_live_readiness_payload(service.session),
                "read_only": True,
            }
        )

    @router.post("/api/live-readiness/review")
    def live_readiness_review_api(
        service: Annotated[DecisionUiService, Depends(get_service)],
        payload: Annotated[dict[str, Any] | None, Body()] = None,
    ) -> JSONResponse:
        try:
            target_stage = (payload or {}).get("target_stage")
            path = generate_live_readiness_report(
                service.session,
                settings=resolved_settings,
                target_stage=target_stage,
            )
            latest = latest_live_readiness_payload(service.session)
            return JSONResponse(
                content=jsonable_encoder(
                    {
                        "ok": True,
                        "message": "Live readiness review completed.",
                        "report_path": str(path),
                        "decision": latest,
                        "read_only": True,
                    }
                )
            )
        except Exception as exc:  # noqa: BLE001 - action endpoint must return JSON.
            logger.exception("Live readiness review failed")
            return JSONResponse(
                status_code=500,
                content={
                    "ok": False,
                    "message": "Live readiness review failed.",
                    "error": str(exc),
                    "next_action": (
                        "Run kalshi-bot live-readiness-review from the terminal "
                        "and inspect logs."
                    ),
                },
            )

    @router.get("/system-certification", response_class=HTMLResponse)
    def system_certification_dashboard(
        request: Request,
        service: Annotated[DecisionUiService, Depends(get_service)],
    ) -> HTMLResponse:
        status = system_certification_card(service.session, settings=resolved_settings)
        return templates.TemplateResponse(
            request,
            "system_certification.html",
            {
                "request": request,
                "status": status,
                "report_links": REPORT_LINKS,
                "shell_context": shell_context_for(service),
            },
        )

    @router.get("/system/certification", response_class=HTMLResponse)
    def system_certification_alias() -> RedirectResponse:
        return RedirectResponse("/system-certification", status_code=307)

    @router.get("/system", response_class=HTMLResponse)
    @router.get("/system/health", response_class=HTMLResponse)
    def system_health_dashboard(
        request: Request,
        service: Annotated[DecisionUiService, Depends(get_service)],
    ) -> HTMLResponse:
        shell_context = shell_context_for(service)
        status_payload = _fast_system_health_payload(
            settings=resolved_settings,
            session=service.session,
            shell_context=shell_context,
        )
        return templates.TemplateResponse(
            request,
            "system_health.html",
            {
                "request": request,
                "report_links": REPORT_LINKS,
                **status_payload,
            },
        )

    @router.get("/system/long-jobs", response_class=HTMLResponse)
    def long_jobs_dashboard(
        request: Request,
        service: Annotated[DecisionUiService, Depends(get_service)],
    ) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "long_jobs.html",
            {
                "request": request,
                "report_links": REPORT_LINKS,
                "shell_context": shell_context_for(service),
                "monitor": build_long_job_monitor(settings=resolved_settings),
            },
        )

    @router.get("/system/progress", response_class=HTMLResponse)
    def process_progress_dashboard(
        request: Request,
        service: Annotated[DecisionUiService, Depends(get_service)],
    ) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "progress_dashboard.html",
            {
                "request": request,
                "shell_context": shell_context_for(service),
                "progress": get_cached_progress_dashboard(),
            },
        )

    @router.get("/api/system/progress")
    def process_progress_api() -> dict[str, Any]:
        return jsonable_encoder(get_cached_progress_dashboard())

    @router.get("/system/progress/certification-export/{kind}")
    def certification_timeline_export(kind: str) -> FileResponse:
        path = timeline_export_path(certification_reports_root(), kind)
        if path is None:
            raise HTTPException(status_code=404, detail="Certified timeline export unavailable")
        media_type = "application/json" if kind == "json" else "text/csv"
        return FileResponse(path, media_type=media_type, filename=path.name)

    @router.get("/system/evidence", response_class=HTMLResponse)
    def evidence_catalog_dashboard(request: Request, service: Annotated[DecisionUiService, Depends(get_service)]) -> HTMLResponse:
        return templates.TemplateResponse(request, "evidence_catalog.html", {"request":request,"shell_context":shell_context_for(service),"catalog":get_cached_evidence_catalog()})

    @router.get("/system/evidence/{artifact_id}", response_class=HTMLResponse)
    def evidence_artifact_dashboard(artifact_id: str, request: Request, service: Annotated[DecisionUiService, Depends(get_service)]) -> HTMLResponse:
        try:
            artifact = load_evidence_artifact(artifact_id)
        except EvidenceRejected as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return templates.TemplateResponse(request, "evidence_detail.html", {"request":request,"shell_context":shell_context_for(service),"artifact":artifact})

    @router.get("/api/system/evidence")
    def evidence_catalog_api() -> dict[str, Any]:
        return jsonable_encoder(get_cached_evidence_catalog())

    @router.get("/api/system/evidence/{artifact_id}")
    def evidence_artifact_api(artifact_id: str) -> dict[str, Any]:
        try:
            return jsonable_encoder(load_evidence_artifact(artifact_id))
        except EvidenceRejected as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.get("/system/provenance", response_class=HTMLResponse)
    def provenance_diagnostics_dashboard(
        request: Request,
        service: Annotated[DecisionUiService, Depends(get_service)],
    ) -> HTMLResponse:
        if not resolved_settings.prov11_dashboard_preview_enabled:
            raise HTTPException(status_code=404, detail="PROV-11 preview is disabled")
        diagnostics = build_provenance_diagnostics(
            service.session,
            event_limit=250,
            execution_enabled=resolved_settings.execution_enabled,
        )
        drift_alerts = build_provenance_drift_alerts(
            service.session,
            ticker_limit=50,
            stale_after_minutes=resolved_settings.prov12_provenance_stale_after_minutes,
        ) if resolved_settings.prov12_decision_trace_preview_enabled else None
        return templates.TemplateResponse(
            request,
            "provenance_diagnostics.html",
            {
                "request": request,
                "diagnostics": diagnostics,
                "drift_alerts": drift_alerts,
                "shell_context": shell_context_for(service),
            },
        )

    @router.get("/api/provenance/diagnostics")
    def provenance_diagnostics_api(
        service: Annotated[DecisionUiService, Depends(get_service)],
    ) -> dict[str, Any]:
        if not resolved_settings.prov11_dashboard_preview_enabled:
            raise HTTPException(status_code=404, detail="PROV-11 preview is disabled")
        return jsonable_encoder(
            build_provenance_diagnostics(
                service.session,
                event_limit=250,
                execution_enabled=resolved_settings.execution_enabled,
            )
        )

    @router.get("/system/provenance/{ticker}", response_class=HTMLResponse)
    def market_provenance_trace_dashboard(
        ticker: str,
        request: Request,
        service: Annotated[DecisionUiService, Depends(get_service)],
    ) -> HTMLResponse:
        if not resolved_settings.prov12_decision_trace_preview_enabled:
            raise HTTPException(status_code=404, detail="PROV-12 preview is disabled")
        trace = build_market_decision_trace(
            service.session,
            ticker,
            stale_after_minutes=resolved_settings.prov12_provenance_stale_after_minutes,
        )
        if trace["status"] == "NOT_FOUND":
            raise HTTPException(status_code=404, detail="Ticker provenance trace not found")
        return templates.TemplateResponse(
            request,
            "provenance_trace.html",
            {
                "request": request,
                "trace": trace,
                "shell_context": shell_context_for(service),
            },
        )

    @router.get("/api/provenance/traces/{ticker}")
    def market_provenance_trace_api(
        ticker: str,
        service: Annotated[DecisionUiService, Depends(get_service)],
    ) -> dict[str, Any]:
        if not resolved_settings.prov12_decision_trace_preview_enabled:
            raise HTTPException(status_code=404, detail="PROV-12 preview is disabled")
        trace = build_market_decision_trace(
            service.session,
            ticker,
            stale_after_minutes=resolved_settings.prov12_provenance_stale_after_minutes,
        )
        if trace["status"] == "NOT_FOUND":
            raise HTTPException(status_code=404, detail="Ticker provenance trace not found")
        return jsonable_encoder(trace)

    @router.get("/api/provenance/drift-alerts")
    def provenance_drift_alerts_api(
        service: Annotated[DecisionUiService, Depends(get_service)],
    ) -> dict[str, Any]:
        if not resolved_settings.prov12_decision_trace_preview_enabled:
            raise HTTPException(status_code=404, detail="PROV-12 preview is disabled")
        return jsonable_encoder(build_provenance_drift_alerts(
            service.session,
            ticker_limit=50,
            stale_after_minutes=resolved_settings.prov12_provenance_stale_after_minutes,
        ))

    @router.get("/api/db-writer-monitor")
    def db_writer_monitor_api() -> dict:
        return jsonable_encoder(
            {
                "ok": True,
                "monitor": db_writer_monitor(settings=resolved_settings),
                "read_only": True,
            }
        )

    @router.get("/api/workspace-guard")
    def workspace_guard_api() -> dict:
        return jsonable_encoder(
            {
                "ok": True,
                "guard": build_workspace_consistency_guard(settings=resolved_settings),
                "read_only": True,
            }
        )

    @router.get("/api/long-jobs/status")
    def long_jobs_status_api() -> dict:
        return jsonable_encoder(
            {
                "ok": True,
                "monitor": build_long_job_monitor(settings=resolved_settings),
                "read_only": True,
            }
        )

    @router.get("/api/phase3x/status")
    def phase_3x_status_api(
        service: Annotated[DecisionUiService, Depends(get_service)],
    ) -> dict:
        return jsonable_encoder(
            {
                "ok": True,
                "status": phase_3x_card(service.session, settings=resolved_settings),
                "live_trading_authorized": False,
            }
        )

    @router.get("/risk", response_class=HTMLResponse)
    def risk_alias() -> RedirectResponse:
        return RedirectResponse("/institutional#risk", status_code=307)

    @router.get("/trades", response_class=HTMLResponse)
    def trades_alias() -> RedirectResponse:
        return RedirectResponse("/portfolio", status_code=307)

    @router.get("/journal", response_class=HTMLResponse)
    def journal_alias() -> RedirectResponse:
        return RedirectResponse("/memory", status_code=307)

    @router.get("/portfolio/exposures", response_class=HTMLResponse)
    def exposures_alias() -> RedirectResponse:
        return RedirectResponse("/portfolio", status_code=307)

    @router.get("/settings/appearance", response_class=HTMLResponse)
    def appearance_alias() -> RedirectResponse:
        return RedirectResponse("/settings", status_code=307)

    @router.get("/settings/views", response_class=HTMLResponse)
    def views_alias() -> RedirectResponse:
        return RedirectResponse("/settings", status_code=307)

    @router.get("/research/features", response_class=HTMLResponse)
    def research_features_alias() -> RedirectResponse:
        return RedirectResponse("/research", status_code=307)

    @router.get("/research/synthetic-markets", response_class=HTMLResponse)
    def synthetic_markets_alias() -> RedirectResponse:
        return RedirectResponse("/research", status_code=307)

    @router.get("/research/roi-policy", response_class=HTMLResponse)
    def roi_policy_alias() -> RedirectResponse:
        return RedirectResponse("/research", status_code=307)

    @router.get("/api/system-certification/status")
    def system_certification_status_api(
        service: Annotated[DecisionUiService, Depends(get_service)],
    ) -> dict:
        return jsonable_encoder(
            {
                "ok": True,
                "status": system_certification_card(service.session, settings=resolved_settings),
                "read_only": True,
            }
        )

    @router.post("/api/system-certification/run")
    def system_certification_run_api(
        service: Annotated[DecisionUiService, Depends(get_service)],
        payload: Annotated[dict[str, Any] | None, Body()] = None,
    ) -> JSONResponse:
        try:
            output_dir = (payload or {}).get("output_dir") or "reports/system_certification"
            report = generate_system_certification_report(
                service.session,
                settings=resolved_settings,
                output_dir=output_dir,
            )
            return JSONResponse(
                content=jsonable_encoder(
                    {
                        "ok": True,
                        "message": "System certification audit completed.",
                        "overall_status": report["overall_status"],
                        "report_path": report["phase_3v_handoff"]["evidence_package"],
                        "live_trading_authorized": False,
                    }
                )
            )
        except Exception as exc:  # noqa: BLE001 - action endpoint must return JSON.
            logger.exception("System certification run failed")
            return JSONResponse(
                status_code=500,
                content={
                    "ok": False,
                    "message": "System certification run failed.",
                    "error": str(exc),
                    "next_action": "Run kalshi-bot system-certification-run from the terminal.",
                },
            )

    @router.get("/personal-trader", response_class=HTMLResponse)
    def personal_trader_dashboard(
        request: Request,
        service: Annotated[DecisionUiService, Depends(get_service)],
    ) -> HTMLResponse:
        stored = latest_brief(service.session)
        brief = stored or build_personal_trade_brief(
            service.session,
            settings=resolved_settings,
            persist=False,
        )
        return templates.TemplateResponse(
            request,
            "personal_trader.html",
            {
                "request": request,
                "brief": brief,
                "conversation": conversational_response(brief),
                "audit_events": recommendation_audit_events(
                    service.session,
                    brief_id=brief["brief_id"],
                )[:20],
                "report_links": REPORT_LINKS,
            },
        )

    @router.post("/personal-trader/query")
    def personal_trader_query(
        service: Annotated[DecisionUiService, Depends(get_service)],
        payload: Annotated[dict[str, Any] | None, Body()] = None,
    ) -> dict:
        payload = _personal_trader_payload(payload)
        brief = build_personal_trade_brief(
            service.session,
            settings=resolved_settings,
            natural_language_query=str(
                payload.get("natural_language_query") or "What should I trade today?"
            ),
            as_of=payload.get("as_of"),
            timezone=payload.get("timezone"),
            maximum_recommendations=payload.get("maximum_recommendations"),
            category_include=payload.get("category_include"),
            category_exclude=payload.get("category_exclude"),
            market_include=payload.get("market_include"),
            market_exclude=payload.get("market_exclude"),
            persist=True,
        )
        service.session.commit()
        return jsonable_encoder(personal_trader_api_response(brief))

    @router.get("/personal-trader/briefs/{brief_id}/recommendations")
    def personal_trader_brief_recommendations(
        brief_id: str,
        service: Annotated[DecisionUiService, Depends(get_service)],
    ) -> dict:
        brief = brief_by_id(service.session, brief_id)
        if brief is None:
            raise _phase_3u_not_found("BRIEF_NOT_FOUND", "Personal trader brief not found.")
        return jsonable_encoder(
            personal_trader_api_response(
                brief,
                data=brief["recommendations"],
                warnings=["Returning recommendation cards from immutable audit memory."],
            )
        )

    @router.get("/personal-trader/briefs/{brief_id}/rejections")
    def personal_trader_brief_rejections(
        brief_id: str,
        service: Annotated[DecisionUiService, Depends(get_service)],
    ) -> dict:
        brief = brief_by_id(service.session, brief_id)
        if brief is None:
            raise _phase_3u_not_found("BRIEF_NOT_FOUND", "Personal trader brief not found.")
        return jsonable_encoder(
            personal_trader_api_response(brief, data=brief["rejection_summary"])
        )

    @router.get("/personal-trader/briefs/{brief_id}")
    def personal_trader_brief(
        brief_id: str,
        service: Annotated[DecisionUiService, Depends(get_service)],
    ) -> dict:
        brief = brief_by_id(service.session, brief_id)
        if brief is None:
            raise _phase_3u_not_found("BRIEF_NOT_FOUND", "Personal trader brief not found.")
        return jsonable_encoder(personal_trader_api_response(brief))

    @router.get("/personal-trader/recommendations/{recommendation_id}/eligibility")
    def personal_trader_recommendation_eligibility(
        recommendation_id: str,
        service: Annotated[DecisionUiService, Depends(get_service)],
    ) -> dict:
        recommendation = recommendation_by_id(service.session, recommendation_id)
        if recommendation is None:
            raise _phase_3u_not_found(
                "RECOMMENDATION_NOT_FOUND",
                "Personal trader recommendation not found.",
            )
        return jsonable_encoder(
            {
                "schema_version": PHASE_3U_API_SCHEMA,
                "recommendation_id": recommendation_id,
                "status": recommendation["status"],
                "phase_3s": recommendation["model_policy"]["phase_3s_action"],
                "phase_3m_quantity": recommendation["model_policy"]["phase_3m_proposed_quantity"],
                "phase_3n_decision": recommendation["model_policy"]["phase_3n_decision"],
                "phase_3n_quantity": recommendation["model_policy"]["phase_3n_approved_quantity"],
                "reason_codes": recommendation["model_policy"]["phase_3n_reason_codes"],
                "read_only": True,
            }
        )

    @router.get("/personal-trader/recommendations/{recommendation_id}/lineage")
    def personal_trader_recommendation_lineage(
        recommendation_id: str,
        service: Annotated[DecisionUiService, Depends(get_service)],
    ) -> dict:
        recommendation = recommendation_by_id(service.session, recommendation_id)
        if recommendation is None:
            raise _phase_3u_not_found(
                "RECOMMENDATION_NOT_FOUND",
                "Personal trader recommendation not found.",
            )
        return jsonable_encoder(
            {
                "schema_version": PHASE_3U_API_SCHEMA,
                "recommendation_id": recommendation_id,
                "lineage": recommendation["lineage"],
                "evidence_ids": recommendation["explanation"]["evidence_ids"],
                "read_only": True,
            }
        )

    @router.get("/personal-trader/recommendations/{recommendation_id}")
    def personal_trader_recommendation(
        recommendation_id: str,
        service: Annotated[DecisionUiService, Depends(get_service)],
    ) -> dict:
        recommendation = recommendation_by_id(service.session, recommendation_id)
        if recommendation is None:
            raise _phase_3u_not_found(
                "RECOMMENDATION_NOT_FOUND",
                "Personal trader recommendation not found.",
            )
        return jsonable_encoder(
            {
                "schema_version": PHASE_3U_API_SCHEMA,
                "recommendation_id": recommendation_id,
                "data": recommendation,
                "read_only": True,
            }
        )

    @router.get("/personal-trader/profiles/{profile_id}")
    def personal_trader_profile(profile_id: str) -> dict:
        if profile_id not in {"local", "profile-local-v1"}:
            raise _phase_3u_not_found("PROFILE_NOT_FOUND", "Personal trader profile not found.")
        return jsonable_encoder(
            {
                "schema_version": PHASE_3U_API_SCHEMA,
                "profile_id": profile_id,
                "profile_version": "profile-local-v1",
                "risk_preference_override": "LOWER_ONLY",
                "can_raise_risk_limits": False,
                "read_only": True,
            }
        )

    @router.get("/api/dashboard/v1/snapshots/current")
    def dashboard_snapshot_current(
        service: Annotated[DecisionUiService, Depends(get_service)],
    ) -> dict:
        del service
        return jsonable_encoder(fast_dashboard_snapshot_response())

    @router.post("/api/dashboard/v1/query/snapshot")
    def dashboard_query_snapshot(
        service: Annotated[DecisionUiService, Depends(get_service)],
        payload: Annotated[dict[str, Any] | None, Body()] = None,
    ) -> dict:
        return jsonable_encoder(dashboard_snapshot_response(service, payload=payload))

    @router.post("/api/dashboard/v1/query/heatmap")
    def dashboard_query_heatmap(
        service: Annotated[DecisionUiService, Depends(get_service)],
        payload: Annotated[dict[str, Any] | None, Body()] = None,
    ) -> dict:
        return jsonable_encoder(
            dashboard_snapshot_response(service, payload=payload, panel_id="market_heatmap")
        )

    @router.post("/api/dashboard/v1/query/opportunities")
    def dashboard_query_opportunities(
        service: Annotated[DecisionUiService, Depends(get_service)],
        payload: Annotated[dict[str, Any] | None, Body()] = None,
    ) -> dict:
        return jsonable_encoder(
            dashboard_snapshot_response(service, payload=payload, panel_id="opportunity_scanner")
        )

    @router.post("/api/dashboard/v1/query/model-matrix")
    def dashboard_query_model_matrix(
        service: Annotated[DecisionUiService, Depends(get_service)],
        payload: Annotated[dict[str, Any] | None, Body()] = None,
    ) -> dict:
        return jsonable_encoder(
            dashboard_snapshot_response(service, payload=payload, panel_id="model_matrix")
        )

    @router.post("/api/dashboard/v1/query/exposures")
    def dashboard_query_exposures(
        service: Annotated[DecisionUiService, Depends(get_service)],
        payload: Annotated[dict[str, Any] | None, Body()] = None,
    ) -> dict:
        return jsonable_encoder(
            dashboard_snapshot_response(service, payload=payload, panel_id="exposure_maps")
        )

    @router.post("/api/dashboard/v1/query/risk-limits")
    def dashboard_query_risk_limits(
        service: Annotated[DecisionUiService, Depends(get_service)],
        payload: Annotated[dict[str, Any] | None, Body()] = None,
    ) -> dict:
        return jsonable_encoder(
            dashboard_snapshot_response(service, payload=payload, panel_id="risk_waterfall")
        )

    @router.post("/api/dashboard/v1/query/trades")
    def dashboard_query_trades(
        service: Annotated[DecisionUiService, Depends(get_service)],
        payload: Annotated[dict[str, Any] | None, Body()] = None,
    ) -> dict:
        return jsonable_encoder(
            dashboard_snapshot_response(service, payload=payload, panel_id="trade_blotter")
        )

    @router.post("/api/dashboard/v1/query/system-health")
    def dashboard_query_system_health(
        service: Annotated[DecisionUiService, Depends(get_service)],
        payload: Annotated[dict[str, Any] | None, Body()] = None,
    ) -> dict:
        return jsonable_encoder(
            dashboard_snapshot_response(service, payload=payload, panel_id="system_health")
        )

    @router.post("/api/dashboard/v1/query/journals")
    def dashboard_query_journals(
        service: Annotated[DecisionUiService, Depends(get_service)],
        payload: Annotated[dict[str, Any] | None, Body()] = None,
    ) -> dict:
        return jsonable_encoder(
            dashboard_snapshot_response(service, payload=payload, panel_id="research_layers")
        )

    @router.post("/api/dashboard/v1/query/research")
    def dashboard_query_research(
        service: Annotated[DecisionUiService, Depends(get_service)],
        payload: Annotated[dict[str, Any] | None, Body()] = None,
    ) -> dict:
        return jsonable_encoder(
            dashboard_snapshot_response(service, payload=payload, panel_id="research_layers")
        )

    @router.get("/api/dashboard/v1/markets/{market_id}")
    def dashboard_market_detail(
        market_id: str,
        service: Annotated[DecisionUiService, Depends(get_service)],
    ) -> dict:
        market = service.session.get(Market, market_id)
        detail = service.opportunity_detail(market_id)
        if market is None and detail is None:
            raise HTTPException(status_code=404, detail="Market not found.")
        snapshot = build_dashboard_snapshot(service.session, settings=resolved_settings)
        return jsonable_encoder(
            dashboard_panel_response(
                snapshot,
                panel_id="market_detail",
                data={
                    "market": jsonable_encoder(market) if market else None,
                    "detail": jsonable_encoder(detail) if detail else None,
                },
            )
        )

    @router.get("/api/dashboard/v1/forecasts/{forecast_id}")
    def dashboard_forecast_detail(
        forecast_id: str,
        service: Annotated[DecisionUiService, Depends(get_service)],
    ) -> dict:
        forecast = (
            service.session.get(Forecast, int(forecast_id))
            if forecast_id.isdigit()
            else None
        )
        if forecast is None:
            raise HTTPException(status_code=404, detail="Forecast not found.")
        snapshot = build_dashboard_snapshot(service.session, settings=resolved_settings)
        return jsonable_encoder(
            dashboard_panel_response(
                snapshot,
                panel_id="forecast_detail",
                data=jsonable_encoder(forecast),
            )
        )

    @router.get("/api/dashboard/v1/opportunities/{opportunity_id}")
    def dashboard_opportunity_detail(
        opportunity_id: str,
        service: Annotated[DecisionUiService, Depends(get_service)],
    ) -> dict:
        detail = service.opportunity_detail(opportunity_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="Opportunity not found.")
        snapshot = build_dashboard_snapshot(service.session, settings=resolved_settings)
        return jsonable_encoder(
            dashboard_panel_response(
                snapshot,
                panel_id="opportunity_detail",
                data=jsonable_encoder(detail),
            )
        )

    @router.get("/api/dashboard/v1/trades/{trade_id}")
    def dashboard_trade_detail(
        trade_id: str,
        service: Annotated[DecisionUiService, Depends(get_service)],
    ) -> dict:
        order = service.session.get(PaperOrder, int(trade_id)) if trade_id.isdigit() else None
        if order is None:
            raise HTTPException(status_code=404, detail="Trade not found.")
        snapshot = build_dashboard_snapshot(service.session, settings=resolved_settings)
        return jsonable_encoder(
            dashboard_panel_response(
                snapshot,
                panel_id="trade_detail",
                data=jsonable_encoder(order),
            )
        )

    @router.get("/api/dashboard/v1/models/{model_id}/versions/{version}")
    def dashboard_model_detail(
        model_id: str,
        version: str,
        service: Annotated[DecisionUiService, Depends(get_service)],
    ) -> dict:
        rows = [
            row
            for row in model_status_rows(service.session)
            if row.get("model_name") == model_id
            and str(row.get("version") or row.get("model_version") or version) == version
        ]
        if not rows:
            rows = [
                row
                for row in model_status_rows(service.session)
                if row.get("model_name") == model_id
            ]
        if not rows:
            raise HTTPException(status_code=404, detail="Model not found.")
        snapshot = build_dashboard_snapshot(service.session, settings=resolved_settings)
        return jsonable_encoder(
            dashboard_panel_response(snapshot, panel_id="model_detail", data=rows[0])
        )

    @router.get("/api/dashboard/v1/stream")
    def dashboard_stream(
        service: Annotated[DecisionUiService, Depends(get_service)],
    ) -> StreamingResponse:
        snapshot = build_dashboard_snapshot(service.session, settings=resolved_settings)
        response = dashboard_panel_response(snapshot, panel_id="snapshot", data=snapshot)

        def event_stream() -> Iterator[str]:
            yield "event: snapshot\n"
            yield f"data: {json.dumps(jsonable_encoder(response), sort_keys=True)}\n\n"
            yield "event: heartbeat\n"
            payload = {"status": "read_only", "snapshot_id": snapshot["snapshot_id"]}
            yield f"data: {json.dumps(payload)}\n\n"

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    @router.get("/api/dashboard/v1/export/snapshot.csv")
    def dashboard_snapshot_csv(
        service: Annotated[DecisionUiService, Depends(get_service)],
    ) -> Response:
        snapshot = build_dashboard_snapshot(service.session, settings=resolved_settings)
        return Response(
            content=export_snapshot_csv(snapshot),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=phase_3t_snapshot.csv"},
        )

    @router.get("/opportunities/best-payouts", response_class=HTMLResponse)
    def best_payouts_dashboard(
        request: Request,
        service: Annotated[DecisionUiService, Depends(get_service)],
        model_name: Annotated[str, Query()] = "ensemble_v2",
        limit: Annotated[int, Query()] = 20,
    ) -> HTMLResponse:
        rows = best_payout_rows(service.session, model_name=model_name, limit=limit)
        return templates.TemplateResponse(
            request,
            "best_payouts.html",
            {
                "request": request,
                "rows": rows,
                "model_name": model_name,
                "report_links": REPORT_LINKS,
            },
        )

    @router.get("/research", response_class=HTMLResponse)
    def research_dashboard_page(
        request: Request,
        service: Annotated[DecisionUiService, Depends(get_service)],
        model_name: Annotated[str, Query()] = "ensemble_v2",
    ) -> HTMLResponse:
        context = research_dashboard(service.session, model_name=model_name, limit=5)
        return templates.TemplateResponse(
            request,
            "research.html",
            {"request": request, **context, "report_links": REPORT_LINKS},
        )

    @router.get("/research/opportunity/{ticker}", response_class=HTMLResponse)
    def research_opportunity_page(
        ticker: str,
        request: Request,
        service: Annotated[DecisionUiService, Depends(get_service)],
        model_name: Annotated[str, Query()] = "ensemble_v2",
    ) -> HTMLResponse:
        context = research_opportunity(
            service.session,
            ticker=ticker,
            model_name=model_name,
            persist_note=False,
        )
        return templates.TemplateResponse(
            request,
            "research_opportunity.html",
            {"request": request, **context, "report_links": REPORT_LINKS},
        )

    @router.post("/research/ask")
    def research_ask(
        service: Annotated[DecisionUiService, Depends(get_service)],
        question: Annotated[str, Query()],
        ticker: Annotated[str | None, Query()] = None,
        model_name: Annotated[str, Query()] = "ensemble_v2",
    ) -> dict:
        result = answer_research_question(
            service.session,
            question=question,
            ticker=ticker,
            model_name=model_name,
        )
        store_research_question(service.session, result=result)
        service.session.commit()
        return jsonable_encoder({"status": "ANSWER", "message": result["answer"], **result})

    @router.get("/signals", response_class=HTMLResponse)
    def signals_dashboard(
        request: Request,
        service: Annotated[DecisionUiService, Depends(get_service)],
    ) -> HTMLResponse:
        context = signal_marketplace(service.session)
        return templates.TemplateResponse(
            request,
            "signals.html",
            {"request": request, **context, "report_links": REPORT_LINKS},
        )

    @router.get("/signals/health", response_class=HTMLResponse)
    def signals_health_page(
        request: Request,
        service: Annotated[DecisionUiService, Depends(get_service)],
    ) -> HTMLResponse:
        context = signal_health(service.session)
        return templates.TemplateResponse(
            request,
            "signals_health.html",
            {"request": request, **context, "report_links": REPORT_LINKS},
        )

    @router.get("/signals/{signal_name}", response_class=HTMLResponse)
    def signal_detail_page(
        signal_name: str,
        request: Request,
        service: Annotated[DecisionUiService, Depends(get_service)],
    ) -> HTMLResponse:
        context = signal_detail(service.session, signal_name=signal_name)
        if context is None:
            raise HTTPException(status_code=404, detail="Signal not found.")
        return templates.TemplateResponse(
            request,
            "signal_detail.html",
            {"request": request, **context, "report_links": REPORT_LINKS},
        )

    @router.get("/news", response_class=HTMLResponse)
    def news_dashboard_page(
        request: Request,
        service: Annotated[DecisionUiService, Depends(get_service)],
    ) -> HTMLResponse:
        context = news_dashboard_summary(service.session, limit=25)
        opportunities = news_opportunity_rows(service.session, model_name="news_v1", limit=10)
        return templates.TemplateResponse(
            request,
            "news.html",
            {
                "request": request,
                **context,
                "opportunities": opportunities,
                "report_links": REPORT_LINKS,
            },
        )

    @router.get("/news/{item_id}", response_class=HTMLResponse)
    def news_detail_page(
        item_id: int,
        request: Request,
        service: Annotated[DecisionUiService, Depends(get_service)],
    ) -> HTMLResponse:
        item = service.session.get(NewsItem, item_id)
        if item is None:
            raise HTTPException(status_code=404, detail="News item not found.")
        links = news_links_for_item(service.session, item_id)
        linked_tickers = {link.ticker for link in links}
        opportunities = [
            row
            for row in news_opportunity_rows(service.session, model_name="news_v1", limit=25)
            if row["ticker"] in linked_tickers
        ]
        return templates.TemplateResponse(
            request,
            "news_detail.html",
            {
                "request": request,
                "item": {
                    "id": item.id,
                    "source": item.source,
                    "source_url": item.source_url,
                    "published_at": item.published_at.isoformat() if item.published_at else None,
                    "ingested_at": item.ingested_at.isoformat(),
                    "title": item.title,
                    "summary": item.summary,
                    "body": item.body,
                    "author": item.author,
                    "category": item.category,
                    "entities": item_entities(item),
                    "sentiment_score": item.sentiment_score,
                    "importance_score": item.importance_score,
                    "freshness_score": item.freshness_score,
                    "raw_json": decode_json(item.raw_json),
                },
                "links": links,
                "opportunities": opportunities,
                "report_links": REPORT_LINKS,
            },
        )

    @router.get("/sports", response_class=HTMLResponse)
    def sports_dashboard_page(
        request: Request,
        service: Annotated[DecisionUiService, Depends(get_service)],
    ) -> HTMLResponse:
        context = sports_dashboard_summary(service.session, league="ALL", limit=25)
        opportunities = sports_opportunity_rows(
            service.session,
            model_name="sports_v1",
            league="ALL",
            limit=10,
        )
        return templates.TemplateResponse(
            request,
            "sports.html",
            {
                "request": request,
                **context,
                "opportunities": opportunities,
                "report_links": REPORT_LINKS,
            },
        )

    @router.get("/sports/leagues/{league}", response_class=HTMLResponse)
    def sports_league_page(
        league: str,
        request: Request,
        service: Annotated[DecisionUiService, Depends(get_service)],
    ) -> HTMLResponse:
        context = sports_dashboard_summary(service.session, league=league.upper(), limit=50)
        opportunities = sports_opportunity_rows(
            service.session,
            model_name="sports_v1",
            league=league.upper(),
            limit=20,
        )
        return templates.TemplateResponse(
            request,
            "sports_league.html",
            {
                "request": request,
                "league": league.upper(),
                **context,
                "opportunities": opportunities,
                "report_links": REPORT_LINKS,
            },
        )

    @router.get("/sports/games/{game_key}", response_class=HTMLResponse)
    def sports_game_page(
        game_key: str,
        request: Request,
        service: Annotated[DecisionUiService, Depends(get_service)],
    ) -> HTMLResponse:
        context = sports_game_detail(service.session, game_key)
        if context is None:
            raise HTTPException(status_code=404, detail="Sports game not found.")
        return templates.TemplateResponse(
            request,
            "sports_game.html",
            {"request": request, **context, "report_links": REPORT_LINKS},
        )

    @router.get("/microstructure", response_class=HTMLResponse)
    def microstructure_dashboard(
        request: Request,
        service: Annotated[DecisionUiService, Depends(get_service)],
    ) -> HTMLResponse:
        context = build_microstructure_dashboard(service.session)
        return templates.TemplateResponse(
            request,
            "microstructure.html",
            {"request": request, **context, "report_links": REPORT_LINKS},
        )

    @router.get("/microstructure/{ticker}", response_class=HTMLResponse)
    def microstructure_detail_page(
        ticker: str,
        request: Request,
        service: Annotated[DecisionUiService, Depends(get_service)],
    ) -> HTMLResponse:
        context = microstructure_detail(service.session, ticker)
        if context is None:
            raise HTTPException(status_code=404, detail="Microstructure data not found.")
        return templates.TemplateResponse(
            request,
            "microstructure_detail.html",
            {"request": request, **context, "report_links": REPORT_LINKS},
        )

    @router.get("/meta", response_class=HTMLResponse)
    def meta_dashboard(
        request: Request,
        service: Annotated[DecisionUiService, Depends(get_service)],
    ) -> HTMLResponse:
        context = build_meta_dashboard(service.session)
        return templates.TemplateResponse(
            request,
            "meta.html",
            {"request": request, **context, "report_links": REPORT_LINKS},
        )

    @router.get("/meta/{ticker}", response_class=HTMLResponse)
    def meta_detail_page(
        ticker: str,
        request: Request,
        service: Annotated[DecisionUiService, Depends(get_service)],
    ) -> HTMLResponse:
        context = meta_detail(service.session, ticker)
        if context is None:
            raise HTTPException(status_code=404, detail="Meta decision not found.")
        return templates.TemplateResponse(
            request,
            "meta_detail.html",
            {"request": request, **context, "report_links": REPORT_LINKS},
        )

    @router.get("/opportunities/{ticker}", response_class=HTMLResponse)
    def opportunity_detail(
        ticker: str,
        request: Request,
        service: Annotated[DecisionUiService, Depends(get_service)],
    ) -> HTMLResponse:
        detail = service.opportunity_detail(ticker)
        if detail is None:
            raise HTTPException(status_code=404, detail="Opportunity not found.")
        return templates.TemplateResponse(
            request,
            "opportunity_detail.html",
            {
                "request": request,
                "detail": detail,
                "report_links": REPORT_LINKS,
                "shell_context": shell_context_for(service),
            },
        )

    @router.get("/execution/review/{ticker}", response_class=HTMLResponse)
    def execution_review(
        ticker: str,
        request: Request,
        service: Annotated[DecisionUiService, Depends(get_service)],
    ) -> HTMLResponse:
        review = service.execution_review(ticker)
        if review is None:
            raise HTTPException(status_code=404, detail="Opportunity not found.")
        return templates.TemplateResponse(
            request,
            "execution_review.html",
            {"request": request, **review, "shell_context": shell_context_for(service)},
        )

    @router.get("/autopilot", response_class=HTMLResponse)
    def autopilot_dashboard(
        request: Request,
        service: Annotated[DecisionUiService, Depends(get_service)],
    ) -> HTMLResponse:
        context = build_autopilot_status(service.session, settings=resolved_settings)
        return templates.TemplateResponse(
            request,
            "autopilot.html",
            {"request": request, **context, "report_links": REPORT_LINKS},
        )

    @router.get("/learning", response_class=HTMLResponse)
    def learning_dashboard(
        request: Request,
        service: Annotated[DecisionUiService, Depends(get_service)],
    ) -> HTMLResponse:
        context = build_learning_dashboard(service.session, settings=resolved_settings)
        context["tonight_status"] = tonight_card(service.session, settings=resolved_settings)
        return templates.TemplateResponse(
            request,
            "learning.html",
            {"request": request, **context, "report_links": REPORT_LINKS},
        )

    @router.get("/portfolio", response_class=HTMLResponse)
    def portfolio_dashboard(
        request: Request,
        service: Annotated[DecisionUiService, Depends(get_service)],
    ) -> HTMLResponse:
        context = portfolio_context_for(service)
        return templates.TemplateResponse(
            request,
            "portfolio.html",
            {
                "request": request,
                "portfolio": context,
                "report_links": REPORT_LINKS,
                "shell_context": shell_context_for(service),
            },
        )

    @router.get("/positions/{ticker}", response_class=HTMLResponse)
    def position_dashboard(
        ticker: str,
        request: Request,
        service: Annotated[DecisionUiService, Depends(get_service)],
    ) -> HTMLResponse:
        context = position_detail(service.session, ticker)
        if context is None:
            raise HTTPException(status_code=404, detail="Position not found.")
        return templates.TemplateResponse(
            request,
            "position_detail_workstation.html",
            {"request": request, **context, "report_links": REPORT_LINKS},
        )

    @router.get("/models/confidence", response_class=HTMLResponse)
    def model_confidence_dashboard(
        request: Request,
        service: Annotated[DecisionUiService, Depends(get_service)],
    ) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "model_confidence.html",
            {
                "request": request,
                "rows": confidence_rows_for_ui(service.session),
                "report_links": REPORT_LINKS,
            },
        )

    @router.get("/models/readiness", response_class=HTMLResponse)
    def model_readiness_dashboard(
        request: Request,
        service: Annotated[DecisionUiService, Depends(get_service)],
    ) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "model_readiness.html",
            {
                "request": request,
                "models": model_status_rows(service.session),
                "report_links": REPORT_LINKS,
                "shell_context": shell_context_for(service),
            },
        )

    @router.get("/control-center", response_class=HTMLResponse)
    def control_center_dashboard(
        request: Request,
        service: Annotated[DecisionUiService, Depends(get_service)],
    ) -> HTMLResponse:
        context = build_control_center(service.session, settings=resolved_settings)
        return templates.TemplateResponse(
            request,
            "control_center.html",
            {
                "request": request,
                **context,
                "report_links": REPORT_LINKS,
                "shell_context": shell_context_for(service),
            },
        )

    @router.get("/models", response_class=HTMLResponse)
    def models_dashboard(
        request: Request,
        service: Annotated[DecisionUiService, Depends(get_service)],
    ) -> HTMLResponse:
        models = _model_rows_with_readiness(service.session)
        return templates.TemplateResponse(
            request,
            "models.html",
            {
                "request": request,
                "models": models,
                "report_links": REPORT_LINKS,
                "shell_context": shell_context_for(service),
            },
        )

    @router.get("/leaderboard", response_class=HTMLResponse)
    def leaderboard_dashboard(
        request: Request,
        service: Annotated[DecisionUiService, Depends(get_service)],
    ) -> HTMLResponse:
        models = _model_rows_with_readiness(service.session)
        return templates.TemplateResponse(
            request,
            "models.html",
            {
                "request": request,
                "models": models,
                "report_links": REPORT_LINKS,
                "shell_context": shell_context_for(service),
            },
        )

    @router.get("/settings/database", response_class=HTMLResponse)
    def database_settings_page(
        request: Request,
        service: Annotated[DecisionUiService, Depends(get_service)],
    ) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "database_settings.html",
            {
                "request": request,
                "database": database_status_card(
                    service.session,
                    settings=resolved_settings,
                ),
                "settings": resolved_settings,
                "report_links": REPORT_LINKS,
                "shell_context": shell_context_for(service),
            },
        )

    @router.get("/memory", response_class=HTMLResponse)
    def market_memory_page(
        request: Request,
        service: Annotated[DecisionUiService, Depends(get_service)],
    ) -> HTMLResponse:
        context = memory_health(service.session, settings=resolved_settings)
        return templates.TemplateResponse(
            request,
            "memory.html",
            {
                "request": request,
                "memory": context,
                "report_links": REPORT_LINKS,
            },
        )

    @router.get("/settings", response_class=HTMLResponse)
    def settings_dashboard(
        request: Request,
        service: Annotated[DecisionUiService, Depends(get_service)],
    ) -> HTMLResponse:
        learning_context = build_learning_dashboard(service.session, settings=resolved_settings)
        database = database_status_card(service.session, settings=resolved_settings)
        memory = memory_health(service.session, settings=resolved_settings)
        return templates.TemplateResponse(
            request,
            "settings.html",
            {
                "request": request,
                "safety": service.safety_state(),
                "settings": resolved_settings,
                "learning": learning_context["status"],
                "database": database,
                "memory": memory,
                "liquidity": paper_liquidity_plan(
                    service.session,
                    settings=resolved_settings,
                ),
                "advanced_risk": advanced_risk_card(
                    service.session,
                    settings=resolved_settings,
                ),
                "config": learning_context["config"],
                "report_links": REPORT_LINKS,
            },
        )

    @router.get("/markets", response_class=HTMLResponse)
    def markets_dashboard(
        request: Request,
        service: Annotated[DecisionUiService, Depends(get_service)],
        category: Annotated[str | None, Query()] = None,
        model: Annotated[str | None, Query()] = None,
        search: Annotated[str | None, Query()] = None,
        min_score: Annotated[float | None, Query()] = None,
        min_liquidity: Annotated[float | None, Query()] = None,
        min_confidence: Annotated[float | None, Query()] = None,
    ) -> HTMLResponse:
        from decimal import Decimal

        rows = market_monitor_rows(
            service.session,
            category=category,
            model=model,
            search=search,
            min_score=Decimal(str(min_score)) if min_score is not None else None,
            min_liquidity=Decimal(str(min_liquidity)) if min_liquidity is not None else None,
            min_confidence=Decimal(str(min_confidence)) if min_confidence is not None else None,
        )
        return templates.TemplateResponse(
            request,
            "markets.html",
            {
                "request": request,
                "markets": rows,
                "report_links": REPORT_LINKS,
                "shell_context": shell_context_for(service),
            },
        )

    @router.get("/links/coverage", response_class=HTMLResponse)
    def link_coverage_view(
        request: Request,
        service: Annotated[DecisionUiService, Depends(get_service)],
    ) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "link_coverage.html",
            {
                "request": request,
                "coverage": _link_coverage_for_ui(service.session),
                "report_links": REPORT_LINKS,
                "shell_context": shell_context_for(service),
            },
        )

    @router.get("/analytics", response_class=HTMLResponse)
    def analytics_dashboard(
        request: Request,
        service: Annotated[DecisionUiService, Depends(get_service)],
    ) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "analytics.html",
            {
                "request": request,
                "analytics": analytics_summary(service.session),
                "report_links": REPORT_LINKS,
            },
        )

    @router.get("/watchlists", response_class=HTMLResponse)
    def watchlists_dashboard(
        request: Request,
        service: Annotated[DecisionUiService, Depends(get_service)],
    ) -> HTMLResponse:
        watchlists = watchlists_summary(service.session, ensure_defaults=False)
        return templates.TemplateResponse(
            request,
            "watchlists.html",
            {
                "request": request,
                "watchlists": watchlists,
                "report_links": REPORT_LINKS,
            },
        )

    @router.post("/watchlists/add")
    def watchlist_add(
        service: Annotated[DecisionUiService, Depends(get_service)],
        watchlist_id: Annotated[int, Query()],
        ticker: Annotated[str, Query()],
    ) -> dict:
        item = add_market_to_watchlist(
            service.session,
            watchlist_id=watchlist_id,
            ticker=ticker,
        )
        service.session.commit()
        return jsonable_encoder({"status": "ADDED", "id": item.id, "ticker": item.ticker})

    @router.post("/watchlists/remove")
    def watchlist_remove(
        service: Annotated[DecisionUiService, Depends(get_service)],
        item_id: Annotated[int, Query()],
    ) -> dict:
        removed = remove_market_from_watchlist(service.session, item_id=item_id)
        service.session.commit()
        return jsonable_encoder({"status": "REMOVED", "removed": removed})

    @router.get("/alerts", response_class=HTMLResponse)
    def alerts_dashboard(
        request: Request,
        service: Annotated[DecisionUiService, Depends(get_service)],
    ) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "alerts.html",
            {
                "request": request,
                "alerts": alerts_summary(service.session, ensure_defaults=False),
                "report_links": REPORT_LINKS,
            },
        )

    @router.post("/reports/portfolio-summary")
    def write_portfolio_summary(
        service: Annotated[DecisionUiService, Depends(get_service)],
    ) -> dict:
        path = generate_portfolio_summary_report(service.session)
        service.session.commit()
        return jsonable_encoder({"status": "WROTE", "path": str(path)})

    @router.post("/reports/daily-briefing")
    def write_daily_briefing(
        service: Annotated[DecisionUiService, Depends(get_service)],
    ) -> dict:
        path = generate_daily_briefing(service.session, settings=resolved_settings)
        service.session.commit()
        return jsonable_encoder({"status": "WROTE", "path": str(path)})

    @router.post("/reports/analytics-report")
    def write_analytics_report(
        service: Annotated[DecisionUiService, Depends(get_service)],
    ) -> dict:
        path = generate_analytics_report(service.session)
        return jsonable_encoder({"status": "WROTE", "path": str(path)})

    @router.post("/reports/phase3x-report")
    def write_phase_3x_report(
        service: Annotated[DecisionUiService, Depends(get_service)],
    ) -> dict:
        result = generate_phase_3x_report(
            service.session,
            output_dir=resolved_settings.phase_3x_output_dir,
            settings=resolved_settings,
        )
        return jsonable_encoder(
            {
                "status": "WROTE",
                "ok": True,
                "decision": result["decision"],
                "path": str(result["output_dir"]),
                "live_trading_authorized": False,
            }
        )

    @router.post("/reports/best-payouts")
    def write_best_payouts_report(
        service: Annotated[DecisionUiService, Depends(get_service)],
        model_name: Annotated[str, Query()] = "ensemble_v2",
        limit: Annotated[int, Query()] = 20,
    ) -> dict:
        path = generate_best_payouts_report(
            service.session,
            model_name=model_name,
            limit=limit,
            output_path=Path("reports/best_payouts.md"),
        )
        return jsonable_encoder({"status": "WROTE", "path": str(path)})

    @router.post("/reports/research-report")
    def write_research_report(
        service: Annotated[DecisionUiService, Depends(get_service)],
        model_name: Annotated[str, Query()] = "ensemble_v2",
        limit: Annotated[int, Query()] = 10,
    ) -> dict:
        path = generate_research_report(
            service.session,
            model_name=model_name,
            limit=limit,
            output_path=Path("reports/research_report.md"),
        )
        service.session.commit()
        return jsonable_encoder({"status": "WROTE", "path": str(path)})

    @router.post("/reports/signal-report")
    def write_signal_report(
        service: Annotated[DecisionUiService, Depends(get_service)],
    ) -> dict:
        path = generate_signal_report(
            service.session,
            output_path=Path("reports/signal_report.md"),
        )
        service.session.commit()
        return jsonable_encoder({"status": "WROTE", "path": str(path)})

    @router.post("/reports/learning-report")
    def write_learning_report(
        service: Annotated[DecisionUiService, Depends(get_service)],
    ) -> dict:
        path = generate_learning_report(service.session, settings=resolved_settings)
        service.session.commit()
        return jsonable_encoder({"status": "WROTE", "path": str(path)})

    @router.post("/reports/learning-diagnostics")
    def write_learning_diagnostics_report(
        service: Annotated[DecisionUiService, Depends(get_service)],
    ) -> dict:
        path = generate_learning_diagnostics_report(
            service.session,
            settings=resolved_settings,
        )
        service.session.commit()
        return jsonable_encoder({"status": "WROTE", "path": str(path)})

    @router.post("/reports/self-evaluation")
    def write_self_evaluation_report(
        service: Annotated[DecisionUiService, Depends(get_service)],
    ) -> dict:
        result = generate_self_evaluation_report(service.session, settings=resolved_settings)
        service.session.commit()
        return jsonable_encoder(
            {
                "status": result.journal_status,
                "path": str(result.markdown_path),
                "journal_id": result.journal_id,
                "revision": result.journal_revision,
                "idempotent": result.idempotent,
            }
        )

    @router.post("/reports/learning-targets")
    def write_learning_targets_report(
        service: Annotated[DecisionUiService, Depends(get_service)],
        limit: Annotated[int, Query()] = 50,
    ) -> dict:
        path = generate_learning_targets_report(
            service.session,
            settings=resolved_settings,
            limit=limit,
        )
        service.session.commit()
        return jsonable_encoder({"status": "WROTE", "path": str(path)})

    @router.post("/reports/advanced-risk")
    def write_advanced_risk_report(
        service: Annotated[DecisionUiService, Depends(get_service)],
    ) -> dict:
        path = generate_advanced_risk_report(service.session, settings=resolved_settings)
        service.session.commit()
        return jsonable_encoder({"status": "WROTE", "path": str(path)})

    @router.post("/reports/institutional-dashboard")
    def write_institutional_dashboard_report(
        service: Annotated[DecisionUiService, Depends(get_service)],
    ) -> dict:
        path = generate_institutional_dashboard_report(
            service.session,
            settings=resolved_settings,
        )
        return jsonable_encoder({"status": "WROTE", "path": str(path)})

    @router.post("/reports/personal-trader")
    def write_personal_trader_report(
        service: Annotated[DecisionUiService, Depends(get_service)],
    ) -> dict:
        path = generate_personal_trader_report(
            service.session,
            settings=resolved_settings,
            persist=False,
        )
        return jsonable_encoder({"status": "WROTE", "path": str(path)})

    @router.post("/reports/model-confidence")
    def write_model_confidence_report(
        service: Annotated[DecisionUiService, Depends(get_service)],
        days: Annotated[int, Query()] = 30,
    ) -> dict:
        path = generate_model_confidence_report(
            service.session,
            settings=resolved_settings,
            days=days,
        )
        service.session.commit()
        return jsonable_encoder({"status": "WROTE", "path": str(path)})

    @router.post("/reports/model-readiness")
    def write_model_readiness_report(
        service: Annotated[DecisionUiService, Depends(get_service)],
    ) -> dict:
        path = generate_model_readiness_report(
            service.session,
            output_path=Path("reports/model_readiness.md"),
        )
        return jsonable_encoder({"status": "WROTE", "path": str(path)})

    @router.post("/reports/market-memory")
    def write_market_memory_report(
        service: Annotated[DecisionUiService, Depends(get_service)],
    ) -> dict:
        path = generate_memory_report(service.session, settings=resolved_settings)
        service.session.commit()
        return jsonable_encoder({"status": "WROTE", "path": str(path)})

    @router.post("/reports/control-center")
    def write_control_center_report(
        service: Annotated[DecisionUiService, Depends(get_service)],
    ) -> dict:
        path = generate_control_center_report(service.session, settings=resolved_settings)
        return jsonable_encoder({"status": "WROTE", "path": str(path)})

    @router.post("/reports/news-report")
    def write_news_report(
        service: Annotated[DecisionUiService, Depends(get_service)],
    ) -> dict:
        path = generate_news_report(
            service.session,
            output_path=Path("reports/news_report.md"),
            settings=resolved_settings,
        )
        service.session.commit()
        return jsonable_encoder({"status": "WROTE", "path": str(path)})

    @router.post("/reports/sports-report")
    def write_sports_report(
        service: Annotated[DecisionUiService, Depends(get_service)],
        league: Annotated[str, Query()] = "ALL",
    ) -> dict:
        path = generate_sports_report(
            service.session,
            league=league,
            output_path=Path("reports/sports_report.md"),
            settings=resolved_settings,
        )
        service.session.commit()
        return jsonable_encoder({"status": "WROTE", "path": str(path)})

    @router.post("/reports/microstructure-report")
    def write_microstructure_report(
        service: Annotated[DecisionUiService, Depends(get_service)],
    ) -> dict:
        path = generate_microstructure_report(service.session)
        return jsonable_encoder({"status": "WROTE", "path": str(path)})

    @router.post("/reports/microstructure-opportunities")
    def write_microstructure_opportunities_report(
        service: Annotated[DecisionUiService, Depends(get_service)],
        model_name: Annotated[str, Query()] = "microstructure_v1",
        limit: Annotated[int, Query()] = 20,
    ) -> dict:
        path = generate_microstructure_opportunities_report(
            service.session,
            model_name=model_name,
            limit=limit,
            settings=resolved_settings,
        )
        return jsonable_encoder({"status": "WROTE", "path": str(path)})

    @router.post("/reports/microstructure-backtest")
    def write_microstructure_backtest_report(
        service: Annotated[DecisionUiService, Depends(get_service)],
        days: Annotated[int, Query()] = 30,
    ) -> dict:
        path = generate_microstructure_backtest_report(service.session, days=days)
        service.session.commit()
        return jsonable_encoder({"status": "WROTE", "path": str(path)})

    @router.post("/reports/meta-report")
    def write_meta_report(
        service: Annotated[DecisionUiService, Depends(get_service)],
    ) -> dict:
        path = generate_meta_report(service.session)
        return jsonable_encoder({"status": "WROTE", "path": str(path)})

    @router.post("/reports/meta-evaluation")
    def write_meta_evaluation_report(
        service: Annotated[DecisionUiService, Depends(get_service)],
        days: Annotated[int, Query()] = 90,
    ) -> dict:
        path = generate_meta_evaluation_report(service.session, days=days)
        service.session.commit()
        return jsonable_encoder({"status": "WROTE", "path": str(path)})

    @router.post("/reports/meta-opportunities")
    def write_meta_opportunities_report(
        service: Annotated[DecisionUiService, Depends(get_service)],
        limit: Annotated[int, Query()] = 20,
    ) -> dict:
        path = generate_meta_opportunities_report(service.session, limit=limit)
        return jsonable_encoder({"status": "WROTE", "path": str(path)})

    @router.get("/overnight", response_class=HTMLResponse)
    def overnight_dashboard(
        request: Request,
        service: Annotated[DecisionUiService, Depends(get_service)],
    ) -> HTMLResponse:
        context = build_overnight_status(service.session, settings=resolved_settings)
        return templates.TemplateResponse(
            request,
            "overnight.html",
            {"request": request, **context, "report_links": REPORT_LINKS},
        )

    @router.post("/overnight/run-once")
    def overnight_run_once(
        service: Annotated[DecisionUiService, Depends(get_service)],
    ) -> dict:
        safe_settings = resolved_settings.model_copy(
            update={"overnight_run_demo": False, "overnight_run_paper": True}
        )
        result = run_overnight_once(service.session, settings=safe_settings)
        generate_overnight_report(service.session, settings=safe_settings)
        service.session.commit()
        return jsonable_encoder(
            {
                "status": result.status,
                "message": "Paper-only overnight cycle recorded.",
                "run_id": result.run_id,
                "cycle_id": result.cycle_id,
                "paper_orders_created": result.paper_orders_created,
                "errors": len(result.errors),
            }
        )

    @router.post("/learning/run-once")
    def learning_run_once(
        service: Annotated[DecisionUiService, Depends(get_service)],
    ) -> JSONResponse:
        cap_status = learning_daily_cap_status(service.session, settings=resolved_settings)
        if cap_status["reached"]:
            return JSONResponse(
                content={
                    "ok": False,
                    "message": cap_status["message"],
                    "next_action": cap_status["next_action"],
                }
            )
        try:
            result = run_learning_once(service.session, settings=resolved_settings)
            generate_learning_report(service.session, settings=resolved_settings)
            service.session.commit()
        except Exception as exc:
            service.session.rollback()
            logger.exception("Learning cycle UI action failed.")
            return JSONResponse(
                status_code=500,
                content={
                    "ok": False,
                    "message": "Learning cycle failed.",
                    "error": str(exc) or type(exc).__name__,
                    "next_action": ("Check logs or run kalshi-bot learning-once from terminal."),
                },
            )

        ok = not result.errors
        message = (
            "Learning cycle completed."
            if ok
            else "Learning cycle completed with paper-only step errors."
        )
        return JSONResponse(
            content=jsonable_encoder(
                {
                    "ok": ok,
                    "status": result.status,
                    "message": message,
                    "run_id": result.run_id,
                    "cycle_id": result.cycle_id,
                    "summary": {
                        "paper_trades_created": result.paper_trades_created,
                        "forecasts_evaluated": result.forecasts_generated,
                        "opportunities_found": result.opportunities_found,
                        "markets_scanned": result.markets_scanned,
                        "settlements_synced": result.settlements_synced,
                        "settled_paper_trades_total": result.settled_paper_trades_total,
                    },
                    "errors": result.errors,
                }
            )
        )

    @router.post("/autopilot/run-once")
    def autopilot_run_once(
        service: Annotated[DecisionUiService, Depends(get_service)],
    ) -> dict:
        dry_run_settings = resolved_settings.model_copy(update={"autopilot_dry_run": True})
        result = run_autopilot_once(service.session, settings=dry_run_settings)
        generate_autopilot_report(service.session, settings=dry_run_settings)
        service.session.commit()
        return jsonable_encoder(
            {
                "status": result.status,
                "message": result.stop_reason or "Dry-run autopilot cycle recorded.",
                "run_id": result.run_id,
                "cycle_id": result.cycle_id,
                "orders_attempted": result.orders_attempted,
                "orders_submitted": result.orders_submitted,
                "orders_blocked": result.orders_blocked,
            }
        )

    @router.post("/paper-trade/{ticker}")
    def paper_trade(
        ticker: str,
        service: Annotated[DecisionUiService, Depends(get_service)],
    ) -> dict:
        result = service.paper_trade(ticker)
        service.session.commit()
        return jsonable_encoder(result)

    @router.post("/demo-dry-run/{ticker}")
    def demo_dry_run(
        ticker: str,
        service: Annotated[DecisionUiService, Depends(get_service)],
    ) -> dict:
        result = service.demo_dry_run(ticker)
        return jsonable_encoder(result)

    @router.post("/demo-execute/{ticker}")
    def demo_execute(
        ticker: str,
        service: Annotated[DecisionUiService, Depends(get_service)],
        confirmation: Annotated[str | None, Query()] = None,
    ) -> dict:
        result = service.demo_execute(ticker, confirmation=confirmation)
        return jsonable_encoder(result)

    @router.get("/reports/{report_name}")
    def report_file(report_name: str) -> FileResponse:
        allowed = {
            "opportunities.md",
            "model_leaderboard.md",
            "model_tournament.md",
            "paper_trading.md",
            "execution_report.md",
            "autopilot_report.md",
            "overnight_report.md",
            "portfolio_summary.md",
            "daily_briefing.md",
            "analytics_report.md",
            "best_payouts.md",
            "research_report.md",
            "signal_report.md",
            "news_report.md",
            "news_opportunities.md",
            "news_backtest.md",
            "sports_report.md",
            "sports_opportunities.md",
            "sports_backtest.md",
            "learning_report.md",
            "learning_diagnostics.md",
            "learning_targets.md",
            "self_evaluation_journal.md",
            "self_evaluation_journal.json",
            "database_report.md",
            "system_readiness_remediation.md",
            "market_memory_report.md",
            "advanced_risk_report.md",
            "institutional_dashboard.md",
            "personal_trader_brief.md",
            "model_readiness.md",
            "model_confidence.md",
            "control_center.md",
            "microstructure_report.md",
            "microstructure_opportunities.md",
            "microstructure_backtest.md",
            "meta_report.md",
            "meta_evaluation.md",
            "meta_opportunities.md",
        }
        if report_name not in allowed:
            raise HTTPException(status_code=404, detail="Unknown report.")
        path = Path("reports") / report_name
        if not path.exists():
            raise HTTPException(status_code=404, detail="Report has not been generated.")
        return FileResponse(path)

    return router


def _dashboard_payload(payload: dict[str, Any] | None) -> dict[str, Any]:
    if payload is None:
        return {}
    schema_version = payload.get("schema_version")
    if schema_version not in (None, API_SCHEMA_VERSION):
        raise HTTPException(
            status_code=400,
            detail={
                "code": "DASHBOARD_SCHEMA_UNSUPPORTED",
                "message": f"Unsupported dashboard schema version: {schema_version}",
            },
        )
    return payload


def _personal_trader_payload(payload: dict[str, Any] | None) -> dict[str, Any]:
    if payload is None:
        return {}
    schema_version = payload.get("schema_version")
    if schema_version not in (None, PHASE_3U_API_SCHEMA):
        raise HTTPException(
            status_code=400,
            detail={
                "code": "PERSONAL_TRADER_SCHEMA_UNSUPPORTED",
                "message": f"Unsupported personal trader schema version: {schema_version}",
            },
        )
    return payload


def _phase_3u_not_found(code: str, message: str) -> HTTPException:
    return HTTPException(status_code=404, detail={"code": code, "message": message})


def build_personal_trade_brief_time() -> str:
    return utc_now().isoformat()


def _phase_3u_freshness(brief: dict[str, Any]) -> str:
    statuses = {row["status"] for row in brief.get("source_health", [])}
    if "STALE" in statuses:
        return "STALE"
    if "UNAVAILABLE" in statuses:
        return "UNAVAILABLE"
    if "AGING" in statuses:
        return "AGING"
    return "FRESH"


def _model_rows_with_readiness(session: Session) -> list[dict[str, Any]]:
    performance_rows = {row["model_name"]: row for row in model_performance_rows(session)}
    statuses = model_status_rows(session)
    merged = []
    for status in statuses:
        performance = performance_rows.pop(status["model_name"], {})
        merged.append(_model_row_for_ui({**_empty_model_performance(), **performance, **status}))
    for performance in performance_rows.values():
        merged.append(_model_row_for_ui({**_empty_model_performance(), **performance}))
    return merged


def _model_row_for_ui(row: dict[str, Any]) -> dict[str, Any]:
    latest = row.get("latest_forecast_time") or row.get("latest_forecast_at")
    row["latest_forecast_display"] = _compact_datetime_display(latest)
    return row


def _compact_datetime_display(value: Any) -> str:
    parsed = _parse_link_coverage_generated_at(value)
    if parsed is None:
        return "none"
    age_seconds = int((utc_now() - parsed).total_seconds())
    if 0 <= age_seconds < 7 * 24 * 60 * 60:
        return f"{_compact_age(age_seconds)} ago"
    return parsed.astimezone(UTC).strftime("%b %d %H:%M")


def _empty_model_performance() -> dict[str, Any]:
    return {
        "trade_count": 0,
        "roi": None,
        "win_rate": None,
        "brier_score": None,
        "log_loss": None,
        "max_drawdown": None,
        "rank_color": "gray",
        "notes": "No leaderboard row yet.",
    }
