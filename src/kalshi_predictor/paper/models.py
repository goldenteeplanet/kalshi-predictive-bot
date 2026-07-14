from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

BUY_YES = "BUY_YES"
BUY_NO = "BUY_NO"
SELL_YES = "SELL_YES"
SELL_NO = "SELL_NO"

ORDER_OPEN = "OPEN"
ORDER_FILLED = "FILLED"
ORDER_CANCELLED = "CANCELLED"
ORDER_EXPIRED = "EXPIRED"


@dataclass(frozen=True)
class PaperDecision:
    ticker: str
    forecast_id: int | None
    model_name: str
    side: str
    probability: Decimal
    market_price: Decimal
    limit_price: Decimal
    edge: Decimal
    quantity: int
    reason: str
    raw_decision_json: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class StrategyResult:
    forecasts_scanned: int = 0
    decisions: list[PaperDecision] = field(default_factory=list)
    skipped_due_to_edge: int = 0
    skipped_due_to_risk_limits: int = 0
    duplicates_skipped: int = 0
    candidate_scan_limit: int | None = None

    @property
    def decisions_generated(self) -> int:
        return len(self.decisions)


@dataclass(frozen=True)
class PaperRunSummary:
    forecasts_scanned: int
    decisions_generated: int
    orders_created: int
    fills_created: int
    skipped_due_to_edge: int
    skipped_due_to_risk_limits: int
    duplicates_skipped: int
    candidate_scan_limit: int | None = None


@dataclass(frozen=True)
class PnlSummary:
    positions_evaluated: int
    pnl_rows_inserted: int
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    total_pnl: Decimal


@dataclass(frozen=True)
class PaperSummary:
    total_orders: int
    filled_orders: int
    open_orders: int
    active_positions: int
    total_realized_pnl: Decimal
    estimated_unrealized_pnl: Decimal
    total_pnl: Decimal
    top_positions: list[dict[str, Any]]
    recent_fills: list[dict[str, Any]]
