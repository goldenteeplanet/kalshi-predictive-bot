from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class PaperScaleEvidence:
    settled_total: int
    settled_by_category: dict[str, int] = field(default_factory=dict)
    net_pnl_after_costs: str = "0"
    duplicate_violations: int = 0
    limit_violations: int = 0
    incomplete_lineage_rows: int = 0
    synthetic_trades: int = 0
    stale_trades: int = 0
    threshold_relaxed_trades: int = 0


def evaluate_paper_scale_gate(
    evidence: PaperScaleEvidence,
    *,
    proposed_live_categories: tuple[str, ...] = ("crypto", "weather"),
    required_total: int = 100,
    required_per_live_category: int = 30,
) -> dict[str, Any]:
    category_checks = {
        category: int(evidence.settled_by_category.get(category, 0))
        >= required_per_live_category
        for category in proposed_live_categories
    }
    eligible_settled = max(
        evidence.settled_total
        - evidence.synthetic_trades
        - evidence.stale_trades
        - evidence.threshold_relaxed_trades,
        0,
    )
    checks = {
        "portfolio_sample": eligible_settled >= required_total,
        "category_samples": all(category_checks.values()),
        "positive_net_pnl": _positive(evidence.net_pnl_after_costs),
        "no_duplicate_violations": evidence.duplicate_violations == 0,
        "no_limit_violations": evidence.limit_violations == 0,
        "complete_lineage": evidence.incomplete_lineage_rows == 0,
    }
    return {
        "schema_version": "paper-scale-gate-v1",
        "passed": all(checks.values()),
        "checks": checks,
        "category_checks": category_checks,
        "eligible_settled_trades": eligible_settled,
        "required_total": required_total,
        "required_per_live_category": required_per_live_category,
        "remaining_total": max(required_total - eligible_settled, 0),
        "evidence": asdict(evidence),
        "paper_order_creation_enabled": False,
        "live_execution_enabled": False,
    }


def build_zero_trade_diagnosis(counts: dict[str, int]) -> dict[str, Any]:
    ordered = (
        "no_market",
        "no_snapshot",
        "no_forecast",
        "no_ranking",
        "insufficient_edge",
        "liquidity",
        "risk",
        "duplicate",
        "category_quota",
    )
    primary = next((reason for reason in ordered if int(counts.get(reason, 0)) > 0), None)
    return {
        "schema_version": "zero-trade-diagnosis-v1",
        "primary_reason": primary or "NO_OBSERVED_BLOCKER_COUNTS",
        "counts": {reason: int(counts.get(reason, 0)) for reason in ordered},
        "thresholds_lowered": False,
    }


def _positive(value: str) -> bool:
    try:
        return float(value) > 0
    except (TypeError, ValueError):
        return False
