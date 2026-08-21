#!/usr/bin/env python3
"""Build an isolated, conservative shadow-paper trade ledger from observations."""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


def _decimal(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value)) if value is not None else None
    except (InvalidOperation, ValueError):
        return None


def evaluate(
    row: dict[str, Any], *, fee: Decimal, slippage: Decimal
) -> tuple[dict[str, Any] | None, list[str]]:
    """Return a trade only when all existing gates and conservative costs pass."""
    reasons: list[str] = []
    blockers = list(row.get("production_blockers") or [])
    if blockers or not row.get("production_gates_all_passed", False):
        reasons.extend(f"PRODUCTION_GATE:{item}" for item in blockers)
        if not blockers:
            reasons.append("PRODUCTION_GATES_NOT_PASSED")

    side = row.get("executable_side")
    if side not in {"BUY_YES", "BUY_NO"}:
        reasons.append("NO_EXECUTABLE_SIDE")
    price = _decimal(row.get("executable_price"))
    if price is None or not Decimal("0") < price < Decimal("1"):
        reasons.append("NO_VALID_EXECUTABLE_PRICE")
    raw_ev = _decimal(row.get("executable_ev"))
    if raw_ev is None or raw_ev <= 0:
        reasons.append("EXECUTABLE_EV_NOT_POSITIVE")
    if not row.get("book_usable", False):
        reasons.append("BOOK_NOT_USABLE")

    net_ev = raw_ev - fee - slippage if raw_ev is not None else None
    if net_ev is None or net_ev <= 0:
        reasons.append("NET_EV_NOT_POSITIVE_AFTER_COSTS")
    if reasons:
        return None, sorted(set(reasons))

    assert price is not None and net_ev is not None
    fill_price = price + slippage
    if fill_price >= 1:
        return None, ["SLIPPAGE_ADJUSTED_PRICE_INVALID"]
    observation_id = str(row["observation_id"])
    trade_id = hashlib.sha256(f"shadow-paper-v1|{observation_id}".encode()).hexdigest()
    return {
        "trade_id": trade_id,
        "observation_id": observation_id,
        "strategy_id": row.get("strategy_id"),
        "strategy_version": row.get("strategy_version"),
        "market_ticker": row.get("market_ticker"),
        "market_family": row.get("market_family"),
        "event_ticker": row.get("event_ticker"),
        "decision_timestamp": row.get("captured_at"),
        "forecast_timestamp": row.get("forecast_timestamp"),
        "snapshot_timestamp": row.get("snapshot_timestamp"),
        "side": side,
        "observable_ask": row.get("observable_ask"),
        "quoted_executable_price": str(price),
        "fill_price": str(fill_price),
        "quantity": 1,
        "fee_per_contract": str(fee),
        "slippage_per_contract": str(slippage),
        "raw_executable_ev": str(raw_ev),
        "net_executable_ev": str(net_ev),
        "settlement": None,
        "shadow_pnl": None,
        "guarded_tables_written": False,
    }, []


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--signals", type=Path, required=True)
    parser.add_argument("--trades", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--fee", type=Decimal, default=Decimal("0.01"))
    parser.add_argument("--slippage", type=Decimal, default=Decimal("0.01"))
    args = parser.parse_args()
    if args.fee < 0 or args.slippage < 0:
        parser.error("fee and slippage must be non-negative")

    signals = [
        json.loads(line)
        for line in args.signals.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    args.trades.parent.mkdir(parents=True, exist_ok=True)
    existing: dict[str, dict[str, Any]] = {}
    if args.trades.exists():
        existing = {
            row["trade_id"]: row
            for row in (
                json.loads(line)
                for line in args.trades.read_text(encoding="utf-8").splitlines()
                if line.strip()
            )
        }

    rejected = Counter()
    accepted: list[dict[str, Any]] = []
    for row in signals:
        trade, reasons = evaluate(row, fee=args.fee, slippage=args.slippage)
        if trade is None:
            rejected.update(reasons)
        elif trade["trade_id"] not in existing:
            accepted.append(trade)

    if accepted:
        with args.trades.open("a", encoding="utf-8") as target:
            for trade in accepted:
                target.write(json.dumps(trade, sort_keys=True) + "\n")
                existing[trade["trade_id"]] = trade

    payload = {
        "mode": "ISOLATED_SHADOW_PAPER_ONLY",
        "guarded_tables_written": False,
        "observations_evaluated": len(signals),
        "new_trades": len(accepted),
        "total_trades": len(existing),
        "open_trades": sum(row.get("settlement") is None for row in existing.values()),
        "fee_per_contract": str(args.fee),
        "slippage_per_contract": str(args.slippage),
        "acceptance_policy": "EXISTING_PRODUCTION_GATES_AND_POSITIVE_NET_EXECUTABLE_EV",
        "rejection_counts": dict(sorted(rejected.items())),
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
