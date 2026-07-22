import json
import logging
import os
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from time import monotonic
from typing import Any

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from kalshi_predictor.advanced_risk.reports import advanced_risk_card
from kalshi_predictor.autopilot.reports import build_autopilot_status
from kalshi_predictor.confidence.repository import confidence_rows_for_ui
from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.consensus.repository import latest_consensus_for_ticker
from kalshi_predictor.control_center.reports import build_control_center
from kalshi_predictor.data.maintenance import database_status_card
from kalshi_predictor.data.repositories import decode_json
from kalshi_predictor.data.schema import (
    AdvancedRiskDecisionLog,
    AlertEvent,
    BacktestRun,
    BacktestTrade,
    FeatureSnapshot,
    Forecast,
    Market,
    MarketLeg,
    MarketOpportunity,
    MarketRanking,
    MarketSnapshot,
    PaperFill,
    PaperOrder,
    PaperPnl,
    PaperPosition,
    PositionSizingDecisionLog,
    Settlement,
)
from kalshi_predictor.explain.opportunity_explainer import explain_opportunity
from kalshi_predictor.forecasting.status import model_status_summary
from kalshi_predictor.learning.config import learning_paper_settings
from kalshi_predictor.learning.safety import (
    learning_blocks_demo_execution,
    learning_status,
    settled_paper_trade_count,
)
from kalshi_predictor.live_readiness.reports import live_readiness_dashboard_card
from kalshi_predictor.memory.reports import memory_health
from kalshi_predictor.meta.explanations import explain_meta_selection
from kalshi_predictor.microstructure.repository import latest_microstructure_feature
from kalshi_predictor.opportunities.market_identity import (
    is_tradeable_identity,
    market_identity_fields,
    verify_market_identity,
)
from kalshi_predictor.opportunities.payout_scoring import payout_metrics_from_ranking
from kalshi_predictor.opportunities.reports import best_payout_rows
from kalshi_predictor.opportunities.repository import get_recent_rankings
from kalshi_predictor.overnight.reports import build_overnight_status
from kalshi_predictor.paper.ledger import (
    create_paper_order,
    get_position,
    insert_paper_fill,
    mark_order_filled,
    update_position_for_fill,
)
from kalshi_predictor.paper.models import ORDER_OPEN, PaperDecision
from kalshi_predictor.phase_gh4 import build_gh3_soak_status
from kalshi_predictor.professional_ux.service import (
    DEFAULT_SHELL_STATUS_SNAPSHOT_PATH,
    load_shell_status_context,
)
from kalshi_predictor.research.assistant import research_dashboard
from kalshi_predictor.signals.attribution import signal_badges_for_opportunity
from kalshi_predictor.system_certification.reports import system_certification_card
from kalshi_predictor.tonight.control import tonight_card
from kalshi_predictor.ui.decision_clarity import build_decision_clarity
from kalshi_predictor.ui.market_display import (
    category_badge,
    classify_market_category,
    format_time_remaining,
    is_fresh_timestamp,
    recommendation_label,
    risk_meter,
    summarize_market_title,
    traffic_light_label,
)
from kalshi_predictor.ui.schemas import (
    ActionResult,
    DetailView,
    OpportunityView,
    ReportLinks,
    RiskCheck,
)
from kalshi_predictor.utils.decimals import decimal_to_str, to_decimal
from kalshi_predictor.utils.time import parse_datetime, utc_now
from kalshi_predictor.workstation.repository import (
    alerts_summary,
    analytics_summary,
    market_monitor_rows,
    model_performance_rows,
    portfolio_summary,
    position_rows,
)

logger = logging.getLogger(__name__)

REPORT_LINKS = ReportLinks(
    opportunities="/reports/opportunities.md",
    leaderboard="/reports/model_leaderboard.md",
    tournament="/reports/model_tournament.md",
    paper="/reports/paper_trading.md",
    execution="/reports/execution_report.md",
    overnight="/reports/overnight_report.md",
    portfolio="/reports/portfolio_summary.md",
    daily_briefing="/reports/daily_briefing.md",
    analytics="/reports/analytics_report.md",
    best_payouts="/reports/best_payouts.md",
    sports_report="/reports/sports_report.md",
    sports_opportunities="/reports/sports_opportunities.md",
    sports_backtest="/reports/sports_backtest.md",
    learning_report="/reports/learning_report.md",
    learning_diagnostics="/reports/learning_diagnostics.md",
    learning_targets="/reports/learning_targets.md",
    self_evaluation_journal="/reports/self_evaluation_journal.md",
    tonight_report="/reports/tonight_report.md",
    database_report="/reports/database_report.md",
    system_remediation="/reports/system_readiness_remediation.md",
    market_memory_report="/reports/market_memory_report.md",
    advanced_risk_report="/reports/advanced_risk_report.md",
    live_readiness_report="/reports/live_readiness_report.md",
    system_certification_report="/reports/system_certification/system_certification_report.md",
    model_readiness="/reports/model_readiness.md",
    model_confidence="/reports/model_confidence.md",
    control_center="/reports/control_center.md",
    microstructure_report="/reports/microstructure_report.md",
    microstructure_opportunities="/reports/microstructure_opportunities.md",
    microstructure_backtest="/reports/microstructure_backtest.md",
    meta_report="/reports/meta_report.md",
    meta_evaluation="/reports/meta_evaluation.md",
    meta_opportunities="/reports/meta_opportunities.md",
    link_coverage="/reports/link_coverage_report.md",
    model_repair_audit="/reports/model_repair/model_repair_audit.md",
    market_coverage_doctor="/reports/market_coverage/market_coverage_doctor.md",
    metrics_reconciliation="/reports/model_repair/metrics_reconciliation.md",
)

CRYPTO_FRESHNESS_REPORT_PATH = Path("reports/phase3bc_r5/phase3bc_r5_crypto_freshness_watch.json")
CRYPTO_FRESHNESS_STATUS_PATH = Path("reports/phase3bc_r5/phase3bc_r5_status.json")
PHASE3AK_CRYPTO_WATCH_STATUS_PATH = Path("reports/phase_3ak/crypto_watch_status.json")
PHASE3AN_DASHBOARD_STATUS_PATH = Path("reports/phase3an/phase3an_dashboard_status.json")
PHASE3AP_PAPER_READY_GATE_PATH = Path("reports/phase3ap/paper_ready_gate.json")
PHASE3AP_BOOK_DIAGNOSTIC_PATH = Path("reports/phase3ap/book_diagnostic.json")
PHASE3AQ_PAPER_READY_GATE_PATH = Path("reports/phase3aq/paper_ready_gate_summary.json")
PHASE3AQ_LINK_AUDIT_PATH = Path("reports/phase3aq/positive_ev_link_audit.json")
PHASE3AR_PAPER_READY_GATE_PATH = Path("reports/phase3ar/paper_ready_gate_after_url_repair.json")
PHASE3AR_URL_AUDIT_PATH = Path("reports/phase3ar/url_audit.json")
PHASE3AR_CATALOG_REFRESH_PATH = Path("reports/phase3ar/catalog_refresh_plan.json")
PHASE3BC_R3_REFRESH_PATH = Path("reports/phase3bc_r3/phase3bc_r3_active_crypto_refresh.json")
PHASE3AW_DASHBOARD_TRUTH_PATH = Path("reports/phase3aw/dashboard_truth.json")
PHASE3AY_FREE_SOURCE_SPRINT_PATH = Path("reports/phase3ay/free_source_sprint_report.json")
GH2_SOAK_REPORT_PATH = Path(
    os.getenv(
        "KALSHI_GH2_SOAK_REPORT_PATH",
        "/var/lib/kalshi-bot-gh2/reports/gh2_active_candidate_refresh.json",
    )
)
GH2_SOAK_HISTORY_PATH = Path(
    os.getenv(
        "KALSHI_GH2_SOAK_HISTORY_PATH",
        "/var/lib/kalshi-bot-gh2/reports/gh2_paper_only_soak_history.jsonl",
    )
)
GH1_WATCH_STATUS_PATH = Path(
    os.getenv(
        "KALSHI_GH1_WATCH_STATUS_PATH",
        "/var/lib/kalshi-bot-gh1/watch/status.json",
    )
)
GH2_SCHEDULER_STATUS_PATH = Path(
    os.getenv(
        "KALSHI_GH2_SCHEDULER_STATUS_PATH",
        "/var/lib/kalshi-bot-gh2/reports/gh2_scheduler_status.json",
    )
)
CRYPTO_FRESHNESS_REPORT_HREF = "/reports/phase3bc_r5/phase3bc_r5_crypto_freshness_watch.md"
PHASE3AP_EXECUTIVE_SUMMARY_HREF = "/reports/phase3ap/EXECUTIVE_SUMMARY.md"
PHASE3AP_GATE_HREF = "/reports/phase3ap/paper_ready_gate.json"
PHASE3AQ_EXECUTIVE_SUMMARY_HREF = "/reports/phase3aq/EXECUTIVE_SUMMARY.md"
PHASE3AQ_GATE_HREF = "/reports/phase3aq/paper_ready_gate_summary.json"
PHASE3AR_EXECUTIVE_SUMMARY_HREF = "/reports/phase3ar/EXECUTIVE_SUMMARY.md"
PHASE3AR_GATE_HREF = "/reports/phase3ar/paper_ready_gate_after_url_repair.json"
PHASE3AW_EXECUTIVE_SUMMARY_HREF = "/reports/phase3aw/EXECUTIVE_SUMMARY.md"
CRYPTO_WATCH_COMMAND = (
    "kalshi-bot phase3bc-r5-unattended-start --refresh-open-markets "
    "--near-money-only --market-limit 150 --market-max-pages 1 "
    "--near-money-per-symbol-limit 40 --near-money-window-limit 20 "
    "--snapshot-fetch-concurrency 2 "
    "--crypto-series-tickers KXBTC,KXETH,KXSOLE,KXXRP,KXDOGE "
    "--crypto-market-scan-limit 2500 --crypto-link-limit 500 "
    "--forecast-limit 1000 --opportunity-limit 500 --phase3bc-limit 1000 "
    "--cycles 32 --interval-minutes 15 --duration-hours 8 --timeout-grace-seconds 900"
)
SHELL_CONTEXT_CACHE_SECONDS = 60.0
OPPORTUNITIES_PAGE_LIMIT = 10
OPPORTUNITY_SCAN_MULTIPLIER = 3
OPPORTUNITY_SCAN_MINIMUM = 40
OPPORTUNITY_BLOCKED_LIMIT = 5

_SHELL_CONTEXT_CACHE: dict[str, Any] = {
    "key": None,
    "expires_at": 0.0,
    "context": None,
}


