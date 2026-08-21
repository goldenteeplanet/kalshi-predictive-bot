from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cycle-id", required=True)
    parser.add_argument("--preflight", type=Path, required=True)
    parser.add_argument("--gate", type=Path, required=True)
    parser.add_argument("--history", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--required-cycles", type=int, default=3)
    args = parser.parse_args()

    preflight = _read_json(args.preflight)
    gate = _read_json(args.gate)
    results = preflight.get("results") or []
    gate_rows = {
        row.get("ticker"): row
        for row in gate.get("weather_rows", gate.get("rows", []))
        if row.get("ticker")
    }
    recorded = [row for row in results if row.get("status") == "RECORDED"]
    relevant_gate_rows = [gate_rows.get(row.get("ticker"), {}) for row in recorded]
    reasons: list[str] = []
    generated_at = _parse_datetime(preflight.get("generated_at"))
    if generated_at is None or (datetime.now(UTC) - generated_at).total_seconds() > 120:
        reasons.append("PREFLIGHT_ARTIFACT_STALE")
    if not recorded:
        reasons.append("NO_RECORDED_PREFLIGHT_ROWS")
    if any(row.get("phase3n_action") != "ALLOW" for row in recorded):
        reasons.append("PHASE3N_NOT_ALLOW")
    if any(row.get("phase3n_hard_blocks") for row in recorded):
        reasons.append("PHASE3N_HARD_BLOCK")
    if any(not row.get("paper_ready") for row in relevant_gate_rows):
        reasons.append("POST_PREFLIGHT_GATE_NOT_READY")
    if preflight.get("paper_orders_before") != preflight.get("paper_orders_after"):
        reasons.append("PAPER_ORDER_COUNT_CHANGED")

    entry = {
        "cycle_id": str(args.cycle_id),
        "recorded_at": datetime.now(UTC).isoformat(),
        "healthy": not reasons,
        "failure_reasons": reasons,
        "recorded_tickers": [row.get("ticker") for row in recorded],
        "pair_keys": [row.get("forecast_snapshot_pair_key") for row in recorded],
        "paper_orders_before": preflight.get("paper_orders_before"),
        "paper_orders_after": preflight.get("paper_orders_after"),
        "paper_ready_tickers": [
            row.get("ticker") for row in relevant_gate_rows if row.get("paper_ready")
        ],
    }
    history = _read_history(args.history)
    by_cycle = {str(row.get("cycle_id")): row for row in history}
    by_cycle[entry["cycle_id"]] = entry
    history = list(by_cycle.values())[-100:]
    consecutive = 0
    for row in reversed(history):
        if not row.get("healthy"):
            break
        consecutive += 1
    required = max(1, args.required_cycles)
    status = {
        "generated_at": datetime.now(UTC).isoformat(),
        "status": "COMPLETE" if consecutive >= required else "RUNNING",
        "required_healthy_cycles": required,
        "consecutive_healthy_cycles": consecutive,
        "remaining_cycles": max(0, required - consecutive),
        "soak_complete": consecutive >= required,
        "latest_cycle": entry,
        "paper_order_creation_enabled": False,
        "paper_order_kill_switch_retained": True,
        "explicit_operator_approval_required": True,
        "history": history[-10:],
    }
    _write_history(args.history, history)
    _write_atomic(args.output, status)
    print(json.dumps(status, indent=2, sort_keys=True))


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _parse_datetime(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _read_history(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _write_history(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )
    temporary.replace(path)


def _write_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(path)


if __name__ == "__main__":
    main()
