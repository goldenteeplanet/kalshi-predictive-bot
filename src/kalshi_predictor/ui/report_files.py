"""Explicit report downloads and filesystem availability for the dashboard."""

from pathlib import Path

ALLOWED_REPORTS = frozenset(
    {
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
        "live_readiness_report.md",
        "link_coverage_report.md",
        "tonight_report.md",
        "system_certification/system_certification_report.md",
        "model_repair/model_repair_audit.md",
        "model_repair/metrics_reconciliation.md",
        "market_coverage/market_coverage_doctor.md",
        "phase3ao/opportunity_link_audit.md",
    }
)


def report_download_path(report_name: str) -> Path | None:
    if report_name not in ALLOWED_REPORTS:
        return None
    try:
        root = Path("reports").resolve(strict=True)
        path = (root / report_name).resolve(strict=True)
        if path.is_relative_to(root) and path.is_file():
            return path
    except (OSError, RuntimeError):
        pass
    return None


def report_available(href: str) -> bool:
    return href.startswith("/reports/") and report_download_path(href[9:]) is not None
