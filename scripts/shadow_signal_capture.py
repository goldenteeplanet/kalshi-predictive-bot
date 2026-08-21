#!/usr/bin/env python3
"""Capture isolated, idempotent shadow observations from canonical R5 evidence."""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--r5", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    r5 = json.loads(args.r5.read_text(encoding="utf-8"))
    phase3bc_path = Path(r5["reports"]["phase3bc_json"])
    if not phase3bc_path.is_absolute():
        phase3bc_path = args.r5.parents[2] / phase3bc_path
    phase3bc = json.loads(phase3bc_path.read_text(encoding="utf-8"))

    args.output.parent.mkdir(parents=True, exist_ok=True)
    existing: set[str] = set()
    if args.output.exists():
        for line in args.output.read_text(encoding="utf-8").splitlines():
            if line.strip():
                existing.add(str(json.loads(line)["observation_id"]))

    captured = 0
    with args.output.open("a", encoding="utf-8") as target:
        for row in phase3bc.get("rows", []):
            if not row.get("ticker") or not row.get("latest_snapshot_at"):
                continue
            identity = "|".join(
                str(value or "")
                for value in (
                    "canonical-r5-shadow-v1",
                    row.get("ticker"),
                    row.get("latest_forecast_id"),
                    row.get("latest_snapshot_at"),
                )
            )
            observation_id = hashlib.sha256(identity.encode()).hexdigest()
            if observation_id in existing:
                continue
            blockers = list(row.get("blockers") or [])
            record = {
                "observation_id": observation_id,
                "strategy_id": "canonical-r5-shadow",
                "strategy_version": "1",
                "captured_at": datetime.now(UTC).isoformat(),
                "market_ticker": row.get("ticker"),
                "market_family": row.get("series_ticker"),
                "event_ticker": row.get("event_ticker"),
                "forecast_id": row.get("latest_forecast_id"),
                "forecast_timestamp": row.get("latest_forecast_at"),
                "snapshot_timestamp": row.get("latest_snapshot_at"),
                "observable_bid": row.get("book_bid_price"),
                "observable_ask": row.get("book_ask_price"),
                "executable_side": row.get("best_side"),
                "executable_price": row.get("best_price"),
                "model_probability": row.get("model_probability"),
                "theoretical_ev": row.get("estimated_edge"),
                "executable_ev": row.get("expected_value"),
                "spread": row.get("spread"),
                "liquidity": row.get("liquidity"),
                "opportunity_score": row.get("opportunity_score"),
                "time_to_close_minutes": row.get("time_to_close_minutes"),
                "book_usable": bool(row.get("book_usable")),
                "estimated_slippage": None,
                "simulated_fee": None,
                "hypothetical_size": 0,
                "decision": "OBSERVE_ONLY",
                "decision_reason": row.get("final_action"),
                "production_blockers": blockers,
                "production_gates_all_passed": not blockers,
                "settlement": None,
                "simulated_pnl": None,
                "brier_contribution": None,
                "log_loss_contribution": None,
                "guarded_tables_written": False,
            }
            target.write(json.dumps(record, sort_keys=True) + "\n")
            existing.add(observation_id)
            captured += 1
    print(json.dumps({"captured": captured, "total": len(existing), "output": str(args.output)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
