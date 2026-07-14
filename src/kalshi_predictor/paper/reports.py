from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.paper.ledger import get_paper_summary
from kalshi_predictor.utils.time import utc_now


def write_paper_trading_report(
    session: Session,
    output_path: str | Path,
    *,
    settings: Settings | None = None,
) -> Path:
    resolved_settings = settings or get_settings()
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    summary = get_paper_summary(session)
    lines = [
        "# Paper Trading Report",
        "",
        f"- Generated at: {utc_now().isoformat()}",
        "",
        "## Strategy Config",
        "",
        f"- Minimum edge: `{resolved_settings.paper_min_edge}`",
        f"- Max order quantity: `{resolved_settings.paper_max_order_quantity}`",
        f"- Max position per market: `{resolved_settings.paper_max_position_per_market}`",
        f"- Max open orders: `{resolved_settings.paper_max_open_orders}`",
        f"- Default fee per contract: `{resolved_settings.paper_default_fee_per_contract}`",
        f"- Allow BUY_NO: `{resolved_settings.paper_allow_buy_no}`",
        f"- Allow selling: `{resolved_settings.paper_allow_selling}`",
        f"- Order TTL minutes: `{resolved_settings.paper_order_ttl_minutes}`",
        f"- Dynamic position sizing mode: `{resolved_settings.dynamic_position_sizing_mode}`",
        "- Dynamic position sizing live max contracts: "
        f"`{resolved_settings.dynamic_position_sizing_live_max_contracts}`",
        "- Dynamic position sizing global max contracts: "
        f"`{resolved_settings.dynamic_position_sizing_global_max_contracts}`",
        "",
        "## Overall Summary",
        "",
        f"- Total paper orders: {summary.total_orders}",
        f"- Filled paper orders: {summary.filled_orders}",
        f"- Open paper orders: {summary.open_orders}",
        f"- Active positions: {summary.active_positions}",
        f"- Total realized P&L: {summary.total_realized_pnl}",
        f"- Estimated unrealized P&L: {summary.estimated_unrealized_pnl}",
        f"- Total P&L: {summary.total_pnl}",
        "",
        "## Positions Table",
        "",
        "| Ticker | YES | NO | Avg YES | Avg NO | Realized P&L | Exposure |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    lines.extend(_position_rows(summary.top_positions))
    lines.extend(
        [
            "",
            "## Recent Fills",
            "",
            "| Filled at | Ticker | Side | Price | Quantity | Fee |",
            "|---|---|---|---:|---:|---:|",
        ]
    )
    lines.extend(_fill_rows(summary.recent_fills))
    lines.extend(
        [
            "",
            "## P&L Summary",
            "",
            f"- Realized P&L: {summary.total_realized_pnl}",
            f"- Estimated unrealized P&L: {summary.estimated_unrealized_pnl}",
            f"- Total P&L: {summary.total_pnl}",
            "",
            "## Risk Notes",
            "",
            "- Phase 2 is paper trading only and uses stored forecasts/snapshots.",
            "- No authenticated Kalshi requests, private endpoints, or real orders are used.",
            "- Immediate fills are optimistic and should be interpreted as a simulation baseline.",
            "- Position and open-order limits are enforced before simulated order creation.",
            "",
            "## Next Recommended Action",
            "",
            "Run recurring collection, then `paper-run`, then review this report after settlement "
            "sync and `paper-pnl` updates.",
            "",
        ]
    )
    output.write_text("\n".join(lines), encoding="utf-8")
    return output


def _position_rows(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return ["| _No active positions_ | 0 | 0 |  |  | 0 | 0 |"]
    return [
        "| "
        f"{row['ticker']} | "
        f"{row['yes_contracts']} | "
        f"{row['no_contracts']} | "
        f"{row.get('avg_yes_price') or ''} | "
        f"{row.get('avg_no_price') or ''} | "
        f"{row.get('realized_pnl') or '0'} | "
        f"{row.get('exposure') or '0'} |"
        for row in rows
    ]


def _fill_rows(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return ["| _No fills yet_ |  |  |  | 0 | 0 |"]
    return [
        "| "
        f"{row['filled_at']} | "
        f"{row['ticker']} | "
        f"{row['side']} | "
        f"{row['price']} | "
        f"{row['quantity']} | "
        f"{row['fee']} |"
        for row in rows
    ]
