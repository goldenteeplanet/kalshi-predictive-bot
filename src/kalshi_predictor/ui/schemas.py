from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class RiskCheck:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class ActionResult:
    ticker: str
    action: str
    status: str
    message: str
    dry_run: bool = True
    report_path: str | None = None
    checks: list[RiskCheck] = field(default_factory=list)


@dataclass(frozen=True)
class ReportLinks:
    opportunities: str
    leaderboard: str
    tournament: str
    execution: str
    paper: str = "/reports/paper_trading.md"
    autopilot: str = "/reports/autopilot_report.md"
    overnight: str = "/reports/overnight_report.md"
    portfolio: str = "/reports/portfolio_summary.md"
    daily_briefing: str = "/reports/daily_briefing.md"
    analytics: str = "/reports/analytics_report.md"
    best_payouts: str = "/reports/best_payouts.md"
    news_report: str = "/reports/news_report.md"
    news_opportunities: str = "/reports/news_opportunities.md"
    news_backtest: str = "/reports/news_backtest.md"
    sports_report: str = "/reports/sports_report.md"
    sports_opportunities: str = "/reports/sports_opportunities.md"
    sports_backtest: str = "/reports/sports_backtest.md"
    learning_report: str = "/reports/learning_report.md"
    learning_diagnostics: str = "/reports/learning_diagnostics.md"
    learning_targets: str = "/reports/learning_targets.md"
    self_evaluation_journal: str = "/reports/self_evaluation_journal.md"
    tonight_report: str = "/reports/tonight_report.md"
    database_report: str = "/reports/database_report.md"
    system_remediation: str = "/reports/system_readiness_remediation.md"
    market_memory_report: str = "/reports/market_memory_report.md"
    advanced_risk_report: str = "/reports/advanced_risk_report.md"
    live_readiness_report: str = "/reports/live_readiness_report.md"
    system_certification_report: str = (
        "/reports/system_certification/system_certification_report.md"
    )
    model_readiness: str = "/reports/model_readiness.md"
    model_confidence: str = "/reports/model_confidence.md"
    control_center: str = "/reports/control_center.md"
    microstructure_report: str = "/reports/microstructure_report.md"
    microstructure_opportunities: str = "/reports/microstructure_opportunities.md"
    microstructure_backtest: str = "/reports/microstructure_backtest.md"
    meta_report: str = "/reports/meta_report.md"
    meta_evaluation: str = "/reports/meta_evaluation.md"
    meta_opportunities: str = "/reports/meta_opportunities.md"
    link_coverage: str = "/reports/link_coverage_report.md"
    model_repair_audit: str = "/reports/model_repair/model_repair_audit.md"
    market_coverage_doctor: str = "/reports/market_coverage/market_coverage_doctor.md"
    metrics_reconciliation: str = "/reports/model_repair/metrics_reconciliation.md"
    opportunity_link_audit: str = "/reports/phase3ao/opportunity_link_audit.md"


@dataclass(frozen=True)
class OpportunityView:
    ticker: str
    title: str
    short_title: str
    category: str
    category_badge: str
    model_name: str
    side: str
    recommendation_label: str
    price: str
    opportunity_score: str
    estimated_edge: str
    expected_value: str
    payout_to_risk_ratio: str
    payout_adjusted_score: str
    spread: str
    liquidity: str
    liquidity_score: str
    confidence_percent: str
    formatted_time_remaining: str
    time_to_close_minutes: str
    model_confidence_score: str
    paper_position: str
    demo_execution_status: str
    ranking_id: int | None
    recommendation: str
    confidence_label: str
    edge_cents: str
    score_label: str
    top_reason: str
    top_risk: str
    badges: list[dict[str, str]]
    traffic_light: dict[str, str]
    risk_meter: dict[str, Any]
    why_interesting: str
    why_risky: str
    what_bot_would_do: str
    primary_driver: str
    supporting_signals: list[str]
    model_confidence: str
    data_freshness: str
    recommended_action: str
    model_explanation: str
    risks: list[str]
    forum_consensus: dict[str, Any]
    microstructure: dict[str, Any]
    signal_badges: list[dict[str, str]]
    meta_selection: dict[str, Any]
    decision_clarity: dict[str, Any]
    market_identity: dict[str, Any]


@dataclass(frozen=True)
class DetailView:
    ticker: str
    title: str
    rules: str
    opportunity: OpportunityView | None
    orderbook_summary: dict[str, Any]
    forecast_history: list[dict[str, Any]]
    component_probabilities: dict[str, Any]
    feature_json: str
    score_breakdown: dict[str, Any]
    paper_pnl: list[dict[str, Any]]
    backtest_history: list[dict[str, Any]]
    recent_snapshots: list[dict[str, Any]]
    recent_fills: list[dict[str, Any]]
    risk_checks: list[RiskCheck]
    explanation: dict[str, Any]