class DecisionUiService:
    def __init__(self, session: Session, settings: Settings | None = None) -> None:
        self.session = session
        self.settings = settings or get_settings()

    def dashboard(self, *, limit: int = 20) -> dict[str, Any]:
        ranking_limit = max(limit * 12, 120)
        opportunities, blocked_opportunities = self._ranked_opportunity_buckets(
            ranking_limit=ranking_limit,
            direct_limit=limit,
            blocked_limit=min(limit, 10),
        )
        portfolio = portfolio_summary(self.session)
        model_rows = model_performance_rows(self.session)
        overnight_status = build_overnight_status(self.session, settings=self.settings)
        autopilot_status = build_autopilot_status(self.session, settings=self.settings)
        learning = learning_status(self.session, settings=self.settings)
        tonight = tonight_card(self.session, settings=self.settings)
        database_status = database_status_card(self.session, settings=self.settings)
        market_memory_status = memory_health(self.session, settings=self.settings)
        advanced_risk = advanced_risk_card(self.session, settings=self.settings)
        live_readiness = live_readiness_dashboard_card(self.session, settings=self.settings)
        system_certification = system_certification_card(self.session, settings=self.settings)
        model_status = model_status_summary(self.session)
        analytics = analytics_summary(self.session)
        control_center = build_control_center(self.session, settings=self.settings)
        crypto_freshness = crypto_freshness_watch_status()
        free_source_hunt = free_source_hunt_status()
        return {
            "opportunities": opportunities,
            "blocked_opportunities": blocked_opportunities,
            "report_links": REPORT_LINKS,
            "report_cards": _report_cards(),
            "overnight_status": overnight_status,
            "autopilot_status": autopilot_status,
            "learning_status": learning,
            "tonight_status": tonight,
            "database_status": database_status,
            "market_memory_status": market_memory_status,
            "advanced_risk_status": advanced_risk,
            "live_readiness_status": live_readiness,
            "system_certification_status": system_certification,
            "inactive_models": model_status.inactive_models,
            "control_center": control_center,
            "portfolio": portfolio,
            "market_monitor": market_monitor_rows(self.session, limit=8),
            "model_rows": model_rows,
            "alert_status": alerts_summary(self.session, limit=8),
            "positions": position_rows(self.session, limit=8),
            "best_payouts": best_payout_rows(
                self.session,
                model_name="ensemble_v2",
                limit=5,
            ),
            "research_summary": research_dashboard(
                self.session,
                model_name="ensemble_v2",
                limit=5,
            ),
            "model_confidence_rows": confidence_rows_for_ui(self.session, limit=8),
            "dashboard_charts": _dashboard_charts(analytics),
            "executive_summary": _executive_summary(
                self.session,
                opportunities=opportunities,
                portfolio=portfolio,
                model_rows=model_rows,
                autopilot_status=autopilot_status,
            ),
            "crypto_freshness": crypto_freshness,
            "free_source_hunt": free_source_hunt,
            "paper_trade_blockers": paper_trade_blocker_status(
                crypto_freshness=crypto_freshness
            ),
            "safety": self.safety_state(),
            "summary": _dashboard_summary(
                opportunities,
                blocked_opportunities=blocked_opportunities,
            ),
            "opportunity_links": _opportunity_links_health_summary(
                self.session,
                settings=self.settings,
            ),
            "shell_context": cached_shell_context(self.session, settings=self.settings),
        }

    def opportunities_dashboard(self, *, limit: int = OPPORTUNITIES_PAGE_LIMIT) -> dict[str, Any]:
        page_generated_at = utc_now()
        ranking_limit = max(limit * OPPORTUNITY_SCAN_MULTIPLIER, OPPORTUNITY_SCAN_MINIMUM)
        opportunities, blocked_opportunities = self._fast_ranked_opportunity_rows(
            ranking_limit=ranking_limit,
            direct_limit=limit,
            blocked_limit=min(OPPORTUNITY_BLOCKED_LIMIT, limit),
        )
        return {
            "fast_bounded": True,
            "opportunities": opportunities,
            "blocked_opportunities": blocked_opportunities,
            "page_generated_at": page_generated_at.isoformat(),
            "page_generated_label": _format_timestamp_age_label(page_generated_at),
            "report_links": REPORT_LINKS,
            "executive_summary": _fast_executive_summary(
                self.session,
                opportunities=opportunities,
            ),
            "crypto_freshness": crypto_freshness_watch_status(),
            "safety": self.safety_state(),
            "summary": _fast_dashboard_summary(
                opportunities=opportunities,
                blocked_opportunities=blocked_opportunities,
            ),
            "opportunity_links": _opportunity_links_health_summary(
                self.session,
                settings=self.settings,
            ),
            "shell_context": cached_shell_context(self.session, settings=self.settings),
        }

    def _ranked_opportunity_buckets(
        self,
        *,
        ranking_limit: int,
        direct_limit: int,
        blocked_limit: int,
    ) -> tuple[list[OpportunityView], list[OpportunityView]]:
        opportunities: list[OpportunityView] = []
        blocked_opportunities: list[OpportunityView] = []
        minimum_scan = direct_limit + blocked_limit
        for index, row in enumerate(_top_rankings(self.session, ranking_limit), start=1):
            item = self._opportunity_from_ranking(row, include_signal_badges=False)
            if _is_direct_review_opportunity(item):
                if len(opportunities) < direct_limit:
                    opportunities.append(item)
            elif len(blocked_opportunities) < blocked_limit:
                blocked_opportunities.append(item)
            if len(opportunities) >= direct_limit and (
                len(blocked_opportunities) >= blocked_limit or index >= minimum_scan
            ):
                break
            if index >= minimum_scan and len(blocked_opportunities) >= blocked_limit:
                break
        def sort_key(item: OpportunityView) -> float:
            return float(item.decision_clarity.get("rank_sort") or 0)

        opportunities = sorted(opportunities, key=sort_key, reverse=True)[:direct_limit]
        blocked_opportunities = sorted(
            blocked_opportunities,
            key=sort_key,
            reverse=True,
        )[:blocked_limit]
        return opportunities, blocked_opportunities

    def _fast_ranked_opportunity_rows(
        self,
        *,
        ranking_limit: int,
        direct_limit: int,
        blocked_limit: int,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        opportunities: list[dict[str, Any]] = []
        blocked_opportunities: list[dict[str, Any]] = []
        for ranking in _top_rankings(self.session, ranking_limit):
            item = _fast_opportunity_row(self.session, ranking, settings=self.settings)
            if item["is_blocked_research"]:
                if len(blocked_opportunities) < blocked_limit:
                    blocked_opportunities.append(item)
            elif len(opportunities) < direct_limit:
                opportunities.append(item)
            if len(opportunities) >= direct_limit and len(blocked_opportunities) >= blocked_limit:
                break
        return opportunities, blocked_opportunities

    def today(self, *, limit: int = 12) -> dict[str, Any]:
        started_at = monotonic()
        logger.debug("today service fast rankings start")
        opportunities, blocked_opportunities = self._fast_ranked_opportunity_rows(
            ranking_limit=max(limit * 4, 48),
            direct_limit=limit,
            blocked_limit=min(limit, 5),
        )
        logger.debug("today service rankings ready in %.2fs", monotonic() - started_at)
        portfolio = _today_portfolio_summary_fast(self.session)
        logger.debug("today service portfolio ready in %.2fs", monotonic() - started_at)
        learning = _today_learning_status_fast(self.session, settings=self.settings)
        logger.debug("today service learning ready in %.2fs", monotonic() - started_at)
        today_workspace = _fast_today_workspace(
            self.session,
            opportunities=opportunities,
            portfolio=portfolio,
        )
        logger.debug("today service workspace ready in %.2fs", monotonic() - started_at)
        alert_status = _today_alert_status_fast(self.session, limit=5)
        logger.debug("today service alerts ready in %.2fs", monotonic() - started_at)
        shell_context = cached_shell_context(self.session, settings=self.settings)
        logger.debug("today service shell ready in %.2fs", monotonic() - started_at)
        crypto_freshness = crypto_freshness_watch_status()
        gh3_soak = paper_only_soak_status()
        return {
            "opportunities": opportunities,
            "blocked_opportunities": blocked_opportunities,
            "portfolio": portfolio,
            "alert_status": alert_status,
            "learning_status": learning,
            "crypto_freshness": crypto_freshness,
            "gh3_soak": gh3_soak,
            "paper_trade_blockers": paper_trade_blocker_status(
                crypto_freshness=crypto_freshness
            ),
            "today_workspace": today_workspace,
            "opportunity_links": _opportunity_links_health_summary(
                self.session,
                settings=self.settings,
            ),
            "report_links": REPORT_LINKS,
            "shell_context": shell_context,
        }

    def opportunity_detail(self, ticker: str) -> DetailView | None:
        market = self.session.get(Market, ticker)
        ranking = _latest_ranking_for_ticker(self.session, ticker)
        if market is None and ranking is None:
            return None
        latest_forecast = _latest_forecast(
            self.session,
            ticker,
            ranking.forecast_model if ranking else None,
        )
        raw_feature_json = decode_json(latest_forecast.feature_json if latest_forecast else None)
        opportunity = self._opportunity_from_ranking(ranking) if ranking is not None else None
        return DetailView(
            ticker=ticker,
            title=str((market.title if market else None) or (ranking.title if ranking else ticker)),
            rules=_rules_text(market),
            opportunity=opportunity,
            orderbook_summary=_orderbook_summary(_latest_snapshot(self.session, ticker)),
            forecast_history=_forecast_history(self.session, ticker),
            component_probabilities=_component_probabilities(raw_feature_json),
            feature_json=json.dumps(raw_feature_json, indent=2, sort_keys=True),
            score_breakdown=_score_breakdown(ranking),
            paper_pnl=_paper_pnl(self.session, ticker),
            backtest_history=_backtest_history(
                self.session,
                ticker,
                ranking.forecast_model if ranking else None,
            ),
            recent_snapshots=_recent_snapshots(self.session, ticker),
            recent_fills=_recent_fills(self.session, ticker),
            risk_checks=self.risk_checks(ticker),
            explanation=opportunity.__dict__ if opportunity is not None else {},
        )

    def execution_review(self, ticker: str) -> dict[str, Any] | None:
        detail = self.opportunity_detail(ticker)
        if detail is None:
            return None
        checks = self.risk_checks(ticker)
        return {
            "detail": detail,
            "checks": checks,
            "order_preview": self.order_preview(ticker),
            "environment": "DEMO ONLY",
            "dry_run": self.settings.execution_dry_run,
            "kill_switch": self.settings.execution_kill_switch,
            "execution_enabled": self.settings.execution_enabled,
            "can_execute": all(check.passed for check in checks)
            and self.settings.execution_enabled
            and not self.settings.execution_dry_run
            and not self.settings.execution_kill_switch,
            "confirmation_token": self.settings.execution_confirmation_token,
            "report_links": REPORT_LINKS,
        }

    def paper_trade(self, ticker: str) -> ActionResult:
        ranking = _latest_ranking_for_ticker(self.session, ticker)
        if ranking is None:
            return _action(ticker, "paper_trade", "NOT_FOUND", "No opportunity found.")
        if self.settings.ui_read_only:
            return _action(
                ticker,
                "paper_trade",
                "READ_ONLY",
                "UI_READ_ONLY=true; paper trade was not written.",
            )
        identity = verify_market_identity(
            self.session,
            ticker=ticker,
            ranking=ranking,
            settings=self.settings,
        )
        if not identity.tradeable:
            return _action(
                ticker,
                "paper_trade",
                "BLOCKED",
                f"Kalshi market link is not verified: {identity.url_verification_status}.",
            )
        paper_settings = learning_paper_settings(self.settings)
        decision = _paper_decision_from_ranking(
            self.session,
            ranking,
            paper_settings,
        )
        if decision is None:
            return _action(ticker, "paper_trade", "BLOCKED", "Opportunity lacks executable inputs.")
        order = create_paper_order(self.session, decision, settings=paper_settings)
        if order is None:
            return _action(ticker, "paper_trade", "DUPLICATE", "Paper order already exists.")
        fill = insert_paper_fill(
            self.session,
            order=order,
            price=to_decimal(order.limit_price) or decision.limit_price,
            quantity=order.quantity,
            fee=paper_settings.paper_default_fee_per_contract * order.quantity,
        )
        mark_order_filled(self.session, order)
        update_position_for_fill(self.session, fill)
        return _action(ticker, "paper_trade", "FILLED", f"Created paper order {order.id}.")

    def demo_dry_run(self, ticker: str) -> ActionResult:
        checks = self.risk_checks(ticker)
        preview = self.order_preview(ticker)
        report_path = write_execution_report(
            ticker=ticker,
            action="demo_dry_run",
            status="DRY_RUN",
            checks=checks,
            preview=preview,
        )
        return ActionResult(
            ticker=ticker,
            action="demo_dry_run",
            status="DRY_RUN",
            message="Dry-run only. No order was placed.",
            dry_run=True,
            report_path=str(report_path),
            checks=checks,
        )

    def demo_execute(self, ticker: str, *, confirmation: str | None = None) -> ActionResult:
        checks = self.risk_checks(ticker)
        preview = self.order_preview(ticker)
        if learning_blocks_demo_execution(self.settings):
            return _action(
                ticker,
                "demo_execute",
                "LEARNING_BLOCKED",
                "Learning Mode blocks demo execution; paper-only data capture remains enabled.",
                checks=checks,
            )
        if not self.settings.execution_enabled:
            return _action(
                ticker,
                "demo_execute",
                "DISABLED",
                "EXECUTION_ENABLED=false; demo execution is disabled.",
                checks=checks,
            )
        if self.settings.execution_kill_switch:
            return _action(
                ticker,
                "demo_execute",
                "BLOCKED",
                "Kill switch is active.",
                checks=checks,
            )
        if self.settings.execution_dry_run:
            report_path = write_execution_report(
                ticker=ticker,
                action="demo_execute",
                status="DRY_RUN",
                checks=checks,
                preview=preview,
            )
            return ActionResult(
                ticker=ticker,
                action="demo_execute",
                status="DRY_RUN",
                message="EXECUTION_DRY_RUN=true; no demo order was placed.",
                dry_run=True,
                report_path=str(report_path),
                checks=checks,
            )
        if confirmation != self.settings.execution_confirmation_token:
            return _action(
                ticker,
                "demo_execute",
                "CONFIRMATION_REQUIRED",
                "Typed confirmation did not match.",
                dry_run=False,
                checks=checks,
            )
        if not all(check.passed for check in checks):
            return _action(
                ticker,
                "demo_execute",
                "BLOCKED",
                "Risk checks failed.",
                dry_run=False,
                checks=checks,
            )
        report_path = write_execution_report(
            ticker=ticker,
            action="demo_execute",
            status="DEMO_EXECUTION_NOT_IMPLEMENTED",
            checks=checks,
            preview=preview,
        )
        return ActionResult(
            ticker=ticker,
            action="demo_execute",
            status="DEMO_EXECUTION_NOT_IMPLEMENTED",
            message="Demo execution path is gated; no real order was placed.",
            dry_run=False,
            report_path=str(report_path),
            checks=checks,
        )

    def order_preview(self, ticker: str) -> dict[str, Any]:
        ranking = _latest_ranking_for_ticker(self.session, ticker)
        if ranking is None:
            return {"ticker": ticker, "status": "missing_opportunity"}
        return {
            "ticker": ticker,
            "side": ranking.best_side,
            "price": ranking.best_price,
            "quantity": learning_paper_settings(self.settings).paper_max_order_quantity,
            "model_name": ranking.forecast_model,
            "estimated_edge": ranking.estimated_edge,
            "demo_only": True,
        }

    def risk_checks(self, ticker: str) -> list[RiskCheck]:
        ranking = _latest_ranking_for_ticker(self.session, ticker)
        snapshot = _latest_snapshot(self.session, ticker)
        identity = verify_market_identity(
            self.session,
            ticker=ticker,
            ranking=ranking,
            settings=self.settings,
        )
        checks = [
            RiskCheck("Environment", True, "DEMO ONLY; production live trading is unavailable."),
            RiskCheck("Secrets", True, "No secrets are displayed or required by this UI."),
            RiskCheck(
                "Execution enabled",
                self.settings.execution_enabled,
                "Set EXECUTION_ENABLED=true to enable demo execution controls.",
            ),
            RiskCheck(
                "Dry-run disabled for execute",
                not self.settings.execution_dry_run,
                "EXECUTION_DRY_RUN=true forces dry-run only.",
            ),
            RiskCheck(
                "Kill switch",
                not self.settings.execution_kill_switch,
                "EXECUTION_KILL_SWITCH must be false.",
            ),
            RiskCheck(
                "Learning Mode demo block",
                not learning_blocks_demo_execution(self.settings),
                "LEARNING_MODE=true blocks demo execution; paper betting remains available.",
            ),
            RiskCheck(
                "Opportunity exists",
                ranking is not None,
                "A ranked opportunity must exist.",
            ),
            RiskCheck(
                "Side and price",
                ranking is not None and bool(ranking.best_side) and bool(ranking.best_price),
                "Opportunity must include side and price.",
            ),
            RiskCheck(
                "Snapshot exists",
                snapshot is not None,
                "Latest market snapshot is required.",
            ),
            RiskCheck(
                "Verified Kalshi link",
                identity.tradeable,
                (
                    "Exact market has a verified Kalshi URL."
                    if identity.tradeable
                    else f"{identity.url_verification_status}: {identity.reason}"
                ),
            ),
        ]
        return checks

    def safety_state(self) -> dict[str, Any]:
        return {
            "environment": "DEMO ONLY",
            "ui_read_only": self.settings.ui_read_only,
            "execution_enabled": self.settings.execution_enabled,
            "execution_dry_run": self.settings.execution_dry_run,
            "execution_kill_switch": self.settings.execution_kill_switch,
            "learning_mode": self.settings.learning_mode,
            "learning_demo_blocked": learning_blocks_demo_execution(self.settings),
        }

    def _opportunity_from_ranking(
        self,
        ranking: MarketRanking,
        *,
        include_signal_badges: bool = True,
    ) -> OpportunityView:
        position = get_position(self.session, ranking.ticker)
        position_text = _position_text(position)
        market = self.session.get(Market, ranking.ticker)
        snapshot = _latest_snapshot(self.session, ranking.ticker)
        forecast = _latest_forecast(self.session, ranking.ticker, ranking.forecast_model)
        feature_snapshot = _latest_feature_snapshot(self.session, ranking.ticker)
        settlement = self.session.get(Settlement, ranking.ticker)
        market_legs = _market_legs(self.session, ranking.ticker)
        sizing_decision = _latest_sizing_decision(
            self.session,
            ranking.ticker,
            ranking.forecast_model,
        )
        risk_decision = _latest_risk_decision(self.session, ranking.ticker, ranking.forecast_model)
        consensus_signal = latest_consensus_for_ticker(self.session, ranking.ticker)
        identity = verify_market_identity(
            self.session,
            ranking=ranking,
            market=market,
            settings=self.settings,
        )
        identity_payload = identity.as_dict()
        explanation = explain_opportunity(
            ranking,
            snapshot=snapshot,
            forecast=forecast,
            consensus_signal=consensus_signal,
            position_text=position_text,
            settings=self.settings,
        )
        meta_selection = explain_meta_selection(self.session, ranking.ticker)
        title = identity.market_title or ranking.title or ranking.ticker
        metrics = payout_metrics_from_ranking(ranking)
        decision_clarity = build_decision_clarity(
            ranking=ranking,
            market=market,
            snapshot=snapshot,
            forecast=forecast,
            feature_snapshot=feature_snapshot,
            settlement=settlement,
            market_legs=market_legs,
            sizing_decision=sizing_decision,
            risk_decision=risk_decision,
            expected_value=metrics.expected_value,
            explanation=explanation,
            settings=self.settings,
        )
        decision_clarity["market_identity"] = identity_payload
        short_title = decision_clarity["market_structure"]["clean_title"] or summarize_market_title(
            title
        )
        category = identity.category or decision_clarity["market_structure"]["category"] or classify_market_category(
            title,
            ranking.series_ticker,
        )
        fresh = is_fresh_timestamp(
            getattr(snapshot, "captured_at", None),
            fresh_data_minutes=self.settings.autopilot_require_fresh_data_minutes,
        )
        traffic = traffic_light_label(
            opportunity_score=ranking.opportunity_score,
            edge=ranking.estimated_edge,
            spread=ranking.spread,
            liquidity=ranking.liquidity_score,
            confidence=ranking.model_confidence_score,
            is_fresh=fresh,
        )
        meter = risk_meter(
            opportunity_score=ranking.opportunity_score,
            edge=ranking.estimated_edge,
            spread=ranking.spread,
            liquidity=ranking.liquidity_score,
            confidence=ranking.model_confidence_score,
            is_fresh=fresh,
        )
        side_for_label = None if traffic["kind"] == "avoid" else ranking.best_side
        return OpportunityView(
            ticker=ranking.ticker,
            title=title,
            short_title=short_title,
            category=category,
            category_badge=category_badge(category),
            model_name=ranking.forecast_model,
            side=ranking.best_side or "n/a",
            recommendation_label=recommendation_label(side_for_label),
            price=ranking.best_price or "n/a",
            opportunity_score=ranking.opportunity_score,
            estimated_edge=ranking.estimated_edge or "n/a",
            expected_value=decimal_to_str(metrics.expected_value) or "n/a",
            payout_to_risk_ratio=decimal_to_str(metrics.payout_to_risk_ratio) or "n/a",
            payout_adjusted_score=decimal_to_str(metrics.payout_adjusted_score) or "0",
            spread=ranking.spread or "n/a",
            liquidity=ranking.liquidity or "n/a",
            liquidity_score=ranking.liquidity_score,
            confidence_percent=_score_percent(ranking.model_confidence_score),
            formatted_time_remaining=format_time_remaining(ranking.time_to_close_minutes),
            time_to_close_minutes=ranking.time_to_close_minutes or "n/a",
            model_confidence_score=ranking.model_confidence_score,
            paper_position=position_text,
            demo_execution_status=_demo_status(self.settings),
            ranking_id=ranking.id,
            recommendation=explanation["recommendation"],
            confidence_label=explanation["confidence_label"],
            edge_cents=explanation["edge_cents"],
            score_label=explanation["score_label"],
            top_reason=explanation["top_reason"],
            top_risk=explanation["top_risk"],
            badges=explanation["badges"],
            traffic_light=traffic,
            risk_meter=meter,
            why_interesting=explanation["why_interesting"],
            why_risky=explanation["why_risky"],
            what_bot_would_do=explanation["what_bot_would_do"],
            primary_driver=explanation["primary_driver"],
            supporting_signals=explanation["supporting_signals"],
            model_confidence=explanation["model_confidence"],
            data_freshness=explanation["data_freshness"],
            recommended_action=explanation["recommended_action"],
            model_explanation=explanation["model_explanation"],
            risks=explanation["risks"],
            forum_consensus=explanation["forum_consensus"],
            microstructure=_microstructure_summary(self.session, ranking.ticker),
            signal_badges=(
                signal_badges_for_opportunity(
                    self.session,
                    ticker=ranking.ticker,
                    model_name=ranking.forecast_model,
                    limit=3,
                )
                if include_signal_badges
                else []
            ),
            meta_selection=meta_selection,
            decision_clarity=decision_clarity,
            market_identity=identity_payload,
        )


def write_execution_report(
    *,
    ticker: str,
    action: str,
    status: str,
    checks: list[RiskCheck],
    preview: dict[str, Any],
) -> Path:
    output = Path("reports/execution_report.md")
    output.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Execution Report",
        "",
        f"- Generated at: {utc_now().isoformat()}",
        f"- Ticker: {ticker}",
        f"- Action: {action}",
        f"- Status: {status}",
        "- Environment: DEMO ONLY",
        "",
        "## Order Preview",
        "",
        "```json",
        json.dumps(preview, indent=2, sort_keys=True, default=str),
        "```",
        "",
        "## Risk Checks",
        "",
    ]
    for check in checks:
        lines.append(f"- [{'PASS' if check.passed else 'FAIL'}] {check.name}: {check.detail}")
    lines.extend(["", "No production live order was placed.", ""])
    output.write_text("\n".join(lines), encoding="utf-8")
    return output


def _top_rankings(session: Session, limit: int) -> list[MarketRanking]:
    return get_recent_rankings(session, limit=limit)


def _is_direct_review_opportunity(item: OpportunityView) -> bool:
    if not is_tradeable_identity(item.market_identity):
        return False
    clarity = item.decision_clarity or {}
    structure = clarity.get("market_structure") or {}
    if structure.get("parser_status") in {"UNSUPPORTED_MULTI_LEG", "CROSS_CATEGORY"}:
        return False
    if clarity.get("execution_quality") in {"Stale Quote", "No Liquidity"}:
        return False
    return True


def _fast_opportunity_row(
    session: Session,
    ranking: MarketRanking,
    *,
    settings: Settings,
) -> dict[str, Any]:
    identity = verify_market_identity(session, ranking=ranking, settings=settings)
    identity_fields = market_identity_fields(identity)
    identity_payload = identity.as_dict()
    title = identity.market_title or ranking.title or ranking.ticker
    metrics = payout_metrics_from_ranking(ranking)
    category = identity.category or classify_market_category(title, ranking.series_ticker)
    fresh = is_fresh_timestamp(
        ranking.ranked_at,
        fresh_data_minutes=settings.autopilot_require_fresh_data_minutes,
    )
    traffic = traffic_light_label(
        opportunity_score=ranking.opportunity_score,
        edge=ranking.estimated_edge,
        spread=ranking.spread,
        liquidity=ranking.liquidity_score,
        confidence=ranking.model_confidence_score,
        is_fresh=fresh,
    )
    side_for_label = None if traffic["kind"] == "avoid" else ranking.best_side
    is_blocked = _fast_is_blocked_research(ranking, traffic=traffic, is_fresh=fresh)
    blocked_reason, blocked_detail = _fast_blocked_research_reason(
        ranking,
        traffic=traffic,
        is_fresh=fresh,
    )
    if not identity.tradeable:
        previous_reason = blocked_reason
        previous_detail = blocked_detail
        is_blocked = True
        blocked_reason = identity.status_label
        if previous_reason:
            blocked_detail = (
                f"{identity.reason} Existing readiness blocker: "
                f"{previous_reason}: {previous_detail}"
            )
        else:
            blocked_detail = identity.reason
    return {
        "ticker": ranking.ticker,
        **identity_fields,
        "market_identity": identity_payload,
        "title": title,
        "short_title": summarize_market_title(title),
        "category": category,
        "category_badge": category_badge(category),
        "model_name": ranking.forecast_model,
        "side": ranking.best_side or "n/a",
        "recommendation_label": recommendation_label(side_for_label),
        "price": ranking.best_price or "n/a",
        "opportunity_score": ranking.opportunity_score,
        "opportunity_score_label": _format_score_decimal(ranking.opportunity_score),
        "estimated_edge": ranking.estimated_edge or "n/a",
        "expected_value": decimal_to_str(metrics.expected_value) or "n/a",
        "payout_to_risk_ratio": decimal_to_str(metrics.payout_to_risk_ratio) or "n/a",
        "payout_adjusted_score": decimal_to_str(metrics.payout_adjusted_score) or "0",
        "spread": ranking.spread or "n/a",
        "liquidity": ranking.liquidity or "n/a",
        "liquidity_score": ranking.liquidity_score,
        "confidence_percent": _score_percent(ranking.model_confidence_score),
        "formatted_time_remaining": format_time_remaining(ranking.time_to_close_minutes),
        "time_to_close_minutes": ranking.time_to_close_minutes or "n/a",
        "model_confidence_score": ranking.model_confidence_score,
        "ranking_id": ranking.id,
        "recommendation": "Review" if not is_blocked else "Blocked research",
        "confidence_label": _confidence_label(ranking.model_confidence_score),
        "edge_cents": _edge_cents(ranking.estimated_edge),
        "score_label": _format_score_decimal(ranking.opportunity_score),
        "traffic_light": traffic,
        "data_freshness": "fresh ranking" if fresh else "stale ranking",
        "recommended_action": "Review paper candidate" if not is_blocked else blocked_reason,
        "blocked_reason": blocked_reason,
        "blocked_reason_detail": blocked_detail,
        "is_blocked_research": is_blocked,
        "rank_sort": to_decimal(ranking.opportunity_score) or Decimal("0"),
        "detail_href": f"/opportunities/{ranking.ticker}",
    }


def _fast_is_blocked_research(
    ranking: MarketRanking,
    *,
    traffic: dict[str, str],
    is_fresh: bool,
) -> bool:
    ticker = ranking.ticker.upper()
    if ticker.startswith(("KXMVECROSSCATEGORY-", "KXMVESPORTSMULTIGAMEEXTENDED-")):
        return True
    if not is_fresh:
        return True
    if traffic.get("kind") == "avoid":
        return True
    liquidity = to_decimal(ranking.liquidity_score)
    if liquidity is not None and liquidity <= Decimal("0"):
        return True
    return False


def _fast_blocked_research_reason(
    ranking: MarketRanking,
    *,
    traffic: dict[str, str],
    is_fresh: bool,
) -> tuple[str, str]:
    ticker = ranking.ticker.upper()
    score_label = _format_score_decimal(ranking.opportunity_score)
    if ticker.startswith(("KXMVECROSSCATEGORY-", "KXMVESPORTSMULTIGAMEEXTENDED-")):
        return (
            "Synthetic composite",
            "Needs the guarded composite settlement resolver before it can be "
            "treated as a clean direct market.",
        )
    if not is_fresh:
        return (
            "Stale market data",
            "Refresh snapshots before ranking or paper-ready checks can trust this row.",
        )

    liquidity = to_decimal(ranking.liquidity_score)
    if liquidity is None:
        return (
            "Missing liquidity evidence",
            "No usable book/liquidity score is attached to the latest ranking.",
        )
    if liquidity <= Decimal("0"):
        return (
            "No visible liquidity",
            "Wait for an executable book with usable bid/ask depth before paper entry.",
        )
    if liquidity < Decimal("25"):
        return (
            "Thin liquidity",
            f"Liquidity score {_format_score_decimal(liquidity)} is below the 25 ready filter.",
        )

    spread = to_decimal(ranking.spread)
    if spread is not None and spread > Decimal("0.10"):
        return (
            "Spread too wide",
            f"Spread {_format_score_decimal(spread)} is above the 0.10 ready filter.",
        )

    confidence = to_decimal(ranking.model_confidence_score)
    if confidence is not None and confidence < Decimal("20"):
        return (
            "Low model confidence",
            f"Model confidence {_format_score_decimal(confidence)} is below the 20 ready filter.",
        )

    score = to_decimal(ranking.opportunity_score)
    if score is None:
        return (
            "Missing score evidence",
            "The row needs a valid opportunity score before it can enter the ready list.",
        )
    if score < Decimal("60"):
        return (
            "Below ready score",
            f"Score {score_label} is below the 60 ready filter; keep watching "
            "for better EV, spread, or liquidity.",
        )

    if traffic.get("kind") == "avoid":
        return (
            "Paper-ready gates blocked",
            "The traffic-light preflight still marks this row as avoid.",
        )
    return (
        "Paper-ready gates blocked",
        "One or more paper-only readiness checks still needs clean evidence.",
    )


def _format_score_decimal(value: Any) -> str:
    decimal_value = to_decimal(value)
    if decimal_value is None:
        return "n/a"
    rounded = decimal_value.quantize(Decimal("0.01"))
    text = format(rounded, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _confidence_label(value: Any) -> str:
    score = to_decimal(value)
    if score is None:
        return "Unknown"
    if score >= Decimal("75"):
        return "High"
    if score >= Decimal("50"):
        return "Medium"
    return "Low"


def _edge_cents(value: Any) -> str:
    decimal_value = to_decimal(value)
    if decimal_value is None:
        return "n/a"
    cents = (decimal_value * Decimal("100")).quantize(Decimal("0.1"))
    return f"{cents}c"


def _latest_ranking_for_ticker(session: Session, ticker: str) -> MarketRanking | None:
    return session.scalar(
        select(MarketRanking)
        .where(MarketRanking.ticker == ticker)
        .order_by(desc(MarketRanking.ranked_at), desc(MarketRanking.id))
        .limit(1)
    )


def _latest_snapshot(session: Session, ticker: str) -> MarketSnapshot | None:
    return session.scalar(
        select(MarketSnapshot)
        .where(MarketSnapshot.ticker == ticker)
        .order_by(desc(MarketSnapshot.captured_at), desc(MarketSnapshot.id))
        .limit(1)
    )


def _latest_forecast(
    session: Session,
    ticker: str,
    model_name: str | None = None,
) -> Forecast | None:
    statement = select(Forecast).where(Forecast.ticker == ticker)
    if model_name:
        statement = statement.where(Forecast.model_name == model_name)
    return session.scalar(
        statement.order_by(desc(Forecast.forecasted_at), desc(Forecast.id)).limit(1)
    )


def _latest_feature_snapshot(session: Session, ticker: str) -> FeatureSnapshot | None:
    return session.scalar(
        select(FeatureSnapshot)
        .where(FeatureSnapshot.ticker == ticker)
        .order_by(desc(FeatureSnapshot.captured_at), desc(FeatureSnapshot.id))
        .limit(1)
    )


def _market_legs(session: Session, ticker: str) -> list[MarketLeg]:
    return list(
        session.scalars(
            select(MarketLeg)
            .where(MarketLeg.ticker == ticker)
            .order_by(MarketLeg.leg_index, MarketLeg.id)
        )
    )


def _latest_sizing_decision(
    session: Session,
    ticker: str,
    model_name: str | None,
) -> PositionSizingDecisionLog | None:
    statement = select(PositionSizingDecisionLog).where(PositionSizingDecisionLog.ticker == ticker)
    if model_name:
        statement = statement.where(PositionSizingDecisionLog.model_name == model_name)
    return session.scalar(
        statement.order_by(
            desc(PositionSizingDecisionLog.decision_timestamp),
            desc(PositionSizingDecisionLog.id),
        ).limit(1)
    )


def _latest_risk_decision(
    session: Session,
    ticker: str,
    model_name: str | None,
) -> AdvancedRiskDecisionLog | None:
    statement = select(AdvancedRiskDecisionLog).where(AdvancedRiskDecisionLog.ticker == ticker)
    if model_name:
        statement = statement.where(AdvancedRiskDecisionLog.model_id == model_name)
    return session.scalar(
        statement.order_by(
            desc(AdvancedRiskDecisionLog.decision_timestamp),
            desc(AdvancedRiskDecisionLog.id),
        ).limit(1)
    )


def _forecast_history(session: Session, ticker: str) -> list[dict[str, Any]]:
    forecasts = session.scalars(
        select(Forecast)
        .where(Forecast.ticker == ticker)
        .order_by(desc(Forecast.forecasted_at), desc(Forecast.id))
        .limit(20)
    )
    return [
        {
            "forecasted_at": forecast.forecasted_at.isoformat(),
            "model_name": forecast.model_name,
            "yes_probability": forecast.yes_probability,
            "market_mid_probability": forecast.market_mid_probability,
            "notes": forecast.notes,
        }
        for forecast in forecasts
    ]


def _component_probabilities(feature_json: dict[str, Any]) -> dict[str, Any]:
    return (
        feature_json.get("component_forecasts")
        or feature_json.get("component_model_probabilities")
        or {}
    )


def _score_breakdown(ranking: MarketRanking | None) -> dict[str, Any]:
    if ranking is None:
        return {}
    return {
        "opportunity_score": ranking.opportunity_score,
        "estimated_edge": ranking.estimated_edge,
        "liquidity_score": ranking.liquidity_score,
        "spread_score": ranking.spread_score,
        "time_score": ranking.time_score,
        "model_confidence_score": ranking.model_confidence_score,
        "reason": ranking.reason,
    }


def _paper_pnl(session: Session, ticker: str) -> list[dict[str, Any]]:
    rows = session.scalars(
        select(PaperPnl)
        .where(PaperPnl.ticker == ticker)
        .order_by(desc(PaperPnl.calculated_at), desc(PaperPnl.id))
        .limit(10)
    )
    return [
        {
            "calculated_at": row.calculated_at.isoformat(),
            "realized_pnl": row.realized_pnl,
            "unrealized_pnl": row.unrealized_pnl,
            "total_pnl": row.total_pnl,
            "notes": row.notes,
        }
        for row in rows
    ]


def _backtest_history(
    session: Session,
    ticker: str,
    model_name: str | None,
) -> list[dict[str, Any]]:
    statement = (
        select(BacktestTrade, BacktestRun)
        .join(BacktestRun, BacktestTrade.backtest_run_id == BacktestRun.id)
        .where(BacktestTrade.ticker == ticker)
        .order_by(desc(BacktestTrade.simulated_at), desc(BacktestTrade.id))
        .limit(20)
    )
    if model_name:
        statement = statement.where(BacktestRun.model_name == model_name)
    return [
        {
            "simulated_at": trade.simulated_at.isoformat(),
            "model_name": run.model_name,
            "side": trade.side,
            "price": trade.price,
            "edge": trade.edge,
            "pnl": trade.pnl,
            "settlement_result": trade.settlement_result,
        }
        for trade, run in session.execute(statement).all()
    ]


def _recent_snapshots(session: Session, ticker: str) -> list[dict[str, Any]]:
    snapshots = session.scalars(
        select(MarketSnapshot)
        .where(MarketSnapshot.ticker == ticker)
        .order_by(desc(MarketSnapshot.captured_at), desc(MarketSnapshot.id))
        .limit(10)
    )
    return [
        {
            "captured_at": snapshot.captured_at.isoformat(),
            "best_yes_bid": snapshot.best_yes_bid,
            "best_yes_ask": snapshot.best_yes_ask,
            "best_no_bid": snapshot.best_no_bid,
            "best_no_ask": snapshot.best_no_ask,
            "spread": snapshot.spread,
        }
        for snapshot in snapshots
    ]


def _recent_fills(session: Session, ticker: str) -> list[dict[str, Any]]:
    fills = session.scalars(
        select(PaperFill)
        .where(PaperFill.ticker == ticker)
        .order_by(desc(PaperFill.filled_at), desc(PaperFill.id))
        .limit(20)
    )
    return [
        {
            "filled_at": fill.filled_at.isoformat(),
            "side": fill.side,
            "price": fill.price,
            "quantity": fill.quantity,
            "fee": fill.fee,
        }
        for fill in fills
    ]


def _orderbook_summary(snapshot: MarketSnapshot | None) -> dict[str, Any]:
    if snapshot is None:
        return {"status": "missing"}
    return {
        "captured_at": snapshot.captured_at.isoformat(),
        "best_yes_bid": snapshot.best_yes_bid,
        "best_yes_ask": snapshot.best_yes_ask,
        "best_no_bid": snapshot.best_no_bid,
        "best_no_ask": snapshot.best_no_ask,
        "spread": snapshot.spread,
    }


def _rules_text(market: Market | None) -> str:
    if market is None:
        return "No market rules found."
    return (
        "\n\n".join(part for part in (market.rules_primary, market.rules_secondary) if part)
        or "No market rules found."
    )


def _position_text(position: Any) -> str:
    if position is None:
        return "none"
    return (
        f"YES {position.yes_contracts}, "
        f"NO {position.no_contracts}, "
        f"realized {position.realized_pnl}"
    )


def _demo_status(settings: Settings) -> str:
    if learning_blocks_demo_execution(settings):
        return "LEARNING_BLOCKED"
    if not settings.execution_enabled:
        return "DISABLED"
    if settings.execution_kill_switch:
        return "KILL_SWITCH"
    if settings.execution_dry_run:
        return "DRY_RUN_ONLY"
    return "REVIEW_REQUIRED"


def _executive_summary(
    session: Session,
    *,
    opportunities: list[OpportunityView],
    portfolio: dict[str, Any],
    model_rows: list[dict[str, Any]],
    autopilot_status: dict[str, Any],
) -> dict[str, Any]:
    today = utc_now().replace(hour=0, minute=0, second=0, microsecond=0)
    best_model = _best_model_label(model_rows)
    largest_exposure = max(
        portfolio.get("positions", []),
        key=lambda row: to_decimal(row.get("exposure")) or Decimal("0"),
        default=None,
    )
    latest_snapshot = session.scalar(
        select(MarketSnapshot)
        .order_by(desc(MarketSnapshot.captured_at), desc(MarketSnapshot.id))
        .limit(1)
    )
    return {
        "markets_scanned": _count_since(session, MarketSnapshot, MarketSnapshot.captured_at, today),
        "forecasts_generated": _count_since(session, Forecast, Forecast.forecasted_at, today),
        "opportunities_found": _count_since(
            session,
            MarketOpportunity,
            MarketOpportunity.detected_at,
            today,
        ),
        "paper_trades_open": portfolio.get("open_orders", 0),
        "paper_pnl": portfolio.get("total_pnl", "0"),
        "realized_pnl": portfolio.get("realized_pnl", "0"),
        "unrealized_pnl": portfolio.get("unrealized_pnl", "0"),
        "open_positions": portfolio.get("open_positions", 0),
        "open_opportunities": len(opportunities),
        "best_model": best_model,
        "best_opportunity": opportunities[0].short_title if opportunities else "Run a scan first.",
        "largest_exposure": (
            f"{largest_exposure['ticker']} {largest_exposure['exposure']}"
            if largest_exposure
            else "No paper exposure"
        ),
        "autopilot_status": autopilot_status.get("plain_status", "Autopilot state unavailable."),
        "last_data_refresh": latest_snapshot.captured_at.isoformat() if latest_snapshot else "n/a",
    }


def paper_only_soak_status(
    *,
    report_path: Path = GH2_SOAK_REPORT_PATH,
    history_path: Path = GH2_SOAK_HISTORY_PATH,
    gh1_status_path: Path = GH1_WATCH_STATUS_PATH,
    scheduler_status_path: Path = GH2_SCHEDULER_STATUS_PATH,
    now: datetime | None = None,
) -> dict[str, Any]:
    return build_gh3_soak_status(
        report_path=report_path,
        history_path=history_path,
        gh1_status_path=gh1_status_path,
        scheduler_status_path=scheduler_status_path,
        now=now,
    )


def crypto_freshness_watch_status(
    *,
    report_path: Path = CRYPTO_FRESHNESS_REPORT_PATH,
    status_path: Path | None = None,
    phase3ak_status_path: Path | None = PHASE3AK_CRYPTO_WATCH_STATUS_PATH,
    now: datetime | None = None,
) -> dict[str, Any]:
    generated_at: datetime | None = None
    payload: dict[str, Any] = {}
    if report_path.exists():
        try:
            loaded = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            loaded = {}
        if isinstance(loaded, dict):
            payload = loaded
            generated_at = parse_datetime(loaded.get("generated_at"))
    status_payload = _crypto_freshness_status_payload(
        report_path=report_path,
        status_path=status_path,
    )
    guard = status_payload.get("guard") if isinstance(status_payload.get("guard"), dict) else {}
    latest_summary = (
        status_payload.get("latest_summary")
        if isinstance(status_payload.get("latest_summary"), dict)
        else {}
    )
    guard_status = str(guard.get("status") or "UNKNOWN")
    scheduled_owner_active = bool(guard.get("scheduled_owner_active"))
    guard_recommended_next_action = str(
        guard.get("recommended_next_action")
        or status_payload.get("recommended_next_action")
        or ""
    )

    resolved_now = now or utc_now()
    options = payload.get("options") if isinstance(payload.get("options"), dict) else {}
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    cadence = int(options.get("cadence_minutes") or 15)
    freshness = int(options.get("freshness_minutes") or cadence)
    freshness_window = max(cadence, freshness, 1)
    age_minutes = (
        (resolved_now - generated_at).total_seconds() / 60
        if generated_at is not None
        else None
    )
    if age_minutes is None:
        status = "NOT_RUN"
        label = "Not run"
        badge_kind = "warn"
        description = "No Phase 3BC-R5 crypto freshness report exists yet."
    elif (
        guard_status in {"STOPPED_WITH_STALE_PID", "STOPPED", "NO_UNATTENDED_JOB"}
        and age_minutes > freshness_window
    ):
        status = "WATCHER_STOPPED"
        label = "Watcher stopped"
        badge_kind = "warn"
        description = (
            "The last crypto freshness report is stale because no guarded "
            "Phase 3BC-R5 watcher process is running."
        )
    elif guard_status == "RUNNING" and age_minutes > freshness_window:
        status = "RUNNER_STALLED"
        label = "Runner stale"
        badge_kind = "warn"
        description = (
            "The guarded crypto watcher appears to be running, but it has not "
            "written a fresh report inside the configured watch window."
        )
    elif age_minutes <= freshness_window:
        status = "FRESH"
        label = "Fresh"
        badge_kind = "good"
        description = (
            "Crypto rankings are inside the configured "
            f"{freshness_window}-minute watch window."
        )
    else:
        status = "STALE"
        label = "Stale"
        badge_kind = "warn"
        description = (
            "Crypto rankings are older than the configured "
            f"{freshness_window}-minute watch window."
        )

    actionability_gap = _crypto_actionability_gap(summary)
    book_probe = _crypto_book_probe(payload, summary)
    cycle_number = _coerce_positive_int(summary.get("cycle_number")) or _coerce_positive_int(
        latest_summary.get("cycle_number")
    )
    total_cycles = _coerce_positive_int(summary.get("total_cycles")) or _coerce_positive_int(
        latest_summary.get("total_cycles")
    )
    elapsed_seconds = _coerce_nonnegative_float(guard.get("elapsed_seconds"))
    duration_budget_seconds = _coerce_positive_float(guard.get("duration_budget_seconds"))
    progress_percent = _crypto_watch_progress_percent(
        cycle_number=cycle_number,
        total_cycles=total_cycles,
        elapsed_seconds=elapsed_seconds,
        duration_budget_seconds=duration_budget_seconds,
    )
    remaining_seconds = _crypto_watch_remaining_seconds(
        cycle_number=cycle_number,
        total_cycles=total_cycles,
        cadence_minutes=cadence,
        elapsed_seconds=elapsed_seconds,
        duration_budget_seconds=duration_budget_seconds,
    )
    status_payload = {
        "status": status,
        "status_label": label,
        "badge_kind": badge_kind,
        "description": description,
        "generated_at": generated_at.isoformat() if generated_at else "n/a",
        "age_label": _format_age_minutes(age_minutes),
        "auto_refresh_seconds": (
            60 if bool(guard.get("running")) or scheduled_owner_active else 0
        ),
        "auto_refresh_label": (
            "60s" if bool(guard.get("running")) or scheduled_owner_active else "off"
        ),
        "cadence_minutes": cadence,
        "freshness_minutes": freshness,
        "freshness_window_minutes": freshness_window,
        "watch_state": summary.get("watch_state") or "UNKNOWN",
        "watch_state_label": _format_enum_label(summary.get("watch_state") or "UNKNOWN"),
        "runner_status": guard_status,
        "runner_status_label": _format_enum_label(guard_status),
        "runner_running": bool(guard.get("running")),
        "scheduled_owner_active": scheduled_owner_active,
        "scheduler_owner": guard.get("scheduler_owner"),
        "runner_pid": guard.get("pid"),
        "runner_next_action": guard_recommended_next_action,
        "r5_latest_report_generated_at": status_payload.get("latest_report_generated_at"),
        "cycle_number": cycle_number or "n/a",
        "total_cycles": total_cycles or "n/a",
        "watch_progress_percent": progress_percent,
        "watch_progress_label": _crypto_watch_progress_label(
            cycle_number=cycle_number,
            total_cycles=total_cycles,
            progress_percent=progress_percent,
        ),
        "elapsed_label": _format_duration_seconds(elapsed_seconds),
        "eta_label": _format_duration_seconds(remaining_seconds),
        "active_pure_crypto_rows": summary.get("active_pure_crypto_rows", 0),
        "current_active_window_rows": summary.get("current_active_window_rows", 0),
        "expired_crypto_window_rows": summary.get("expired_crypto_window_rows", 0),
        "paper_ready_candidates": summary.get("paper_ready_candidates", 0),
        "positive_ev_rows": summary.get("positive_ev_rows", 0),
        "positive_ev_no_executable_book_rows": summary.get(
            "positive_ev_no_executable_book_rows",
            0,
        ),
        "positive_ev_liquidity_positive_rows": summary.get(
            "positive_ev_liquidity_positive_rows",
            0,
        ),
        "positive_ev_clean_book_rows": summary.get("positive_ev_clean_book_rows", 0),
        "positive_ev_snapshot_stale_rows": summary.get(
            "positive_ev_snapshot_stale_rows",
            0,
        ),
        "positive_ev_forecast_stale_rows": summary.get(
            "positive_ev_forecast_stale_rows",
            0,
        ),
        "positive_ev_spread_blocked_rows": summary.get(
            "positive_ev_spread_blocked_rows",
            0,
        ),
        "positive_ev_clean_book_risk_missing_rows": summary.get(
            "positive_ev_clean_book_risk_missing_rows",
            0,
        ),
        "positive_ev_preflight_candidates": summary.get(
            "positive_ev_preflight_candidates",
            0,
        ),
        "exact_snapshot_refresh_selected": summary.get(
            "exact_snapshot_refresh_selected",
            0,
        ),
        "exact_snapshot_refresh_attempted": summary.get(
            "exact_snapshot_refresh_attempted",
            0,
        ),
        "exact_snapshot_refresh_repaired": summary.get(
            "exact_snapshot_refresh_repaired",
            0,
        ),
        "exact_snapshot_refresh_book_visible_candidates": summary.get(
            "exact_snapshot_refresh_book_visible_candidates",
            0,
        ),
        "exact_snapshot_refresh_no_book_recheck_candidates": summary.get(
            "exact_snapshot_refresh_no_book_recheck_candidates",
            0,
        ),
        "exact_snapshot_refresh_candidate_filter": summary.get(
            "exact_snapshot_refresh_candidate_filter",
            "n/a",
        ),
        "liquidity_actionability_state": summary.get("liquidity_actionability_state")
        or "UNKNOWN",
        "actionability_gap": actionability_gap,
        "actionability_gap_label": _crypto_actionability_gap_label(actionability_gap),
        "actionability_note": _crypto_actionability_note(summary, actionability_gap),
        "book_probe": book_probe,
        "book_probe_available": bool(book_probe.get("available")),
        "primary_gap": summary.get("primary_gap_after_refresh") or "UNKNOWN",
        "primary_gap_label": _format_enum_label(
            summary.get("primary_gap_after_refresh") or "UNKNOWN"
        ),
        "primary_gap_scope": summary.get("primary_gap_scope") or "UNKNOWN",
        "snapshot_stale_rows": summary.get("snapshot_stale_rows", 0),
        "snapshot_missing_rows": summary.get("snapshot_missing_rows", 0),
        "missing_executable_price_rows": summary.get(
            "missing_executable_price_rows",
            0,
        ),
        "forecast_stale_rows": summary.get("forecast_stale_rows", 0),
        "ranking_missing_rows": summary.get("ranking_missing_rows", 0),
        "ranking_stale_rows": summary.get("ranking_stale_rows", 0),
        "ranking_coverage_gap_after_repair": summary.get(
            "ranking_coverage_gap_after_repair",
            0,
        ),
        "true_ranking_gap_after_repair": summary.get(
            "true_ranking_gap_after_repair",
            0,
        ),
        "clean_execution_rows": summary.get("clean_execution_rows", 0),
        "ev_calibration_state": summary.get("ev_calibration_state") or "UNKNOWN",
        "ev_calibration_label": _crypto_ev_calibration_label(
            summary.get("ev_calibration_state")
        ),
        "best_current_expected_value_cents": summary.get(
            "best_current_expected_value_cents",
            "n/a",
        ),
        "best_current_expected_value_label": _format_cents(
            summary.get("best_current_expected_value_cents")
        ),
        "best_ev_candidate_ticker": summary.get("best_ev_candidate_ticker") or "n/a",
        "best_ev_gap_to_positive_cents": summary.get(
            "best_ev_gap_to_positive_cents",
            "n/a",
        ),
        "best_ev_gap_to_positive_label": _format_cents(
            summary.get("best_ev_gap_to_positive_cents")
        ),
        "ev_near_miss_rows": summary.get("ev_near_miss_rows", 0),
        "ev_near_miss_liquidity_positive_rows": summary.get(
            "ev_near_miss_liquidity_positive_rows",
            0,
        ),
        "ev_near_miss_clean_execution_rows": summary.get(
            "ev_near_miss_clean_execution_rows",
            0,
        ),
        "liquidity_emergence_rows": summary.get("liquidity_emergence_rows", 0),
        "positive_ev_liquidity_emergence_rows": summary.get(
            "positive_ev_liquidity_emergence_rows",
            0,
        ),
        "near_miss_liquidity_emergence_rows": summary.get(
            "near_miss_liquidity_emergence_rows",
            0,
        ),
        "clean_execution_emergence_rows": summary.get(
            "clean_execution_emergence_rows",
            0,
        ),
        "positive_ev_clean_execution_emergence_rows": summary.get(
            "positive_ev_clean_execution_emergence_rows",
            0,
        ),
        "near_miss_clean_book_emergence_rows": summary.get(
            "near_miss_clean_book_emergence_rows",
            0,
        ),
        "liquidity_emergence_summary": _crypto_liquidity_emergence_summary(summary),
        "liquidity_emergence_examples": _crypto_liquidity_emergence_examples(payload),
        "ev_near_miss_band_label": _format_cents(
            summary.get("ev_near_miss_band_cents")
        ),
        "near_miss_summary": _crypto_near_miss_summary(summary),
        "near_miss_examples": _crypto_near_miss_examples(payload),
        "gate_failure_examples": _crypto_gate_failure_examples(payload),
        "command": CRYPTO_WATCH_COMMAND,
        "scheduler_command": "kalshi-bot scheduler-plan --profile crypto-watch",
        "report_href": CRYPTO_FRESHNESS_REPORT_HREF,
    }
    if phase3ak_status_path is not None:
        _apply_phase3ak_crypto_watch_status(status_payload, path=phase3ak_status_path)
    return status_payload


def free_source_hunt_status(
    *,
    report_path: Path = PHASE3AY_FREE_SOURCE_SPRINT_PATH,
) -> dict[str, Any]:
    payload = _load_json_payload(report_path)
    if not payload:
        return {
            "status": "NOT_RUN",
            "status_label": "Run sprint",
            "badge_kind": "warn",
            "generated_at": "n/a",
            "best_next_category": "n/a",
            "next_codex_sprint": "Phase 3AY Free Source Sprint Report",
            "first_hard_blocker": "REPORT_NOT_RUN",
            "operator_next_command": (
                "kalshi-bot phase3ay-free-source-sprint-report "
                "--output-dir reports/phase3ay --reports-dir reports"
            ),
            "markets_scanned": 0,
            "positive_ev_rows": 0,
            "paper_ready_rows": 0,
            "rows": [],
            "report_href": "/reports/phase3ay/EXECUTIVE_SUMMARY.md",
            "scorecard_href": "/reports/phase3ay/category_readiness.md",
        }
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    readiness = (
        payload.get("category_readiness")
        if isinstance(payload.get("category_readiness"), dict)
        else {}
    )
    scorecard = (
        readiness.get("category_scorecard")
        if isinstance(readiness.get("category_scorecard"), list)
        else []
    )
    rows = [
        {
            "category": str(row.get("category") or "unknown"),
            "score": row.get("sprint_score", 0),
            "current_markets": row.get("current_active_markets", 0),
            "source_ready": row.get("free_source_available_rows", 0),
            "linked": row.get("linked_rows", 0),
            "forecast_ready": row.get("forecast_ready_rows", 0),
            "book_ready": row.get("book_ready_rows", 0),
            "positive_ev": row.get("positive_ev_rows", 0),
            "paper_ready": row.get("paper_ready_rows", 0),
            "top_blocker": row.get("top_blocker") or "UNKNOWN",
            "next_action": row.get("next_action") or "Run the sprint report.",
        }
        for row in scorecard[:6]
        if isinstance(row, dict)
    ]
    return {
        "status": "READY",
        "status_label": str(summary.get("best_next_category") or "Review report"),
        "badge_kind": "info",
        "generated_at": str(payload.get("generated_at") or "n/a"),
        "best_next_category": str(summary.get("best_next_category") or "n/a"),
        "next_codex_sprint": str(summary.get("next_codex_sprint") or "n/a"),
        "first_hard_blocker": str(summary.get("first_hard_blocker") or "UNKNOWN"),
        "operator_next_command": str(
            summary.get("operator_next_command")
            or (
                "kalshi-bot phase3ay-free-source-sprint-report "
                "--output-dir reports/phase3ay --reports-dir reports"
            )
        ),
        "markets_scanned": summary.get("markets_scanned", 0),
        "positive_ev_rows": summary.get("positive_ev_rows", 0),
        "paper_ready_rows": summary.get("paper_ready_rows", 0),
        "rows": rows,
        "report_href": "/reports/phase3ay/EXECUTIVE_SUMMARY.md",
        "scorecard_href": "/reports/phase3ay/category_readiness.md",
    }


def paper_trade_blocker_status(*, crypto_freshness: dict[str, Any]) -> dict[str, Any]:
    def count(key: str) -> int:
        try:
            return max(0, int(crypto_freshness.get(key) or 0))
        except (TypeError, ValueError):
            return 0

    paper_ready = count("paper_ready_candidates")
    positive_ev = count("positive_ev_rows")
    no_book = count("positive_ev_no_executable_book_rows")
    clean_book = count("positive_ev_clean_book_rows")
    risk_missing = count("positive_ev_clean_book_risk_missing_rows")
    status = str(crypto_freshness.get("status") or "UNKNOWN")
    runner_status = str(crypto_freshness.get("runner_status") or "UNKNOWN")
    actionability_gap = str(crypto_freshness.get("actionability_gap") or "UNKNOWN")
    actionability_note = str(
        crypto_freshness.get("actionability_note")
        or "Paper-readiness evidence is unavailable."
    )
    runner_next_action = str(
        crypto_freshness.get("runner_next_action")
        or "Keep the watcher running and wait for the next freshness cycle."
    )
    phase3aw_truth = _phase3aw_dashboard_truth_payload()
    if _phase3aw_truth_matches_current_r5(phase3aw_truth, crypto_freshness):
        return _paper_trade_blocker_status_from_phase3aw(phase3aw_truth)
    if _crypto_freshness_has_current_truth(crypto_freshness):
        return _paper_trade_blocker_status_from_crypto_truth(
            crypto_freshness=crypto_freshness,
            stale_artifacts_ignored=0,
        )

    if paper_ready > 0:
        status_kind = "good"
        status_label = "Paper ready"
        summary = (
            f"{paper_ready} paper-ready candidate(s) exist. Keep the UI read-only "
            "and inspect risk gates before any execution discussion."
        )
    elif status == "RUNNING_CYCLE_OVERDUE":
        status_kind = "warn"
        status_label = "Refresh running / cycle overdue"
        summary = actionability_note
    elif status in {"WATCHER_STOPPED", "RUNNER_STALLED"}:
        status_kind = "warn"
        status_label = "Watch needs attention"
        summary = actionability_note
    elif positive_ev > 0:
        status_kind = "warn"
        status_label = "Blocked"
        summary = actionability_note
    else:
        status_kind = "neutral"
        status_label = "No trade"
        summary = actionability_note

    best_ticker = str(crypto_freshness.get("best_ev_candidate_ticker") or "n/a")
    best_ev = str(crypto_freshness.get("best_current_expected_value_label") or "n/a")
    age_label = str(crypto_freshness.get("age_label") or "n/a")
    watch_state = str(crypto_freshness.get("watch_state") or "UNKNOWN")
    watch_state_label = str(
        crypto_freshness.get("watch_state_label") or _format_enum_label(watch_state)
    )
    runner_status_label = str(
        crypto_freshness.get("runner_status_label") or _format_enum_label(runner_status)
    )

    readiness_kind = "healthy" if paper_ready > 0 else "no_trade"
    if actionability_gap in {"RISK_MISSING", "POSITIVE_EV_NO_EXECUTABLE_BOOK"}:
        readiness_kind = "blocked"
    elif actionability_gap in {"SNAPSHOT_STALE", "FORECAST_STALE"}:
        readiness_kind = "stale"

    runner_kind = (
        "healthy" if bool(crypto_freshness.get("runner_running")) else "unknown"
    )
    if status == "RUNNING_CYCLE_OVERDUE":
        runner_kind = "warn"
    elif status in {"WATCHER_STOPPED", "RUNNER_STALLED"}:
        runner_kind = "stale"

    blockers = [
        {
            "area": "Crypto paper readiness",
            "source": "Phase 3BC-R5 / Phase 3AK watch",
            "status": _format_enum_label(actionability_gap),
            "status_kind": readiness_kind,
            "status_label": _format_enum_label(actionability_gap),
            "evidence": (
                f"{paper_ready} paper-ready, {positive_ev} positive-EV, "
                f"{no_book} no-book, {clean_book} clean-book, "
                f"{risk_missing} risk-missing row(s). Best watched row: "
                f"{best_ticker} at {best_ev}."
            ),
            "next_action": (
                "Inspect paper-only risk preflight before any execution toggle."
                if paper_ready > 0
                else actionability_note
            ),
        },
        {
            "area": "Watcher freshness",
            "source": "Crypto freshness watch",
            "status": runner_status_label,
            "status_kind": runner_kind,
            "status_label": runner_status_label,
            "evidence": (
                f"Runner {runner_status_label}; watch state {watch_state_label}; "
                f"last report {age_label}."
            ),
            "next_action": runner_next_action,
        },
    ]
    phase3an_payload = _phase3an_dashboard_status_payload()
    phase3an_summary = (
        phase3an_payload.get("summary")
        if isinstance(phase3an_payload.get("summary"), dict)
        else {}
    )
    if phase3an_summary:
        _extend_phase3an_blockers(blockers, phase3an_summary)
        phase3an_crypto = (
            phase3an_summary.get("crypto_watch")
            if isinstance(phase3an_summary.get("crypto_watch"), dict)
            else {}
        )
        if phase3an_crypto.get("status"):
            status_label = _format_enum_label(str(phase3an_crypto["status"]))
            summary = str(
                phase3an_crypto.get("next_action")
                or summary
            )

    phase3bc_r3_payload = _phase3bc_r3_dashboard_status_payload()
    phase3ar_payload = _phase3ar_dashboard_status_payload()
    phase3ar_summary = (
        phase3ar_payload.get("gate_summary")
        if isinstance(phase3ar_payload.get("gate_summary"), dict)
        else {}
    )
    phase3aq_payload = _phase3aq_dashboard_status_payload()
    phase3aq_summary = (
        phase3aq_payload.get("gate_summary")
        if isinstance(phase3aq_payload.get("gate_summary"), dict)
        else {}
    )
    phase3ap_payload = _phase3ap_dashboard_status_payload()
    phase3ap_summary = (
        phase3ap_payload.get("gate_summary")
        if isinstance(phase3ap_payload.get("gate_summary"), dict)
        else {}
    )
    if phase3ar_summary:
        _extend_phase3ar_blockers(blockers, phase3ar_payload)
        phase3ar_ready = _safe_int(phase3ar_summary.get("paper_ready_rows"))
        phase3ar_positive = _safe_int(phase3ar_summary.get("positive_ev_rows"))
        phase3ar_expired = _safe_int(phase3ar_summary.get("expired_positive_ev_rows"))
        phase3ar_first_blocker = str(
            phase3ar_summary.get("first_hard_blocker") or ""
        )
        top_phase3ar_reason = (
            phase3ar_first_blocker
            or _top_count_key(phase3ar_summary.get("primary_blocker_counts"))
            or _top_count_key(phase3ar_summary.get("url_status_counts"))
        )
        if phase3ar_ready > 0:
            status_kind = "good"
            status_label = "Paper ready"
            summary = (
                f"Phase 3AR found {phase3ar_ready} paper-ready row(s) after "
                "URL repair and executable-book checks."
            )
        elif phase3ar_positive > 0:
            status_kind = "warn"
            status_label = _format_enum_label(top_phase3ar_reason or "BLOCKED")
            verified = _safe_int(phase3ar_summary.get("verified_tradeable_links"))
            summary = (
                f"Phase 3AR classified {phase3ar_positive} positive-EV row(s); "
                f"{verified} verified link(s); top blocker "
                f"{top_phase3ar_reason or 'UNKNOWN'}."
            )
        elif phase3ar_expired > 0:
            status_kind = "neutral"
            status_label = "EXPIRED_WINDOW_EXCLUDED"
            summary = (
                f"Phase 3AR excluded {phase3ar_expired} expired positive-EV row(s); "
                "no current positive-EV rows remain."
            )
        elif phase3ar_first_blocker == "NO_CURRENT_POSITIVE_EV":
            status_kind = "neutral"
            status_label = "NO_CURRENT_POSITIVE_EV"
            summary = "Phase 3AR found no current positive-EV rows."
    elif phase3aq_summary:
        _extend_phase3aq_blockers(blockers, phase3aq_payload)
        phase3aq_ready = _safe_int(phase3aq_summary.get("paper_ready_rows"))
        phase3aq_positive = _safe_int(phase3aq_summary.get("positive_ev_rows"))
        top_phase3aq_status = _top_count_key(phase3aq_summary.get("url_status_counts"))
        if phase3aq_ready > 0:
            status_kind = "good"
            status_label = "Paper ready"
            summary = (
                f"Phase 3AQ found {phase3aq_ready} paper-ready row(s) after "
                "verified-link and executable-book checks."
            )
        elif phase3aq_positive > 0:
            status_kind = "warn"
            status_label = _format_enum_label(top_phase3aq_status or "BLOCKED")
            summary = (
                f"Phase 3AQ classified {phase3aq_positive} positive-EV row(s); "
                f"top URL status {top_phase3aq_status or 'UNKNOWN'}."
            )
        elif _safe_int(phase3aq_summary.get("expired_positive_ev_rows")) > 0:
            status_kind = "neutral"
            status_label = "EXPIRED_WINDOW_EXCLUDED"
            summary = (
                "Phase 3AQ excluded expired positive-EV row(s); "
                "no current positive-EV rows remain."
            )
    elif phase3ap_summary:
        _extend_phase3ap_blockers(blockers, phase3ap_payload)
        phase3ap_ready = _safe_int(phase3ap_summary.get("paper_ready_rows"))
        phase3ap_positive = _safe_int(phase3ap_summary.get("positive_ev_rows"))
        phase3ap_no_book = _safe_int(
            phase3ap_summary.get("positive_ev_no_executable_book_rows")
        )
        top_phase3ap_reason = _top_count_key(phase3ap_summary.get("reason_counts"))
        if phase3ap_ready > 0:
            status_kind = "good"
            status_label = "Paper ready"
            summary = (
                f"Phase 3AP found {phase3ap_ready} paper-ready row(s) after "
                "the canonical gate."
            )
        elif phase3ap_positive > 0:
            status_kind = "warn"
            status_label = _format_enum_label(top_phase3ap_reason or "BLOCKED")
            summary = (
                f"Phase 3AP found {phase3ap_positive} positive-EV row(s), "
                f"{phase3ap_no_book} without executable books; top blocker "
                f"{top_phase3ap_reason or 'UNKNOWN'}."
            )
        elif _safe_int(phase3ap_summary.get("expired_positive_ev_rows")) > 0:
            status_kind = "neutral"
            status_label = "EXPIRED_WINDOW_EXCLUDED"
            summary = (
                "Phase 3AP excluded expired positive-EV row(s); "
                "no current positive-EV rows remain."
            )

    phase3bc_r3_rate_limit = _phase3bc_r3_rate_limit(phase3bc_r3_payload)
    if phase3bc_r3_rate_limit:
        _extend_phase3bc_r3_rate_limit_blockers(blockers, phase3bc_r3_payload)
        status_kind = "warn"
        status_label = "RATE_LIMITED_KALSHI_API"
        summary = (
            "Kalshi public API rate limiting left market/catalog/book data partial; "
            "paper-ready is blocked until a complete bounded refresh succeeds."
        )

    metric_paper_ready = paper_ready
    metric_positive_ev = positive_ev
    metric_no_book = no_book
    if phase3ar_summary:
        metric_paper_ready = _safe_int(phase3ar_summary.get("paper_ready_rows"))
        metric_positive_ev = _safe_int(phase3ar_summary.get("positive_ev_rows"))
        metric_no_book = _safe_int(
            phase3ar_summary.get("positive_ev_no_executable_book_rows")
        )
    elif phase3aq_summary:
        metric_paper_ready = _safe_int(phase3aq_summary.get("paper_ready_rows"))
        metric_positive_ev = _safe_int(phase3aq_summary.get("positive_ev_rows"))
        metric_no_book = _safe_int(
            phase3aq_summary.get("positive_ev_no_executable_book_rows")
        )
    elif phase3ap_summary:
        metric_paper_ready = _safe_int(phase3ap_summary.get("paper_ready_rows"))
        metric_positive_ev = _safe_int(phase3ap_summary.get("positive_ev_rows"))
        metric_no_book = _safe_int(
            phase3ap_summary.get("positive_ev_no_executable_book_rows")
        )
    if phase3bc_r3_rate_limit:
        metric_paper_ready = 0

    metrics = [
        {"label": "Paper-ready", "value": metric_paper_ready},
        {"label": "Positive EV", "value": metric_positive_ev},
        {"label": "No executable book", "value": metric_no_book},
        {"label": "Risk missing", "value": risk_missing},
    ]
    expired_metric = 0
    if phase3ar_summary:
        expired_metric = _safe_int(phase3ar_summary.get("expired_positive_ev_rows"))
    elif phase3aq_summary:
        expired_metric = _safe_int(phase3aq_summary.get("expired_positive_ev_rows"))
    elif phase3ap_summary:
        expired_metric = _safe_int(phase3ap_summary.get("expired_positive_ev_rows"))
    if expired_metric:
        metrics.append({"label": "Expired positive EV", "value": expired_metric})
    if phase3ar_summary:
        metrics.append(
            {
                "label": "Verified links",
                "value": _safe_int(phase3ar_summary.get("verified_tradeable_links")),
            }
        )
    elif phase3aq_summary:
        metrics.append(
            {
                "label": "Verified links",
                "value": _safe_int(phase3aq_summary.get("verified_tradeable_links")),
            }
        )

    report_links = [
        {
            "label": "Watch report",
            "href": str(crypto_freshness.get("report_href") or "#"),
        },
        {
            "label": (
                "Phase 3AR"
                if phase3ar_summary
                else "Phase 3AQ"
                if phase3aq_summary
                else "Phase 3AP"
            ),
            "href": (
                PHASE3AR_EXECUTIVE_SUMMARY_HREF
                if phase3ar_summary
                else PHASE3AQ_EXECUTIVE_SUMMARY_HREF
                if phase3aq_summary
                else PHASE3AP_EXECUTIVE_SUMMARY_HREF
            ),
        },
        {
            "label": "Gate JSON",
            "href": (
                PHASE3AR_GATE_HREF
                if phase3ar_summary
                else PHASE3AQ_GATE_HREF
                if phase3aq_summary
                else PHASE3AP_GATE_HREF
            ),
        },
        {"label": "Learning", "href": REPORT_LINKS.learning_report},
        {"label": "Diagnostics", "href": REPORT_LINKS.learning_diagnostics},
        {"label": "Targets", "href": REPORT_LINKS.learning_targets},
        {"label": "Phase 3AN", "href": "/reports/phase3an/EXECUTIVE_SUMMARY.md"},
    ]

    return {
        "summary": summary,
        "status_kind": status_kind,
        "status_label": status_label,
        "metrics": metrics,
        "last_updated": age_label,
        "blockers": blockers,
        "positive_ev_rows": (
            _phase3ar_positive_ev_rows_for_ui(phase3ar_payload)
            if phase3ar_summary
            else _phase3aq_positive_ev_rows_for_ui(phase3aq_payload)
        ),
        "report_links": report_links,
        "phase3an_status_source": (
            str(PHASE3AN_DASHBOARD_STATUS_PATH) if phase3an_summary else None
        ),
        "phase3aq_status_source": (
            str(PHASE3AQ_PAPER_READY_GATE_PATH) if phase3aq_summary else None
        ),
        "phase3ar_status_source": (
            str(PHASE3AR_PAPER_READY_GATE_PATH) if phase3ar_summary else None
        ),
        "phase3ap_status_source": (
            str(PHASE3AP_PAPER_READY_GATE_PATH) if phase3ap_summary else None
        ),
    }


def _phase3aw_dashboard_truth_payload(
    path: Path = PHASE3AW_DASHBOARD_TRUTH_PATH,
) -> dict[str, Any]:
    return _load_json_payload(path)


def _phase3aw_truth_matches_current_r5(
    payload: dict[str, Any],
    crypto_freshness: dict[str, Any],
) -> bool:
    if not payload:
        return False
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    truth_r5_at = parse_datetime(summary.get("r5_latest_report_generated_at"))
    current_r5_at = parse_datetime(crypto_freshness.get("r5_latest_report_generated_at"))
    if truth_r5_at is not None and current_r5_at is not None:
        return truth_r5_at >= current_r5_at
    generated_at = parse_datetime(payload.get("generated_at"))
    if generated_at is None:
        return False
    return (utc_now() - generated_at).total_seconds() <= 120 * 60


def _paper_trade_blocker_status_from_phase3aw(payload: dict[str, Any]) -> dict[str, Any]:
    ui_panel = payload.get("ui_panel") if isinstance(payload.get("ui_panel"), dict) else {}
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    if not ui_panel:
        return _paper_trade_blocker_empty_truth()
    report_links = ui_panel.get("report_links")
    if not isinstance(report_links, list):
        report_links = []
    if not any(link.get("href") == PHASE3AW_EXECUTIVE_SUMMARY_HREF for link in report_links if isinstance(link, dict)):
        report_links.insert(
            0,
            {"label": "Truth report", "href": PHASE3AW_EXECUTIVE_SUMMARY_HREF},
        )
    return {
        "summary": str(ui_panel.get("summary") or ""),
        "status_kind": str(ui_panel.get("status_kind") or "neutral"),
        "status_label": str(ui_panel.get("status_label") or "Unknown"),
        "metrics": ui_panel.get("metrics") if isinstance(ui_panel.get("metrics"), list) else [],
        "last_updated": str(summary.get("r5_latest_report_generated_at") or "n/a"),
        "blockers": ui_panel.get("blockers") if isinstance(ui_panel.get("blockers"), list) else [],
        "positive_ev_rows": (
            ui_panel.get("positive_ev_rows")
            if isinstance(ui_panel.get("positive_ev_rows"), list)
            else []
        ),
        "report_links": report_links,
        "phase3aw_status_source": str(PHASE3AW_DASHBOARD_TRUTH_PATH),
    }


def _crypto_freshness_has_current_truth(crypto_freshness: dict[str, Any]) -> bool:
    primary_gap = str(crypto_freshness.get("primary_gap") or "")
    ranking_gap = _crypto_truth_ranking_gap(crypto_freshness)
    if (
        primary_gap == "EV_NOT_POSITIVE"
        and not _crypto_freshness_backlog_blocks_current_positive_ev(crypto_freshness)
        and ranking_gap == 0
    ):
        return True
    return (
        primary_gap == "EV_NOT_POSITIVE"
        and _safe_int(crypto_freshness.get("snapshot_stale_rows")) == 0
        and _safe_int(crypto_freshness.get("forecast_stale_rows")) == 0
        and ranking_gap == 0
    )


def _paper_trade_blocker_status_from_crypto_truth(
    *,
    crypto_freshness: dict[str, Any],
    stale_artifacts_ignored: int,
) -> dict[str, Any]:
    blocker = _crypto_truth_blocker(crypto_freshness)
    status_label = (
        "Waiting for Positive EV"
        if blocker == "EV_NOT_POSITIVE"
        else _format_enum_label(blocker)
    )
    status_kind = "neutral" if blocker == "EV_NOT_POSITIVE" else "warn"
    positive_ev = _safe_int(crypto_freshness.get("positive_ev_rows"))
    clean_execution = _safe_int(crypto_freshness.get("clean_execution_rows"))
    paper_ready = _safe_int(crypto_freshness.get("paper_ready_candidates"))
    best_ev = str(crypto_freshness.get("best_current_expected_value_label") or "n/a")
    gap = str(crypto_freshness.get("best_ev_gap_to_positive_label") or "n/a")
    summary = (
        "The crypto watch is running. Current snapshots, forecasts, and rankings "
        "are healthy. No current crypto market has strictly positive expected value, "
        "so no paper trade should be created."
        if blocker == "EV_NOT_POSITIVE"
        else str(crypto_freshness.get("actionability_note") or "Current crypto truth is unavailable.")
    )
    blockers = [
        {
            "area": "Current crypto truth",
            "source": "Phase 3BC-R5 current-window status",
            "status": blocker,
            "status_kind": status_kind,
            "status_label": status_label,
            "evidence": (
                f"best_current_expected_value={best_ev}, gap_to_positive={gap}, "
                f"positive_ev_rows={positive_ev}, clean_execution_rows={clean_execution}"
            ),
            "next_action": (
                "Keep R5 watch running. Do not force paper trades."
                if blocker == "EV_NOT_POSITIVE"
                else str(crypto_freshness.get("runner_next_action") or "Review R5 status.")
            ),
        },
        {
            "area": "Watcher freshness",
            "source": "Crypto freshness watch",
            "status": crypto_freshness.get("runner_status") or "UNKNOWN",
            "status_kind": "healthy" if crypto_freshness.get("runner_running") else "stale",
            "status_label": str(
                crypto_freshness.get("runner_status_label")
                or _format_enum_label(str(crypto_freshness.get("runner_status") or "UNKNOWN"))
            ),
            "evidence": (
                f"Runner {crypto_freshness.get('runner_status_label')}; "
                f"watch state {crypto_freshness.get('watch_state_label')}; "
                f"last report {crypto_freshness.get('age_label')}."
            ),
            "next_action": str(
                crypto_freshness.get("runner_next_action")
                or "Keep R5 watch running unless the guard says it is stopped."
            ),
        },
    ]
    if stale_artifacts_ignored:
        blockers.append(
            {
                "area": "Old artifacts ignored",
                "source": "Dashboard truth fallback",
                "status": "STALE_ARTIFACT",
                "status_kind": "warn",
                "status_label": "Old Artifact Ignored",
                "evidence": f"{stale_artifacts_ignored} artifact(s) ignored.",
                "next_action": (
                    "Regenerate phase3aw-dashboard-truth for a full artifact audit."
                ),
            }
        )
    return {
        "summary": summary,
        "status_kind": status_kind,
        "status_label": status_label,
        "metrics": [
            {"label": "Paper-ready", "value": paper_ready},
            {"label": "Positive EV", "value": positive_ev},
            {"label": "Clean execution rows", "value": clean_execution},
            {"label": "Best EV", "value": best_ev},
            {"label": "Gap to positive", "value": gap},
            {
                "label": "R5 status",
                "value": crypto_freshness.get("runner_status_label") or "Unknown",
            },
            {"label": "Stale artifacts ignored", "value": stale_artifacts_ignored},
        ],
        "last_updated": str(
            crypto_freshness.get("r5_latest_report_generated_at")
            or crypto_freshness.get("generated_at")
            or "n/a"
        ),
        "blockers": blockers,
        "positive_ev_rows": [],
        "report_links": [
            {"label": "Watch report", "href": str(crypto_freshness.get("report_href") or "#")},
            {"label": "Truth report", "href": PHASE3AW_EXECUTIVE_SUMMARY_HREF},
            {"label": "R5 status", "href": "/reports/phase3bc_r5/phase3bc_r5_status.json"},
        ],
    }


def _crypto_truth_blocker(crypto_freshness: dict[str, Any]) -> str:
    if (
        str(crypto_freshness.get("primary_gap") or "") == "EV_NOT_POSITIVE"
        and not _crypto_freshness_backlog_blocks_current_positive_ev(crypto_freshness)
    ):
        return "EV_NOT_POSITIVE"
    if _safe_int(crypto_freshness.get("snapshot_stale_rows")) > 0:
        return "SNAPSHOT_STALE"
    if _safe_int(crypto_freshness.get("forecast_stale_rows")) > 0:
        return "FORECAST_STALE"
    if _crypto_truth_ranking_gap(crypto_freshness) > 0:
        return "RANKING_GAP"
    if str(crypto_freshness.get("primary_gap") or "") == "EV_NOT_POSITIVE":
        return "EV_NOT_POSITIVE"
    if _safe_int(crypto_freshness.get("positive_ev_rows")) <= 0:
        return "EV_NOT_POSITIVE"
    return str(crypto_freshness.get("actionability_gap") or "UNKNOWN")


def _crypto_freshness_backlog_blocks_current_positive_ev(
    crypto_freshness: dict[str, Any],
) -> bool:
    if bool(crypto_freshness.get("freshness_backlog_blocks_current_positive_ev")):
        return True
    return (
        _safe_int(crypto_freshness.get("positive_ev_snapshot_stale_rows")) > 0
        or _safe_int(crypto_freshness.get("positive_ev_forecast_stale_rows")) > 0
    )


def _crypto_truth_ranking_gap(crypto_freshness: dict[str, Any]) -> int:
    if crypto_freshness.get("ranking_coverage_gap_after_repair") is not None:
        return _safe_int(crypto_freshness.get("ranking_coverage_gap_after_repair"))
    if crypto_freshness.get("true_ranking_gap_after_repair") is not None:
        return _safe_int(crypto_freshness.get("true_ranking_gap_after_repair"))
    return _safe_int(crypto_freshness.get("ranking_missing_rows")) + _safe_int(
        crypto_freshness.get("ranking_stale_rows")
    )


def _paper_trade_blocker_empty_truth() -> dict[str, Any]:
    return {
        "summary": "Dashboard truth is unavailable.",
        "status_kind": "warn",
        "status_label": "Unknown",
        "metrics": [],
        "last_updated": "n/a",
        "blockers": [],
        "positive_ev_rows": [],
        "report_links": [{"label": "Truth report", "href": PHASE3AW_EXECUTIVE_SUMMARY_HREF}],
    }


def _phase3ap_dashboard_status_payload(
    *,
    gate_path: Path = PHASE3AP_PAPER_READY_GATE_PATH,
    book_path: Path = PHASE3AP_BOOK_DIAGNOSTIC_PATH,
) -> dict[str, Any]:
    gate = _load_json_payload(gate_path)
    book = _load_json_payload(book_path)
    if not gate and not book:
        return {}
    return {
        "gate": gate,
        "book": book,
        "gate_summary": gate.get("summary") if isinstance(gate.get("summary"), dict) else {},
        "book_summary": book.get("summary") if isinstance(book.get("summary"), dict) else {},
        "gate_rows": gate.get("rows") if isinstance(gate.get("rows"), list) else [],
        "positive_ev_rows": (
            book.get("positive_ev_rows")
            if isinstance(book.get("positive_ev_rows"), list)
            else []
        ),
        "generated_at": gate.get("generated_at") or book.get("generated_at"),
    }


def _phase3aq_dashboard_status_payload(
    *,
    gate_path: Path = PHASE3AQ_PAPER_READY_GATE_PATH,
    audit_path: Path = PHASE3AQ_LINK_AUDIT_PATH,
) -> dict[str, Any]:
    gate = _load_json_payload(gate_path)
    audit = _load_json_payload(audit_path)
    if not gate and not audit:
        return {}
    gate_summary = gate.get("summary") if isinstance(gate.get("summary"), dict) else {}
    audit_summary = audit.get("summary") if isinstance(audit.get("summary"), dict) else {}
    rows = (
        gate.get("positive_ev_rows")
        if isinstance(gate.get("positive_ev_rows"), list)
        else audit.get("positive_ev_rows")
        if isinstance(audit.get("positive_ev_rows"), list)
        else []
    )
    return {
        "gate": gate,
        "audit": audit,
        "gate_summary": gate_summary or audit_summary,
        "positive_ev_rows": rows,
        "generated_at": gate.get("generated_at") or audit.get("generated_at"),
    }


def _phase3ar_dashboard_status_payload(
    *,
    gate_path: Path = PHASE3AR_PAPER_READY_GATE_PATH,
    audit_path: Path = PHASE3AR_URL_AUDIT_PATH,
    catalog_refresh_path: Path = PHASE3AR_CATALOG_REFRESH_PATH,
) -> dict[str, Any]:
    gate = _load_json_payload(gate_path)
    audit = _load_json_payload(audit_path)
    catalog_refresh = _load_json_payload(catalog_refresh_path)
    if not gate and not audit and not catalog_refresh:
        return {}
    gate_summary = gate.get("summary") if isinstance(gate.get("summary"), dict) else {}
    audit_summary = audit.get("summary") if isinstance(audit.get("summary"), dict) else {}
    gate_rows = gate.get("positive_ev_rows") if isinstance(gate.get("positive_ev_rows"), list) else []
    audit_rows = audit.get("rows") if isinstance(audit.get("rows"), list) else []
    audit_by_ticker = {
        str(row.get("market_ticker") or row.get("ticker")): row
        for row in audit_rows
        if isinstance(row, dict)
    }
    merged_rows: list[dict[str, Any]] = []
    for row in gate_rows:
        if not isinstance(row, dict):
            continue
        ticker = str(row.get("market_ticker") or row.get("ticker") or "")
        merged = dict(row)
        merged.update(
            {
                f"phase3ar_{key}": value
                for key, value in audit_by_ticker.get(ticker, {}).items()
            }
        )
        audit_row = audit_by_ticker.get(ticker, {})
        for key in (
            "specific_malformed_reason",
            "previous_malformed_reason",
            "current_url_status",
            "current_stored_kalshi_url",
            "safe_to_persist",
            "repair_command_required",
            "proposed_official_url",
        ):
            if key in audit_row:
                merged[key] = audit_row[key]
        merged_rows.append(merged)
    if not merged_rows:
        merged_rows = [row for row in audit_rows if isinstance(row, dict)]
    summary = dict(gate_summary or {})
    if audit_summary:
        summary.setdefault("current_verified_links", audit_summary.get("current_verified_links", 0))
        summary.setdefault("current_malformed_urls", audit_summary.get("current_malformed_urls", 0))
        summary.setdefault("safe_to_persist", audit_summary.get("safe_to_persist", 0))
        summary.setdefault("specific_malformed_reason_counts", audit_summary.get("specific_malformed_reason_counts", {}))
    return {
        "gate": gate,
        "audit": audit,
        "catalog_refresh": catalog_refresh,
        "gate_summary": summary or audit_summary,
        "positive_ev_rows": merged_rows,
        "generated_at": gate.get("generated_at") or audit.get("generated_at") or catalog_refresh.get("generated_at"),
    }


def _load_json_payload(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _phase3an_dashboard_status_payload(
    path: Path = PHASE3AN_DASHBOARD_STATUS_PATH,
) -> dict[str, Any]:
    return _load_json_payload(path)


def _phase3bc_r3_dashboard_status_payload(
    path: Path = PHASE3BC_R3_REFRESH_PATH,
) -> dict[str, Any]:
    return _load_json_payload(path)


def _phase3bc_r3_rate_limit(payload: dict[str, Any]) -> dict[str, Any]:
    rate_limit = payload.get("rate_limit") if isinstance(payload, dict) else {}
    summary = payload.get("summary") if isinstance(payload, dict) else {}
    if not isinstance(rate_limit, dict):
        rate_limit = {}
    if not isinstance(summary, dict):
        summary = {}
    status_values = {
        str(rate_limit.get("blocker") or ""),
        str(rate_limit.get("status") or ""),
        str(summary.get("kalshi_api_status") or ""),
    }
    if "RATE_LIMITED_KALSHI_API" in status_values:
        return rate_limit
    if any(value.startswith("RATE_LIMITED_") for value in status_values):
        return rate_limit
    return {}


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _top_count_key(value: Any) -> str | None:
    if not isinstance(value, dict) or not value:
        return None
    return max(value.items(), key=lambda item: _safe_int(item[1]))[0]


def _extend_phase3bc_r3_rate_limit_blockers(
    blockers: list[dict[str, Any]],
    payload: dict[str, Any],
) -> None:
    rate_limit = _phase3bc_r3_rate_limit(payload)
    endpoints = rate_limit.get("endpoints") if isinstance(rate_limit, dict) else []
    top_endpoint = str(rate_limit.get("top_endpoint") or "")
    if not top_endpoint and isinstance(endpoints, list) and endpoints:
        first = endpoints[0]
        if isinstance(first, dict):
            top_endpoint = str(first.get("endpoint") or "")
    status = str(rate_limit.get("status") or "RATE_LIMITED_PARTIAL")
    evidence = (
        f"{status}; endpoint {top_endpoint or 'Kalshi public API'}; "
        f"retries {rate_limit.get('retry_count', 0)}; "
        f"slept {rate_limit.get('total_sleep_seconds', 0)}s; "
        f"rows before limit {rate_limit.get('rows_fetched_before_limit', 0)}; "
        f"data {rate_limit.get('data_completeness', 'partial')}."
    )
    blockers.insert(
        0,
        {
            "area": "Kalshi API rate limit",
            "source": "Phase 3BC-R3 active crypto refresh",
            "status": "RATE_LIMITED_KALSHI_API",
            "status_kind": "blocked",
            "status_label": "RATE_LIMITED_KALSHI_API",
            "evidence": evidence,
            "next_action": str(
                payload.get("recommended_next_action")
                or "Wait for Kalshi backoff and rerun a bounded refresh before paper-ready."
            ),
        },
    )


def _extend_phase3aq_blockers(
    blockers: list[dict[str, Any]],
    payload: dict[str, Any],
) -> None:
    summary = (
        payload.get("gate_summary")
        if isinstance(payload.get("gate_summary"), dict)
        else {}
    )
    top_status = _top_count_key(summary.get("url_status_counts")) or "UNKNOWN_REQUIRES_INVESTIGATION"
    sample_rows = [
        row for row in payload.get("positive_ev_rows", [])
        if isinstance(row, dict)
    ][:3]
    samples = ", ".join(
        str(row.get("market_ticker") or row.get("ticker") or "unknown")
        for row in sample_rows
    ) or "none"
    url_counts = summary.get("url_status_counts") if isinstance(summary.get("url_status_counts"), dict) else {}
    top_counts = ", ".join(
        f"{_format_enum_label(str(key))}: {_safe_int(value)}"
        for key, value in sorted(
            url_counts.items(),
            key=lambda item: (-_safe_int(item[1]), str(item[0])),
        )[:4]
    ) or "none"
    blockers.insert(
        0,
        {
            "area": "Verified Kalshi link gate",
            "source": "Phase 3AQ link and book unblock report",
            "status": _format_enum_label(top_status),
            "status_kind": (
                "healthy"
                if _safe_int(summary.get("paper_ready_rows")) > 0
                else "blocked"
            ),
            "status_label": _format_enum_label(top_status),
            "evidence": (
                f"{summary.get('positive_ev_rows', 0)} positive-EV row(s); "
                f"URL statuses: {top_counts}; "
                f"catalog matches {summary.get('catalog_match_exists_rows', 0)}; "
                f"verified links {summary.get('verified_tradeable_links', 0)}; "
                f"book refresh candidates {summary.get('book_refresh_needed_rows', 0)}. "
                f"Examples: {samples}."
            ),
            "next_action": str(
                payload.get("gate", {}).get("next_action")
                or payload.get("audit", {}).get("next_action")
                or "Repair exact Kalshi URL/catalog evidence before refreshing books."
            ),
        },
    )


def _extend_phase3ar_blockers(
    blockers: list[dict[str, Any]],
    payload: dict[str, Any],
) -> None:
    catalog_refresh = (
        payload.get("catalog_refresh")
        if isinstance(payload.get("catalog_refresh"), dict)
        else {}
    )
    _extend_phase3ar_exact_catalog_blockers(blockers, catalog_refresh)
    summary = (
        payload.get("gate_summary")
        if isinstance(payload.get("gate_summary"), dict)
        else {}
    )
    current_positive = _safe_int(summary.get("positive_ev_rows"))
    expired_positive = _safe_int(summary.get("expired_positive_ev_rows"))
    first_hard_blocker = str(summary.get("first_hard_blocker") or "")
    top_reason = (
        (
            "EXPIRED_WINDOW_EXCLUDED"
            if current_positive == 0 and expired_positive > 0
            else None
        )
        or first_hard_blocker
        or (
            "NO_CURRENT_POSITIVE_EV"
            if current_positive == 0
            else None
        )
        or _top_count_key(summary.get("primary_blocker_counts"))
        or _top_count_key(summary.get("url_status_counts"))
        or "UNKNOWN_REQUIRES_INVESTIGATION"
    )
    sample_rows = [
        row for row in payload.get("positive_ev_rows", [])
        if isinstance(row, dict)
    ][:3]
    samples = ", ".join(
        str(row.get("market_ticker") or row.get("ticker") or "unknown")
        for row in sample_rows
    ) or "none"
    blockers.insert(
        0,
        {
            "area": "Kalshi URL repair gate",
            "source": "Phase 3AR link repair report",
            "status": top_reason,
            "status_kind": (
                "healthy"
                if _safe_int(summary.get("paper_ready_rows")) > 0
                else "blocked"
            ),
            "status_label": _format_enum_label(top_reason),
            "evidence": (
                f"{summary.get('positive_ev_rows', 0)} current positive-EV row(s); "
                f"{summary.get('expired_positive_ev_rows', 0)} expired positive-EV row(s); "
                f"verified links {summary.get('verified_tradeable_links', summary.get('current_verified_links', 0))}; "
                f"malformed current URLs {summary.get('current_malformed_urls', 0)}; "
                f"safe repairs {summary.get('safe_to_persist', 0)}; "
                f"book refresh candidates {summary.get('book_refresh_needed_rows', 0)}. "
                f"Examples: {samples}."
            ),
            "next_action": str(
                payload.get("gate", {}).get("next_action")
                or payload.get("audit", {}).get("next_action")
                or "Run the next Phase 3AR link repair command."
            ),
        },
    )


def _extend_phase3ar_exact_catalog_blockers(
    blockers: list[dict[str, Any]],
    catalog_refresh: dict[str, Any],
) -> None:
    if not catalog_refresh:
        return
    views = (
        catalog_refresh.get("freshness_views")
        if isinstance(catalog_refresh.get("freshness_views"), dict)
        else {}
    )
    exact = (
        views.get("exact_opportunity_catalog")
        if isinstance(views.get("exact_opportunity_catalog"), dict)
        else {}
    )
    if not exact:
        return
    status = str(exact.get("status") or catalog_refresh.get("status") or "UNKNOWN")
    data_complete = bool(exact.get("data_complete"))
    if data_complete and status in {"COMPLETE", "NO_POSITIVE_EV_ROWS"}:
        return
    rate_limit = (
        catalog_refresh.get("rate_limit")
        if isinstance(catalog_refresh.get("rate_limit"), dict)
        else {}
    )
    if status.startswith("RATE_LIMITED_") or rate_limit.get("rate_limited"):
        status_label = "RATE_LIMITED_KALSHI_API"
    else:
        status_label = status
    blockers.insert(
        0,
        {
            "area": "Exact opportunity catalog freshness",
            "source": "Phase 3AR catalog refresh handoff",
            "status": status_label,
            "status_kind": "blocked",
            "status_label": _format_enum_label(status_label),
            "evidence": (
                f"{exact.get('fresh_rows', 0)} exact fresh row(s) of "
                f"{exact.get('rows_checked', 0)} checked; "
                f"{exact.get('not_refreshed_rows', 0)} not refreshed; "
                f"data {catalog_refresh.get('summary', {}).get('data_completeness', 'partial')}."
            ),
            "next_action": str(
                catalog_refresh.get("next_action")
                or "Rerun the bounded exact Phase 3AR catalog refresh before URL verification."
            ),
        },
    )


def _phase3ar_positive_ev_rows_for_ui(payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows = payload.get("positive_ev_rows") if isinstance(payload, dict) else []
    if not isinstance(rows, list):
        return []
    formatted: list[dict[str, Any]] = []
    for row in rows[:8]:
        if not isinstance(row, dict):
            continue
        url_status = str(
            row.get("current_url_status")
            or row.get("url_status")
            or row.get("kalshi_url_status")
            or "UNKNOWN"
        )
        book_status = str(row.get("book_status") or row.get("legacy_no_book_reason") or "UNKNOWN")
        primary = str(row.get("primary_blocker") or "UNKNOWN")
        malformed_reason = str(
            row.get("specific_malformed_reason")
            or row.get("previous_malformed_reason")
            or ""
        )
        formatted.append(
            {
                "market_ticker": str(row.get("market_ticker") or row.get("ticker") or "unknown"),
                "market_title": str(row.get("market_title") or row.get("catalog_market_title") or "Untitled market"),
                "forecast_model": str(row.get("forecast_model") or "model"),
                "raw_ev": str(row.get("raw_ev_cents") or row.get("raw_ev") or row.get("current_raw_ev") or "n/a"),
                "quote_age": str(row.get("quote_age_minutes") or "n/a"),
                "url_status": url_status,
                "url_status_label": _format_enum_label(url_status),
                "url_status_kind": "healthy" if url_status == "VERIFIED" else "blocked",
                "book_status": book_status,
                "book_status_label": _format_enum_label(book_status),
                "primary_blocker": primary,
                "primary_blocker_label": _format_enum_label(primary),
                "catalog_match_exists": bool(row.get("catalog_match_exists", row.get("canonical_catalog_match"))),
                "url_exists": bool(row.get("url_exists") or row.get("current_stored_kalshi_url") or row.get("kalshi_url")),
                "stored_official_url": bool(row.get("current_stored_kalshi_url") or row.get("kalshi_url")),
                "book_refresh_needed": bool(row.get("book_refresh_needed")),
                "kalshi_url": row.get("kalshi_url") or row.get("current_stored_kalshi_url"),
                "kalshi_url_verified": bool(row.get("kalshi_url_verified")) or url_status == "VERIFIED",
                "malformed_reason": _format_enum_label(malformed_reason) if malformed_reason else "",
                "repair_command_required": str(row.get("repair_command_required") or ""),
                "next_action": str(row.get("next_action") or ""),
            }
        )
    return formatted


def _phase3aq_positive_ev_rows_for_ui(payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows = payload.get("positive_ev_rows") if isinstance(payload, dict) else []
    if not isinstance(rows, list):
        return []
    formatted: list[dict[str, Any]] = []
    for row in rows[:8]:
        if not isinstance(row, dict):
            continue
        url_status = str(row.get("url_status") or row.get("kalshi_url_status") or "UNKNOWN")
        book_status = str(row.get("book_status") or row.get("legacy_no_book_reason") or "UNKNOWN")
        primary = str(row.get("primary_blocker") or "UNKNOWN")
        formatted.append(
            {
                "market_ticker": str(row.get("market_ticker") or row.get("ticker") or "unknown"),
                "market_title": str(row.get("market_title") or "Untitled market"),
                "forecast_model": str(row.get("forecast_model") or "model"),
                "raw_ev": str(row.get("raw_ev_cents") or row.get("raw_ev") or "n/a"),
                "quote_age": str(row.get("quote_age_minutes") or "n/a"),
                "url_status": url_status,
                "url_status_label": _format_enum_label(url_status),
                "url_status_kind": "healthy" if url_status == "VERIFIED" else "blocked",
                "book_status": book_status,
                "book_status_label": _format_enum_label(book_status),
                "primary_blocker": primary,
                "primary_blocker_label": _format_enum_label(primary),
                "catalog_match_exists": bool(row.get("catalog_match_exists")),
                "url_exists": bool(row.get("url_exists")),
                "book_refresh_needed": bool(row.get("book_refresh_needed")),
                "kalshi_url": row.get("kalshi_url"),
                "kalshi_url_verified": bool(row.get("kalshi_url_verified")),
                "next_action": str(row.get("next_action") or ""),
            }
        )
    return formatted


def _extend_phase3ap_blockers(
    blockers: list[dict[str, Any]],
    payload: dict[str, Any],
) -> None:
    gate_summary = (
        payload.get("gate_summary")
        if isinstance(payload.get("gate_summary"), dict)
        else {}
    )
    book_summary = (
        payload.get("book_summary")
        if isinstance(payload.get("book_summary"), dict)
        else {}
    )
    top_reason = _top_count_key(gate_summary.get("reason_counts")) or "UNKNOWN_REQUIRES_INVESTIGATION"
    top_book_reason = (
        _top_count_key(book_summary.get("no_book_reason_counts"))
        or "UNKNOWN_REQUIRES_INVESTIGATION"
    )
    sample_rows = [
        row for row in payload.get("gate_rows", [])
        if isinstance(row, dict)
        and (row.get("paper_ready_blocker") or row.get("primary_blocker")) != "PAPER_READY"
    ][:3]
    samples = ", ".join(
        str(row.get("market_ticker") or row.get("ticker") or "unknown")
        for row in sample_rows
    ) or "none"
    blockers.insert(
        0,
        {
            "area": "Canonical paper-ready gate",
            "source": "Phase 3AP unblock report",
            "status": _format_enum_label(top_reason),
            "status_kind": (
                "healthy"
                if _safe_int(gate_summary.get("paper_ready_rows")) > 0
                else "blocked"
            ),
            "status_label": _format_enum_label(top_reason),
            "evidence": (
                f"{gate_summary.get('paper_ready_rows', 0)} paper-ready; "
                f"{gate_summary.get('positive_ev_rows', 0)} positive-EV; "
                f"{gate_summary.get('positive_ev_no_executable_book_rows', 0)} "
                f"positive-EV/no-book. Examples: {samples}."
            ),
            "next_action": (
                "Open the Phase 3AP report; do not lower thresholds. "
                f"Leading book reason: {top_book_reason}."
            ),
        },
    )


def _extend_phase3an_blockers(
    blockers: list[dict[str, Any]],
    summary: dict[str, Any],
) -> None:
    paper = summary.get("paper_funnel") if isinstance(summary.get("paper_funnel"), dict) else {}
    settlement = summary.get("settlement") if isinstance(summary.get("settlement"), dict) else {}
    sources = summary.get("general_sources") if isinstance(summary.get("general_sources"), dict) else {}
    phase3bb = summary.get("phase3bb_r2") if isinstance(summary.get("phase3bb_r2"), dict) else {}
    sports = summary.get("sports") if isinstance(summary.get("sports"), dict) else {}
    economic = summary.get("economic_news") if isinstance(summary.get("economic_news"), dict) else {}
    if paper:
        top_reason = paper.get("top_reason")
        blockers.append(
            {
                "area": "Paper funnel",
                "source": "Phase 3AN paper funnel explain",
                "status": _format_enum_label(str(paper.get("first_hard_blocker") or "UNKNOWN")),
                "status_kind": "blocked" if int(paper.get("tradeable_rows") or 0) == 0 else "healthy",
                "status_label": _format_enum_label(str(paper.get("first_hard_blocker") or "UNKNOWN")),
                "evidence": (
                    f"{paper.get('tradeable_rows', 0)} tradeable row(s); "
                    f"top reason {top_reason}."
                ),
                "next_action": "Keep funnel read-only; do not lower thresholds to create trades.",
            }
        )
    if settlement:
        blockers.append(
            {
                "area": "Settlement evidence",
                "source": "Phase 3AN settlement health confirm",
                "status": _format_enum_label(str(settlement.get("status") or "UNKNOWN")),
                "status_kind": "healthy" if not settlement.get("apply_command_exposed") else "blocked",
                "status_label": _format_enum_label(str(settlement.get("status") or "UNKNOWN")),
                "evidence": (
                    f"{settlement.get('exact_eligible_trades', 0)} exact eligible; "
                    f"apply exposed={settlement.get('apply_command_exposed')}."
                ),
                "next_action": "No settlement apply from Phase 3AN.",
            }
        )
    if sources:
        source_status = str(sources.get("source_evidence_status") or "SOURCE_EVIDENCE_BLOCKED")
        source_label = (
            "Source evidence classified"
            if source_status == "SOURCE_EVIDENCE_CLASSIFIED_GATED"
            else "Source evidence blocked"
        )
        blockers.append(
            {
                "area": "General sources",
                "source": "Phase 3AN general sources status",
                "status": source_label,
                "status_kind": "blocked",
                "status_label": source_label,
                "evidence": (
                    f"{sources.get('link_safe_rows', 0)} link-safe, "
                    f"{sources.get('forecast_safe_rows', 0)} forecast-safe, "
                    f"{sources.get('review_gated_rows', 0)} review-gated, "
                    f"{sources.get('blocked_rows', 0)} blocked row(s). "
                    f"First blocker {sources.get('first_hard_blocker') or 'UNKNOWN'}. "
                    f"USDA {sources.get('USDA')}; Cushman {sources.get('Cushman')}; "
                    f"FlightAware {sources.get('FlightAware')}."
                ),
                "next_action": str(
                    sources.get("next_action")
                    or "Resolve exact source evidence before link/forecast promotion."
                ),
            }
        )
    if phase3bb:
        blockers.append(
            {
                "area": "3BB-R2 source burn-down",
                "source": "Phase 3AN 3BB-R2 burn-down",
                "status": "Evidence gated",
                "status_kind": "blocked",
                "status_label": "Evidence gated",
                "evidence": f"{phase3bb.get('evidence_ready_rows', 0)} evidence-ready row(s).",
                "next_action": str(phase3bb.get("source_blocker") or "Keep report-only source review running."),
            }
        )
    if sports:
        blockers.append(
            {
                "area": "Sports provenance",
                "source": "Phase 3AN sports blocker report",
                "status": "Placeholders/provenance blocked",
                "status_kind": "blocked",
                "status_label": "Placeholders/provenance blocked",
                "evidence": (
                    f"{sports.get('placeholder_rows', 0)} placeholder row(s), "
                    f"{sports.get('partial_provenance_markets', 0)} partial provenance market(s); "
                    f"reasons {sports.get('reason_codes', [])}."
                ),
                "next_action": "Do not treat placeholders as teams or upgrade partial provenance.",
            }
        )
    if economic:
        blockers.append(
            {
                "area": "Economic/news",
                "source": "Phase 3AN economic/news watch",
                "status": _format_enum_label(str(economic.get("blocker_reason") or "UNKNOWN")),
                "status_kind": "blocked",
                "status_label": _format_enum_label(str(economic.get("blocker_reason") or "UNKNOWN")),
                "evidence": (
                    f"Economic compatible {economic.get('economic_compatible_parsed_markets', 0)}, "
                    f"news compatible {economic.get('news_compatible_parsed_markets', 0)}; "
                    f"current parsed econ/news "
                    f"{economic.get('economic_current_parsed_markets', 0)}/"
                    f"{economic.get('news_current_parsed_markets', 0)}, "
                    f"exact-linked current econ/news "
                    f"{economic.get('economic_exact_linked_current_markets', 0)}/"
                    f"{economic.get('news_exact_linked_current_markets', 0)}; "
                    f"exact-linked without parsed leg econ/news "
                    f"{economic.get('economic_exact_linked_current_without_parsed_leg', 0)}/"
                    f"{economic.get('news_exact_linked_current_without_parsed_leg', 0)}; "
                    f"handoff blockers "
                    f"{economic.get('economic_current_handoff_blocker')}/"
                    f"{economic.get('news_current_handoff_blocker')}."
                ),
                "next_action": "Do not force links or forecasts without compatible parsed markets.",
            }
        )


def _crypto_freshness_status_payload(
    *,
    report_path: Path,
    status_path: Path | None,
) -> dict[str, Any]:
    if report_path == CRYPTO_FRESHNESS_REPORT_PATH:
        status_path = status_path or CRYPTO_FRESHNESS_STATUS_PATH
    if status_path is None or not status_path.exists():
        return {}
    try:
        loaded = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _apply_phase3ak_crypto_watch_status(
    status: dict[str, Any],
    *,
    path: Path = PHASE3AK_CRYPTO_WATCH_STATUS_PATH,
) -> None:
    if not path.exists():
        return
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(payload, dict):
        return
    phase3ak_generated_at = parse_datetime(payload.get("generated_at"))
    crypto_generated_at = parse_datetime(status.get("generated_at"))
    if (
        phase3ak_generated_at is not None
        and crypto_generated_at is not None
        and phase3ak_generated_at < crypto_generated_at
    ):
        status.update(
            {
                "phase3ak_status_source": str(path),
                "phase3ak_status_ignored": "stale_older_than_crypto_watch",
            }
        )
        return
    window_summary = payload.get("window_summary") if isinstance(payload.get("window_summary"), dict) else {}
    funnel = payload.get("readiness_funnel") if isinstance(payload.get("readiness_funnel"), dict) else {}
    primary_blocker = str(payload.get("primary_blocker") or "UNKNOWN")
    runner_state = str(payload.get("runner_state") or payload.get("runner_status") or "UNKNOWN")
    status.update(
        {
            "watch_state": payload.get("watch_state") or primary_blocker,
            "watch_state_label": _format_enum_label(payload.get("watch_state") or primary_blocker),
            "runner_status": runner_state,
            "runner_status_label": _format_enum_label(runner_state),
            "runner_running": bool(payload.get("runner_running")),
            "runner_pid": payload.get("runner_pid"),
            "runner_next_action": payload.get("next_action") or status.get("runner_next_action"),
            "current_active_window_rows": window_summary.get("active_windows", 0),
            "expired_crypto_window_rows": window_summary.get("expired_windows", 0),
            "snapshot_stale_rows": window_summary.get("stale_quote_count", 0),
            "paper_ready_candidates": funnel.get("paper_ready_opportunities", 0),
            "positive_ev_rows": funnel.get("positive_raw_ev", 0),
            "positive_ev_preflight_candidates": funnel.get("positive_executable_ev", 0),
            "actionability_gap": primary_blocker,
            "actionability_gap_label": _format_enum_label(primary_blocker),
            "actionability_note": payload.get("next_action") or status.get("actionability_note"),
            "primary_gap": primary_blocker,
            "primary_gap_label": _format_enum_label(primary_blocker),
            "report_href": "/reports/phase_3ak/crypto_watch_status.md",
            "phase3ak_status_source": str(path),
        }
    )
    if runner_state == "RUNNING_CYCLE_OVERDUE":
        status.update(
            {
                "status": "RUNNING_CYCLE_OVERDUE",
                "status_label": "Cycle overdue",
                "badge_kind": "warn",
                "description": (
                    "Phase 3AK says the crypto watcher is active, but the last "
                    "completed watch cycle is overdue."
                ),
            }
        )
    elif runner_state == "RUNNER_STALE":
        status.update(
            {
                "status": "RUNNER_STALLED",
                "status_label": "Runner stale",
                "badge_kind": "warn",
                "description": "Phase 3AK says the crypto watcher heartbeat is stale.",
            }
        )
    elif primary_blocker == "PAPER_READY":
        status.update(
            {
                "status": "PAPER_READY",
                "status_label": "Paper ready",
                "badge_kind": "good",
                "description": "Phase 3AK found paper-ready crypto candidates.",
            }
        )
    else:
        status.update(
            {
                "status": primary_blocker,
                "status_label": _format_enum_label(primary_blocker),
                "badge_kind": "warn",
                "description": f"Phase 3AK current blocker: {_format_enum_label(primary_blocker)}.",
            }
        )


def cached_shell_context(session: Session, *, settings: Settings) -> dict[str, Any]:
    key = (
        settings.kalshi_env,
        settings.kalshi_db_url,
        settings.phase_3x_timezone,
        settings.phase_3x_theme,
        settings.phase_3x_density,
        settings.phase_3x_command_palette_enabled,
        _shell_status_snapshot_mtime(),
    )
    now = monotonic()
    if (
        _SHELL_CONTEXT_CACHE["key"] == key
        and _SHELL_CONTEXT_CACHE["context"] is not None
        and now < float(_SHELL_CONTEXT_CACHE["expires_at"])
    ):
        return _SHELL_CONTEXT_CACHE["context"]

    context = load_shell_status_context(settings=settings)
    _SHELL_CONTEXT_CACHE.update(
        {
            "key": key,
            "expires_at": now + SHELL_CONTEXT_CACHE_SECONDS,
            "context": context,
        }
    )
    return context


def _shell_status_snapshot_mtime() -> float:
    try:
        return DEFAULT_SHELL_STATUS_SNAPSHOT_PATH.stat().st_mtime
    except OSError:
        return 0.0


def _format_age_minutes(value: float | None) -> str:
    if value is None:
        return "n/a"
    if value < 1:
        return "under 1 minute"
    if value < 60:
        return f"{value:.0f} minutes"
    hours = value / 60
    if hours < 48:
        return f"{hours:.1f} hours"
    return f"{hours / 24:.1f} days"


def _format_duration_seconds(value: float | int | None) -> str:
    if value is None:
        return "n/a"
    seconds = max(0, int(round(float(value))))
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds / 60
    if minutes < 60:
        return f"{minutes:.0f}m"
    hours = minutes / 60
    if hours < 24:
        return f"{hours:.1f}h"
    return f"{hours / 24:.1f}d"


def _coerce_positive_int(value: Any) -> int | None:
    try:
        resolved = int(value)
    except (TypeError, ValueError):
        return None
    return resolved if resolved > 0 else None


def _coerce_nonnegative_float(value: Any) -> float | None:
    try:
        resolved = float(value)
    except (TypeError, ValueError):
        return None
    return resolved if resolved >= 0 else None


def _coerce_positive_float(value: Any) -> float | None:
    resolved = _coerce_nonnegative_float(value)
    return resolved if resolved and resolved > 0 else None


def _crypto_watch_progress_percent(
    *,
    cycle_number: int | None,
    total_cycles: int | None,
    elapsed_seconds: float | None,
    duration_budget_seconds: float | None,
) -> str:
    progress: float | None = None
    if cycle_number is not None and total_cycles:
        progress = min(100.0, max(0.0, cycle_number / total_cycles * 100))
    elif elapsed_seconds is not None and duration_budget_seconds:
        progress = min(100.0, max(0.0, elapsed_seconds / duration_budget_seconds * 100))
    if progress is None:
        return "n/a"
    return f"{progress:.1f}%"


def _crypto_watch_progress_label(
    *,
    cycle_number: int | None,
    total_cycles: int | None,
    progress_percent: str,
) -> str:
    if cycle_number is not None and total_cycles:
        return f"{cycle_number} / {total_cycles} ({progress_percent})"
    return progress_percent


def _crypto_watch_remaining_seconds(
    *,
    cycle_number: int | None,
    total_cycles: int | None,
    cadence_minutes: int,
    elapsed_seconds: float | None,
    duration_budget_seconds: float | None,
) -> float | None:
    if elapsed_seconds is not None and duration_budget_seconds:
        return max(0.0, duration_budget_seconds - elapsed_seconds)
    if cycle_number is not None and total_cycles:
        remaining_cycles = max(0, total_cycles - cycle_number)
        return remaining_cycles * max(1, cadence_minutes) * 60
    return None


def _format_timestamp_age_label(value: datetime | str | None) -> str:
    if value is None:
        return "n/a"
    resolved = parse_datetime(value) if isinstance(value, str) else value
    if resolved is None:
        return "n/a"
    if resolved.tzinfo is None:
        resolved = resolved.replace(tzinfo=UTC)
    age_minutes = max(0.0, (utc_now() - resolved).total_seconds() / 60)
    return f"{_format_age_minutes(age_minutes)} ago"


def _format_enum_label(value: Any) -> str:
    text = str(value or "UNKNOWN").strip()
    if not text:
        return "Unknown"
    known_labels = {
        "EV_NOT_POSITIVE": "EV not positive",
        "FORECAST_STALE": "Refreshing forecasts",
        "LIQUIDITY_ZERO": "No liquidity",
        "LOW_EDGE": "Low edge",
        "LOW_SCORE": "Low score",
        "NO_UNATTENDED_JOB": "No watcher",
        "POSITIVE_EV_NO_EXECUTABLE_BOOK": "Positive EV; no executable book",
        "RANKING_GAP": "Ranking gap",
        "RISK_MISSING": "Risk missing",
        "REFRESH_FORECASTS": "Refresh forecasts",
        "REFRESH_RANKINGS": "Refresh rankings",
        "REFRESH_SNAPSHOTS": "Refresh snapshots",
        "RUNNER_STALLED": "Runner stalled",
        "SNAPSHOT_MISSING": "Snapshot missing",
        "SNAPSHOT_STALE": "Refreshing snapshots",
        "SNAPSHOT_STALE_NO_ACTIONABLE_BOOK": "Refreshing snapshots",
        "STOPPED_WITH_STALE_PID": "Stopped; old PID",
        "WAITING_FOR_CLEAN_LIQUIDITY": "Monitoring book quality",
        "WAITING_FOR_EXECUTABLE_BOOK": "Monitoring liquidity",
        "WAITING_FOR_LIQUIDITY": "Monitoring liquidity",
        "WAITING_FOR_POSITIVE_EV": "Waiting for positive EV",
        "WATCHER_STOPPED": "Watcher stopped",
    }
    if text in known_labels:
        return known_labels[text]
    return text.replace("_", " ").title()


def _crypto_actionability_gap(summary: dict[str, Any]) -> str:
    if int(summary.get("positive_ev_preflight_candidates") or 0) > 0:
        return "READY_FOR_PREFLIGHT"
    if int(summary.get("positive_ev_clean_book_risk_missing_rows") or 0) > 0:
        return "RISK_MISSING"
    if int(summary.get("positive_ev_clean_book_rows") or 0) > 0:
        return "CLEAN_BOOK_WAITING_FOR_RISK"
    if int(summary.get("positive_ev_liquidity_positive_rows") or 0) > 0:
        return "LIQUIDITY_POSITIVE_BUT_NOT_CLEAN"
    if int(summary.get("positive_ev_no_executable_book_rows") or 0) > 0:
        return "POSITIVE_EV_NO_EXECUTABLE_BOOK"
    if int(summary.get("positive_ev_snapshot_stale_rows") or 0) > 0:
        return "SNAPSHOT_STALE"
    if int(summary.get("positive_ev_forecast_stale_rows") or 0) > 0:
        return "FORECAST_STALE"
    if int(summary.get("positive_ev_rows") or 0) > 0:
        return "POSITIVE_EV_NOT_ACTIONABLE"
    return str(summary.get("primary_gap_after_refresh") or "UNKNOWN")


def _crypto_actionability_gap_label(value: str) -> str:
    labels = {
        "CLEAN_BOOK_WAITING_FOR_RISK": "Needs risk check",
        "FORECAST_STALE": "Refreshing forecasts",
        "LIQUIDITY_POSITIVE_BUT_NOT_CLEAN": "Book not clean",
        "POSITIVE_EV_NO_EXECUTABLE_BOOK": "No executable book",
        "POSITIVE_EV_NOT_ACTIONABLE": "Not actionable",
        "READY_FOR_PREFLIGHT": "Ready for preflight",
        "RISK_MISSING": "Risk missing",
        "SNAPSHOT_STALE": "Refreshing snapshots",
        "UNKNOWN": "Unknown",
    }
    return labels.get(value, _format_enum_label(value))


def _crypto_actionability_note(summary: dict[str, Any], actionability_gap: str) -> str:
    positive_ev = int(summary.get("positive_ev_rows") or 0)
    no_book = int(summary.get("positive_ev_no_executable_book_rows") or 0)
    stale = int(summary.get("snapshot_stale_rows") or 0)
    missing = int(summary.get("snapshot_missing_rows") or 0)
    best_ticker = str(summary.get("best_ev_candidate_ticker") or "n/a")
    best_ev = _format_cents(summary.get("best_current_expected_value_cents"))
    if actionability_gap == "SNAPSHOT_MISSING" or missing > 0:
        row_word = "row has" if missing == 1 else "rows have"
        return (
            f"{missing} active crypto {row_word} no snapshot. GH-2 adds them to "
            "GH-1's bounded recovery manifest; no order action is allowed until "
            "a market snapshot arrives."
        )
    if actionability_gap == "POSITIVE_EV_NO_EXECUTABLE_BOOK":
        row_word = "row is" if no_book == 1 else "rows are"
        return (
            f"{no_book} positive-EV {row_word} not paper-ready because no usable "
            f"visible book/liquidity is available yet. Best watched row: "
            f"{best_ticker} at {best_ev}; the watcher keeps refreshing without "
            "placing orders."
        )
    if actionability_gap == "LIQUIDITY_POSITIVE_BUT_NOT_CLEAN":
        return (
            "A positive-EV row has liquidity, but the spread/execution screen is "
            "not clean enough for paper-only preflight."
        )
    if actionability_gap == "CLEAN_BOOK_WAITING_FOR_RISK":
        return (
            "A clean executable book exists, but risk evidence is not ready for "
            "paper-only preflight."
        )
    if actionability_gap == "READY_FOR_PREFLIGHT":
        return "A clean positive-EV row is ready for paper-only risk preflight."
    if actionability_gap in {"SNAPSHOT_STALE", "FORECAST_STALE"}:
        return (
            f"{stale} current crypto row(s) still need fresh market evidence before "
            "the watch can trust their paper-readiness gates."
        )
    if positive_ev <= 0:
        return "No current crypto row has strictly positive EV yet."
    return "Positive-EV crypto rows exist, but one or more paper-readiness gates remain blocked."


def _crypto_gate_failure_examples(
    payload: dict[str, Any],
    *,
    limit: int = 12,
) -> list[dict[str, Any]]:
    if limit <= 0:
        return []
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for section in (
        "blocked_active_pure_examples",
        "liquidity_watch_rows",
        "best_ev_candidates",
    ):
        raw_rows = payload.get(section)
        if not isinstance(raw_rows, list):
            continue
        for raw in raw_rows:
            if not isinstance(raw, dict):
                continue
            ticker = str(raw.get("ticker") or "").strip()
            if not ticker or ticker in seen:
                continue
            blockers = _crypto_candidate_blockers(raw)
            if not blockers:
                continue
            seen.add(ticker)
            rows.append(
                {
                    "ticker": ticker,
                    "title": str(raw.get("clean_title") or raw.get("title") or ticker),
                    "detail_href": f"/opportunities/{ticker}",
                    "expected_value_label": _format_cents(
                        raw.get("expected_value_cents")
                    ),
                    "book_label": _crypto_candidate_book_label(raw, blockers),
                    "failed_gate_label": ", ".join(
                        _format_enum_label(blocker) for blocker in blockers
                    ),
                    "next_action": _crypto_candidate_next_action(raw, blockers),
                }
            )
            if len(rows) >= limit:
                return rows
    return rows


def _crypto_candidate_blockers(row: dict[str, Any]) -> list[str]:
    raw_blockers = row.get("preflight_blockers") or row.get("blocking_gates") or []
    blockers = [str(item) for item in raw_blockers if str(item).strip()]
    blocked_reason = str(row.get("blocked_reason") or row.get("readiness_status") or "")
    if blocked_reason == "BLOCKED_MISSING_ACTIVE_SNAPSHOT":
        blockers.insert(0, "snapshot_missing")
    elif row.get("freshness_issue") and row.get("freshness_issue") != "FRESH":
        blockers.insert(0, str(row["freshness_issue"]).lower())
    if "best_price" in row and row.get("best_price") is None:
        blockers.append("missing_executable_price")
    expected_value = _coerce_nonnegative_float(row.get("expected_value"))
    if expected_value is None:
        try:
            expected_value = float(row.get("expected_value"))
        except (TypeError, ValueError):
            expected_value = None
    if expected_value is not None and expected_value <= 0:
        blockers.append("ev_not_positive")
    return list(dict.fromkeys(blockers))


def _crypto_candidate_book_label(row: dict[str, Any], blockers: list[str]) -> str:
    if "snapshot_missing" in blockers:
        return "Snapshot missing"
    if "missing_executable_price" in blockers:
        return "No executable price"
    try:
        liquidity = float(row.get("liquidity_score"))
    except (TypeError, ValueError):
        liquidity = None
    if liquidity is not None and liquidity <= 0:
        return "No visible liquidity"
    return "Visible executable book"


def _crypto_candidate_next_action(row: dict[str, Any], blockers: list[str]) -> str:
    reported = _first_text(row.get("what_would_make_paper_ready"))
    if reported:
        return reported
    if "snapshot_missing" in blockers:
        return "Keep the ticker in GH-1 snapshot recovery until a snapshot arrives."
    if "missing_executable_price" in blockers:
        return "Wait for a visible bid or ask; paper-order creation remains disabled."
    if "ev_not_positive" in blockers:
        return "Wait for market price or model probability to produce strictly positive EV."
    return "Re-evaluate after the next guarded GH-2 refresh."


def _crypto_book_probe(
    payload: dict[str, Any],
    summary: dict[str, Any],
) -> dict[str, Any]:
    raw_rows = payload.get("positive_ev_no_executable_book_examples")
    raw: dict[str, Any] | None = None
    if isinstance(raw_rows, list):
        for row in raw_rows:
            if isinstance(row, dict) and str(row.get("ticker") or "").strip():
                raw = row
                break

    if raw is None:
        ticker = str(summary.get("best_ev_candidate_ticker") or "").strip()
        if (
            int(summary.get("positive_ev_no_executable_book_rows") or 0) <= 0
            or not ticker
            or ticker == "n/a"
        ):
            return {"available": False}
        raw = {
            "ticker": ticker,
            "expected_value_cents": summary.get("best_current_expected_value_cents"),
        }

    ticker = str(raw.get("ticker") or "").strip()
    needed_label = _first_text(raw.get("what_would_make_paper_ready"))
    if not needed_label:
        needed_label = (
            "Visible bid/ask liquidity with a clean spread must appear in the "
            "real Kalshi snapshot before this can become paper-ready."
        )

    return {
        "available": True,
        "ticker": ticker,
        "title": str(raw.get("clean_title") or ticker),
        "detail_href": f"/opportunities/{ticker}",
        "side": _format_enum_label(raw.get("best_side") or "n/a"),
        "price_label": _format_price_cents(raw.get("best_price")),
        "expected_value_label": _format_cents(raw.get("expected_value_cents")),
        "liquidity_label": _crypto_liquidity_label(raw.get("liquidity_score")),
        "liquidity_raw": _format_plain_decimal(raw.get("liquidity_score")),
        "spread_label": _format_price_cents(raw.get("spread")),
        "blockers_label": _format_book_probe_blockers(raw),
        "needed_label": needed_label,
        "safety_label": (
            "Paper-only: does not create exchange liquidity or place orders."
        ),
    }


def _format_book_probe_blockers(row: dict[str, Any]) -> str:
    blockers = row.get("preflight_blockers")
    if not isinstance(blockers, list) or not blockers:
        blockers = row.get("blocking_gates")
    if not isinstance(blockers, list) or not blockers:
        return "No extra blocker details reported."
    labels = [_format_enum_label(item) for item in blockers if item]
    return ", ".join(labels) if labels else "No extra blocker details reported."


def _crypto_ev_calibration_label(value: Any) -> str:
    state = str(value or "UNKNOWN")
    labels = {
        "NEAR_MISS_NO_POSITIVE_EV": "Near misses only",
        "POSITIVE_EV_PRESENT": "Positive EV present",
        "NO_CURRENT_EV_ROWS": "No current EV rows",
        "UNKNOWN": "Unknown",
    }
    return labels.get(state, state.replace("_", " ").title())


def _crypto_near_miss_summary(summary: dict[str, Any]) -> str:
    near_misses = int(summary.get("ev_near_miss_rows") or 0)
    if near_misses <= 0:
        return "No near-miss crypto rows are inside the current watch band."
    gap = _format_cents(summary.get("best_ev_gap_to_positive_cents"))
    liquidity_positive = int(summary.get("ev_near_miss_liquidity_positive_rows") or 0)
    clean_execution = int(summary.get("ev_near_miss_clean_execution_rows") or 0)
    return (
        f"{near_misses} crypto row(s) are near positive EV. "
        f"The closest needs {gap}; {liquidity_positive} have visible book liquidity "
        f"and {clean_execution} currently pass the clean execution screen."
    )


def _crypto_liquidity_emergence_summary(summary: dict[str, Any]) -> str:
    emerged = int(summary.get("liquidity_emergence_rows") or 0)
    clean = int(summary.get("clean_execution_emergence_rows") or 0)
    positive_ev = int(summary.get("positive_ev_liquidity_emergence_rows") or 0)
    near_miss = int(summary.get("near_miss_liquidity_emergence_rows") or 0)
    if emerged <= 0 and clean <= 0:
        return "No watched crypto row gained executable liquidity since the previous cycle."
    return (
        f"{emerged} watched crypto row(s) gained liquidity and {clean} gained clean "
        f"execution; {positive_ev} are positive EV and {near_miss} are near-miss rows."
    )


def _crypto_liquidity_emergence_examples(
    payload: dict[str, Any],
    *,
    limit: int = 5,
) -> list[dict[str, Any]]:
    raw_rows = payload.get("liquidity_emergence_examples")
    if not isinstance(raw_rows, list):
        return []

    rows: list[dict[str, Any]] = []
    for raw in raw_rows:
        if not isinstance(raw, dict):
            continue
        ticker = str(raw.get("ticker") or "").strip()
        if not ticker:
            continue
        rows.append(
            {
                "ticker": ticker,
                "title": str(raw.get("clean_title") or ticker),
                "detail_href": f"/opportunities/{ticker}",
                "transition_label": str(
                    raw.get("transition_label") or "Liquidity changed"
                ),
                "watch_type": str(raw.get("watch_type") or "EV_WATCH"),
                "expected_value_label": _format_cents(
                    raw.get("expected_value_cents")
                ),
                "gap_label": _format_cents(raw.get("gap_to_positive_cents")),
                "current_liquidity_label": _crypto_liquidity_label(
                    raw.get("liquidity_score")
                ),
                "previous_liquidity_label": _crypto_liquidity_label(
                    raw.get("previous_liquidity_score")
                ),
                "spread_label": _format_price_cents(raw.get("spread")),
                "best_side": str(raw.get("best_side") or "n/a"),
            }
        )
        if len(rows) >= limit:
            break
    return rows


def _crypto_near_miss_examples(payload: dict[str, Any], *, limit: int = 5) -> list[dict[str, Any]]:
    raw_rows = payload.get("ev_near_miss_examples")
    if not isinstance(raw_rows, list) or not raw_rows:
        raw_rows = payload.get("best_ev_candidates")
    if not isinstance(raw_rows, list):
        return []

    rows: list[dict[str, Any]] = []
    for raw in raw_rows:
        if not isinstance(raw, dict):
            continue
        ticker = str(raw.get("ticker") or "").strip()
        if not ticker:
            continue
        rows.append(
            {
                "ticker": ticker,
                "title": str(raw.get("clean_title") or ticker),
                "detail_href": f"/opportunities/{ticker}",
                "expected_value_label": _format_cents(
                    raw.get("expected_value_cents")
                ),
                "gap_label": _format_cents(raw.get("gap_to_positive_cents")),
                "price_label": _format_price_cents(raw.get("best_price")),
                "spread_label": _format_price_cents(raw.get("spread")),
                "liquidity_label": _crypto_liquidity_label(
                    raw.get("liquidity_score")
                ),
                "liquidity_raw": _format_plain_decimal(raw.get("liquidity_score")),
                "model_probability_label": _format_probability(
                    raw.get("side_probability")
                ),
                "best_side": str(raw.get("best_side") or "n/a"),
                "status_label": _crypto_near_miss_status(raw),
                "what_would_make_ready": _first_text(
                    raw.get("what_would_make_paper_ready")
                ),
                "blocking_gates": _format_gate_list(raw.get("blocking_gates")),
            }
        )
        if len(rows) >= limit:
            break
    return rows


def _crypto_near_miss_status(row: dict[str, Any]) -> str:
    liquidity = to_decimal(row.get("liquidity_score"))
    spread = to_decimal(row.get("spread"))
    if liquidity is not None and liquidity <= 0:
        return "Waiting for book"
    if spread is not None and spread > Decimal("0.02"):
        return "Spread watch"
    if liquidity is not None and liquidity > 0:
        return "Book visible"
    return "EV watch"


def _format_cents(value: Any) -> str:
    decimal_value = to_decimal(value)
    if decimal_value is None:
        return "n/a"
    return f"{decimal_value.quantize(Decimal('0.1'))} cents"


def _format_price_cents(value: Any) -> str:
    decimal_value = to_decimal(value)
    if decimal_value is None:
        return "n/a"
    return f"{(decimal_value * Decimal('100')).quantize(Decimal('0.1'))} cents"


def _format_probability(value: Any) -> str:
    decimal_value = to_decimal(value)
    if decimal_value is None:
        return "n/a"
    return f"{(decimal_value * Decimal('100')).quantize(Decimal('0.1'))}%"


def _format_plain_decimal(value: Any) -> str:
    decimal_value = to_decimal(value)
    if decimal_value is None:
        return "n/a"
    return decimal_to_str(decimal_value) or "0"


def _crypto_liquidity_label(value: Any) -> str:
    decimal_value = to_decimal(value)
    if decimal_value is None:
        return "n/a"
    if decimal_value <= 0:
        return "None"
    if decimal_value < Decimal("0.25"):
        return "Low"
    if decimal_value < Decimal("0.70"):
        return "Medium"
    return "High"


def _first_text(value: Any) -> str:
    if isinstance(value, list):
        for item in value:
            text = str(item or "").strip()
            if text:
                return text
        return ""
    return str(value or "").strip()


def _format_gate_list(value: Any) -> str:
    if not isinstance(value, list):
        return "n/a"
    labels = [str(item).replace("_", " ").lower() for item in value if item]
    return ", ".join(labels) if labels else "n/a"


def _dashboard_charts(analytics: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "title": "Paper P&L Over Time",
            "points": _chart_points(analytics.get("daily_pnl", []), value_key="total_pnl"),
        },
        {
            "title": "Forecast Count Over Time",
            "points": _indexed_points(analytics.get("forecast_accuracy_trend", [])),
        },
        {
            "title": "Opportunity Count Over Time",
            "points": _indexed_points(analytics.get("opportunity_trend", [])),
        },
        {
            "title": "Model Accuracy Over Time",
            "points": _chart_points(
                analytics.get("forecast_accuracy_trend", []),
                value_key="value",
            ),
        },
    ]


def _dashboard_summary(
    opportunities: list[OpportunityView],
    *,
    blocked_opportunities: list[OpportunityView] | None = None,
) -> dict[str, Any]:
    high_confidence = sum(1 for item in opportunities if item.confidence_label == "High")
    no_trade = sum(1 for item in opportunities if item.recommendation == "No trade recommended")
    blocked = blocked_opportunities or []
    return {
        "shown": len(opportunities),
        "high_confidence": high_confidence,
        "no_trade": no_trade,
        "blocked_research": len(blocked),
        "top_action": opportunities[0].recommended_action if opportunities else "Run a scan first.",
    }


def _fast_dashboard_summary(
    *,
    opportunities: list[dict[str, Any]],
    blocked_opportunities: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    blocked = blocked_opportunities or []
    return {
        "shown": len(opportunities),
        "high_confidence": sum(
            1 for item in opportunities if item.get("confidence_label") == "High"
        ),
        "no_trade": sum(
            1 for item in opportunities if item.get("traffic_light", {}).get("kind") == "avoid"
        ),
        "blocked_research": len(blocked),
        "top_action": (
            opportunities[0].get("recommended_action", "Run a scan first.")
            if opportunities
            else "Run a scan first."
        ),
    }


def _opportunity_links_health_summary(
    session: Session,
    *,
    settings: Settings,
    limit: int = 100,
) -> dict[str, Any]:
    rankings = _top_rankings(session, limit)
    verified = 0
    blocked = 0
    diagnostic = 0
    stale = 0
    for ranking in rankings:
        identity = verify_market_identity(session, ranking=ranking, settings=settings)
        if identity.tradeable:
            verified += 1
        else:
            blocked += 1
            if identity.diagnostic_only:
                diagnostic += 1
            if identity.url_verification_status == "STALE_CATALOG":
                stale += 1
    if not rankings:
        status = "DIAGNOSTIC_ONLY"
    elif blocked == 0:
        status = "HEALTHY"
    elif verified == 0:
        status = "DIAGNOSTIC_ONLY"
    elif stale:
        status = "STALE"
    else:
        status = "DEGRADED"
    return {
        "status": status,
        "verified_clickable_opportunities": verified,
        "blocked_unverified_opportunities": blocked,
        "diagnostic_only_rows": diagnostic,
        "stale_catalog_rows": stale,
        "last_audit_at": _last_generated(Path("reports/phase3ao/opportunity_link_audit.json")),
        "next_action": _opportunity_links_next_action(status, stale=stale),
    }


def _opportunity_links_next_action(status: str, *, stale: int) -> str:
    if status == "HEALTHY":
        return "Keep running opportunity-link-audit after market catalog refreshes."
    if stale:
        return "Refresh Kalshi market catalog, then rerun opportunity-link-audit."
    return "Repair missing market_ticker lineage or move unsupported rows to diagnostics."


def _fast_executive_summary(
    session: Session,
    *,
    opportunities: list[dict[str, Any]],
) -> dict[str, Any]:
    today = utc_now().replace(hour=0, minute=0, second=0, microsecond=0)
    latest_snapshot = session.scalar(
        select(MarketSnapshot)
        .order_by(desc(MarketSnapshot.captured_at), desc(MarketSnapshot.id))
        .limit(1)
    )
    return {
        "markets_scanned": _count_since(session, MarketSnapshot, MarketSnapshot.captured_at, today),
        "forecasts_generated": _count_since(session, Forecast, Forecast.forecasted_at, today),
        "opportunities_found": _count_since(
            session,
            MarketOpportunity,
            MarketOpportunity.detected_at,
            today,
        ),
        "open_opportunities": len(opportunities),
        "best_opportunity": (
            opportunities[0].get("short_title", "Run a scan first.")
            if opportunities
            else "Run a scan first."
        ),
        "last_data_refresh": latest_snapshot.captured_at.isoformat() if latest_snapshot else "n/a",
        "last_data_refresh_label": _format_timestamp_age_label(
            latest_snapshot.captured_at if latest_snapshot else None
        ),
    }


def _fast_today_workspace(
    session: Session,
    *,
    opportunities: list[dict[str, Any]],
    portfolio: dict[str, Any],
) -> dict[str, Any]:
    warnings = []
    if portfolio.get("fast_bounded"):
        warnings.append(
            {
                "severity": "incomplete",
                "title": "Bounded portfolio view",
                "detail": (
                    "Navigation uses a bounded paper-position summary; open the full report "
                    "for complete historical detail."
                ),
            }
        )
    if opportunities:
        headline = f"{len(opportunities)} ranked paper opportunity candidates are visible."
        decision_state = "ranked"
        decision_label = "Ranked opportunities"
    else:
        headline = "No opportunity currently clears the visible review filters."
        decision_state = "no_trade"
        decision_label = "No trade"
    return {
        "headline": headline,
        "decision_state": decision_state,
        "decision_label": decision_label,
        "candidate_count": _safe_count(session, MarketRanking),
        "forecast_count": _safe_count(session, Forecast),
        "paper_trade_count": _safe_count(session, PaperOrder),
        "blocked_count": 0,
        "reduced_count": 0,
        "warnings": warnings,
        "no_trade_message": (
            "No opportunity currently clears the available ranking, liquidity, sizing, "
            "and risk evidence. This is a valid state, not an error."
        ),
    }


def _today_portfolio_summary_fast(session: Session) -> dict[str, Any]:
    positions = list(
        session.scalars(
            select(PaperPosition)
            .where((PaperPosition.yes_contracts != 0) | (PaperPosition.no_contracts != 0))
            .order_by(PaperPosition.updated_at.desc())
        )
    )
    realized = sum(to_decimal(position.realized_pnl) or Decimal("0") for position in positions)
    exposure = sum(_paper_position_exposure(position) for position in positions)
    open_orders = int(
        session.scalar(
            select(func.count()).select_from(PaperOrder).where(PaperOrder.status == ORDER_OPEN)
        )
        or 0
    )
    return {
        "portfolio_value": decimal_to_str(realized) or "0",
        "total_exposure": decimal_to_str(exposure) or "0",
        "open_positions": len(positions),
        "realized_pnl": decimal_to_str(realized) or "0",
        "unrealized_pnl": "0",
        "total_pnl": decimal_to_str(realized) or "0",
        "open_orders": open_orders,
        "positions": [],
        "latest_snapshot": None,
        "category_allocation": [],
        "pnl_series": [],
        "exposure_series": [],
        "fast_bounded": True,
    }


def _paper_position_exposure(position: PaperPosition) -> Decimal:
    yes_price = to_decimal(position.avg_yes_price) or Decimal("0")
    no_price = to_decimal(position.avg_no_price) or Decimal("0")
    return abs(Decimal(position.yes_contracts)) * yes_price + abs(
        Decimal(position.no_contracts)
    ) * no_price


def _today_learning_status_fast(
    session: Session,
    *,
    settings: Settings,
) -> dict[str, Any]:
    target = max(1, settings.learning_target_settled_trades)
    settled = settled_paper_trade_count(session)
    today = utc_now().replace(hour=0, minute=0, second=0, microsecond=0)
    daily_trades = int(
        session.scalar(
            select(func.count()).select_from(PaperOrder).where(PaperOrder.created_at >= today)
        )
        or 0
    )
    remaining = max(0, target - settled)
    progress = min((settled / target) * 100.0, 100.0)
    return {
        "settled_paper_trades": settled,
        "learning_confidence": f"{progress:.1f}%",
        "trade_generation_health": {
            "label": "Active" if daily_trades else "Idle",
            "kind": "healthy" if daily_trades else "incomplete",
        },
        "expected_completion": (
            "complete" if remaining == 0 else f"{remaining} settlement rows remaining"
        ),
    }


def _today_alert_status_fast(session: Session, *, limit: int) -> dict[str, Any]:
    events = list(
        session.scalars(
            select(AlertEvent)
            .order_by(desc(AlertEvent.created_at), desc(AlertEvent.id))
            .limit(limit)
        )
    )
    return {
        "alerts": [],
        "events": [
            {
                "id": event.id,
                "severity": event.severity,
                "message": event.message,
                "created_at": event.created_at.isoformat(),
                "acknowledged_at": (
                    event.acknowledged_at.isoformat() if event.acknowledged_at else None
                ),
            }
            for event in events
        ],
        "open_count": sum(1 for event in events if event.acknowledged_at is None),
    }


def _safe_count(session: Session, model: type) -> int:
    try:
        return int(session.scalar(select(func.count()).select_from(model)) or 0)
    except Exception:
        return 0


def _report_cards() -> list[dict[str, str]]:
    specs = [
        ("Opportunities report", "reports/opportunities.md", REPORT_LINKS.opportunities),
        ("Model leaderboard", "reports/model_leaderboard.md", REPORT_LINKS.leaderboard),
        ("Tournament report", "reports/model_tournament.md", REPORT_LINKS.tournament),
        ("Paper trading report", "reports/paper_trading.md", REPORT_LINKS.paper),
        ("Execution report", "reports/execution_report.md", REPORT_LINKS.execution),
        ("Autopilot report", "reports/autopilot_report.md", REPORT_LINKS.autopilot),
        ("Overnight report", "reports/overnight_report.md", REPORT_LINKS.overnight),
        ("Portfolio summary", "reports/portfolio_summary.md", REPORT_LINKS.portfolio),
        ("Daily briefing", "reports/daily_briefing.md", REPORT_LINKS.daily_briefing),
        ("Analytics report", "reports/analytics_report.md", REPORT_LINKS.analytics),
        ("Best payouts", "reports/best_payouts.md", REPORT_LINKS.best_payouts),
        ("News report", "reports/news_report.md", REPORT_LINKS.news_report),
        ("News opportunities", "reports/news_opportunities.md", REPORT_LINKS.news_opportunities),
        ("News backtest", "reports/news_backtest.md", REPORT_LINKS.news_backtest),
        ("Sports report", "reports/sports_report.md", REPORT_LINKS.sports_report),
        (
            "Sports opportunities",
            "reports/sports_opportunities.md",
            REPORT_LINKS.sports_opportunities,
        ),
        ("Sports backtest", "reports/sports_backtest.md", REPORT_LINKS.sports_backtest),
        ("Learning report", "reports/learning_report.md", REPORT_LINKS.learning_report),
        (
            "Learning diagnostics",
            "reports/learning_diagnostics.md",
            REPORT_LINKS.learning_diagnostics,
        ),
        ("Learning targets", "reports/learning_targets.md", REPORT_LINKS.learning_targets),
        (
            "Self-evaluation journal",
            "reports/self_evaluation_journal.md",
            REPORT_LINKS.self_evaluation_journal,
        ),
        ("Database health", "reports/database_report.md", REPORT_LINKS.database_report),
        (
            "System remediation",
            "reports/system_readiness_remediation.md",
            REPORT_LINKS.system_remediation,
        ),
        ("Market memory", "reports/market_memory_report.md", REPORT_LINKS.market_memory_report),
        (
            "Advanced risk",
            "reports/advanced_risk_report.md",
            REPORT_LINKS.advanced_risk_report,
        ),
        (
            "Live readiness",
            "reports/live_readiness_report.md",
            REPORT_LINKS.live_readiness_report,
        ),
        (
            "System certification",
            "reports/system_certification/system_certification_report.md",
            REPORT_LINKS.system_certification_report,
        ),
        (
            "Model readiness",
            "reports/model_readiness.md",
            REPORT_LINKS.model_readiness,
        ),
        (
            "Model confidence",
            "reports/model_confidence.md",
            REPORT_LINKS.model_confidence,
        ),
        (
            "Control center",
            "reports/control_center.md",
            REPORT_LINKS.control_center,
        ),
        (
            "Microstructure report",
            "reports/microstructure_report.md",
            REPORT_LINKS.microstructure_report,
        ),
        (
            "Microstructure opportunities",
            "reports/microstructure_opportunities.md",
            REPORT_LINKS.microstructure_opportunities,
        ),
        (
            "Microstructure backtest",
            "reports/microstructure_backtest.md",
            REPORT_LINKS.microstructure_backtest,
        ),
        ("Meta model", "reports/meta_report.md", REPORT_LINKS.meta_report),
        (
            "Meta evaluation",
            "reports/meta_evaluation.md",
            REPORT_LINKS.meta_evaluation,
        ),
        (
            "Meta opportunities",
            "reports/meta_opportunities.md",
            REPORT_LINKS.meta_opportunities,
        ),
        (
            "Phase 3Z model repair audit",
            "reports/model_repair/model_repair_audit.md",
            REPORT_LINKS.model_repair_audit,
        ),
        (
            "Phase 3Z market coverage doctor",
            "reports/market_coverage/market_coverage_doctor.md",
            REPORT_LINKS.market_coverage_doctor,
        ),
        (
            "Phase 3Z metrics reconciliation",
            "reports/model_repair/metrics_reconciliation.md",
            REPORT_LINKS.metrics_reconciliation,
        ),
    ]
    cards: list[dict[str, str]] = []
    for title, local_path, href in specs:
        path = Path(local_path)
        cards.append(
            {
                "title": title,
                "href": href,
                "last_generated": _last_generated(path),
                "status": "Ready" if path.exists() else "Not generated",
            }
        )
    return cards


def _last_generated(path: Path) -> str:
    if not path.exists():
        return "Not generated yet"
    return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).isoformat()


def _best_model_label(model_rows: list[dict[str, Any]]) -> str:
    for row in model_rows:
        if row.get("rank_color") == "green" and row.get("roi") is not None:
            return f"{row['model_name']} ROI {row['roi']}"
    return "Needs more settled paper trades."


def _count_since(session: Session, table: Any, column: Any, since: datetime) -> int:
    return int(session.scalar(select(func.count()).select_from(table).where(column >= since)) or 0)


def _chart_points(rows: list[dict[str, Any]], *, value_key: str) -> list[dict[str, Any]]:
    values = [to_decimal(row.get(value_key)) or Decimal("0") for row in rows]
    return _scaled_points(values)


def _indexed_points(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return _scaled_points([Decimal(index + 1) for index, _ in enumerate(rows)])


def _scaled_points(values: list[Decimal]) -> list[dict[str, Any]]:
    if not values:
        return []
    if len(values) == 1:
        return [{"x": "50", "y": "50", "value": decimal_to_str(values[0]) or "0"}]
    low = min(values)
    high = max(values)
    span = high - low
    points = []
    for index, value in enumerate(values):
        x = Decimal(index) / Decimal(len(values) - 1) * Decimal("100")
        y = Decimal("50") if span == 0 else Decimal("100") - ((value - low) / span * Decimal("100"))
        points.append(
            {
                "x": decimal_to_str(x) or "0",
                "y": decimal_to_str(y) or "50",
                "value": decimal_to_str(value) or "0",
            }
        )
    return points


def _score_percent(value: Any) -> str:
    decimal_value = to_decimal(value)
    if decimal_value is None:
        return "n/a"
    return f"{decimal_value.quantize(Decimal('1'))}%"


def _microstructure_summary(session: Session, ticker: str) -> dict[str, Any]:
    feature = latest_microstructure_feature(session, ticker)
    if feature is None:
        return {
            "available": False,
            "spread_trend": "n/a",
            "liquidity_trend": "n/a",
            "orderbook_pressure": "n/a",
            "late_move_warning": "n/a",
            "possible_informed_flow": "n/a",
        }
    return {
        "available": True,
        "spread_trend": _trend(feature.spread_change, tighter_is_good=True),
        "liquidity_trend": _trend(feature.liquidity_change_pct),
        "orderbook_pressure": _pressure_label(feature.orderbook_imbalance),
        "late_move_warning": _score_label(feature.late_move_score),
        "possible_informed_flow": _score_label(feature.smart_money_score),
        "confidence": feature.microstructure_confidence,
    }


def _trend(value: Any, *, tighter_is_good: bool = False) -> str:
    decimal_value = to_decimal(value)
    if decimal_value is None:
        return "n/a"
    if decimal_value < 0:
        return "tightening" if tighter_is_good else "decreasing"
    if decimal_value > 0:
        return "widening" if tighter_is_good else "improving"
    return "flat"


def _pressure_label(value: Any) -> str:
    decimal_value = to_decimal(value)
    if decimal_value is None:
        return "n/a"
    if decimal_value > Decimal("0.25"):
        return "YES pressure"
    if decimal_value < Decimal("-0.25"):
        return "NO pressure"
    return "balanced"


def _score_label(value: Any) -> str:
    decimal_value = to_decimal(value)
    if decimal_value is None:
        return "n/a"
    if decimal_value >= Decimal("0.70"):
        return "high"
    if decimal_value >= Decimal("0.25"):
        return "watch"
    return "low"


def _paper_decision_from_ranking(
    session: Session,
    ranking: MarketRanking,
    settings: Settings,
) -> PaperDecision | None:
    price = to_decimal(ranking.best_price)
    probability = to_decimal(ranking.forecast_probability)
    edge = to_decimal(ranking.estimated_edge)
    if price is None or probability is None or edge is None or ranking.best_side is None:
        return None
    latest_forecast = session.scalar(
        select(Forecast)
        .where(Forecast.ticker == ranking.ticker, Forecast.model_name == ranking.forecast_model)
        .order_by(desc(Forecast.forecasted_at), desc(Forecast.id))
        .limit(1)
    )
    reason = "Created from local decision UI review."
    if settings.learning_mode:
        reason += " Learning Mode paper trade created to grow settled sample size."
    return PaperDecision(
        ticker=ranking.ticker,
        forecast_id=latest_forecast.id if latest_forecast else None,
        model_name=ranking.forecast_model,
        side=ranking.best_side,
        probability=probability,
        market_price=price,
        limit_price=price,
        edge=edge,
        quantity=settings.paper_max_order_quantity,
        reason=reason,
        raw_decision_json={
            "source": "decision_ui",
            "ranking_id": ranking.id,
            "opportunity_score": ranking.opportunity_score,
            "price": decimal_to_str(price),
        },
    )


def _action(
    ticker: str,
    action: str,
    status: str,
    message: str,
    *,
    dry_run: bool = True,
    checks: list[RiskCheck] | None = None,
) -> ActionResult:
    return ActionResult(
        ticker=ticker,
        action=action,
        status=status,
        message=message,
        dry_run=dry_run,
        checks=checks or [],
    )
