"""Offline correlated latency bootstrap propagated through pessimistic replay."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import random
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from scripts.local.phase4nd_microstructure_replay import replay_order

SCHEMA = "phase4ne.latency-bootstrap.v1"
COMPONENTS = (
    "decision_to_send_ms",
    "send_to_ack_ms",
    "market_data_age_ms",
    "processing_pause_ms",
    "scheduler_jitter_ms",
    "reconnect_delay_ms",
)


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def bootstrap_latency_replay(
    observations: object,
    events: list[dict[str, object]],
    base_order: dict[str, object],
    *,
    outcome: str,
    sample_count: int,
    block_size: int,
    seed: int,
    minimum_observations: int,
    minimum_p99_net_pnl: str,
    timeout_cap_ms: int = 5000,
) -> dict[str, object]:
    errors: list[str] = []
    if not isinstance(observations, list) or sample_count < 1 or block_size < 1:
        return _result(["INPUT_INVALID"], [], "REFUSE")
    valid = []
    censored = 0
    for index, row in enumerate(observations):
        if not isinstance(row, dict) or any(
            not isinstance(row.get(key), int | float) for key in COMPONENTS
        ):
            errors.append(f"OBSERVATION_{index}_INVALID")
            continue
        if any(float(row[key]) < 0 for key in COMPONENTS):
            errors.append(f"OBSERVATION_{index}_CLOCK_SKEW_OR_NEGATIVE_LATENCY")
            continue
        status = row.get("status")
        normalized = copy.deepcopy(row)
        if status in {"TIMEOUT", "MISSING_ACK"}:
            normalized["send_to_ack_ms"] = max(float(normalized["send_to_ack_ms"]), timeout_cap_ms)
            normalized["censored"] = True
            censored += 1
        elif status != "ACK":
            errors.append(f"OBSERVATION_{index}_STATUS_INVALID")
            continue
        else:
            normalized["censored"] = False
        valid.append(normalized)
    rng = random.Random(seed)
    sampled = []
    while len(sampled) < sample_count and valid:
        start = rng.randrange(len(valid))
        for offset in range(block_size):
            sampled.append(copy.deepcopy(valid[(start + offset) % len(valid)]))
            if len(sampled) == sample_count:
                break
    paths = []
    for path_index, row in enumerate(sampled):
        send_latency = int(
            row["decision_to_send_ms"]
            + row["processing_pause_ms"]
            + row["scheduler_jitter_ms"]
            + row["reconnect_delay_ms"]
        )
        order = copy.deepcopy(base_order)
        order["latency_ms"] = send_latency
        order["ack_delay_ms"] = int(row["send_to_ack_ms"])
        replay_events = copy.deepcopy(events)
        decision = _time(str(order["decision_time"]))
        first_book = next((event for event in replay_events if event.get("type") == "BOOK"), None)
        if first_book is not None:
            first_book["timestamp"] = (
                decision - timedelta(milliseconds=float(row["market_data_age_ms"]))
            ).isoformat()
        replay = replay_order(replay_events, order, outcome=outcome)
        pessimistic = replay.get("envelopes", {}).get("pessimistic", {})
        total_latency = send_latency + int(row["send_to_ack_ms"])
        paths.append(
            {
                "path_index": path_index,
                "source_observation_id": row.get("observation_id"),
                "regime": row.get("regime"),
                "components": {key: row[key] for key in COMPONENTS},
                "censored": row["censored"],
                "total_latency_ms": total_latency,
                "verdict": replay["verdict"],
                "errors": replay["errors"],
                "fill_rate": pessimistic.get("fill_rate", "0"),
                "net_pnl": pessimistic.get("net_pnl", "0"),
                "adverse_selection": pessimistic.get("post_fill_adverse_selection", "0"),
            }
        )
    percentiles = {
        name: _percentile([row["total_latency_ms"] for row in paths], value)
        for name, value in (
            ("p50", 0.50),
            ("p90", 0.90),
            ("p95", 0.95),
            ("p99", 0.99),
            ("worst", 1.0),
        )
    }
    p99_path = (
        min(
            paths,
            key=lambda row: (abs(row["total_latency_ms"] - percentiles["p99"]), row["path_index"]),
        )
        if paths
        else None
    )
    claim_errors = []
    if len(valid) < minimum_observations:
        claim_errors.append("TAIL_SUPPORT_INADEQUATE")
    if p99_path is None or Decimal(str(p99_path["net_pnl"])) < Decimal(minimum_p99_net_pnl):
        claim_errors.append("P99_EXECUTION_ECONOMICS_LIMIT_BREACHED")
    distributions = {
        "fill_rate": _summary([float(row["fill_rate"]) for row in paths]),
        "net_pnl": _summary([float(row["net_pnl"]) for row in paths]),
        "adverse_selection": _summary([float(row["adverse_selection"]) for row in paths]),
        "stale_book_count": sum("STALE_BOOK" in row["errors"] for row in paths),
        "rejection_count": sum(row["verdict"] == "REFUSE" for row in paths),
        "market_closure_count": sum("MARKET_CLOSED" in row["errors"] for row in paths),
    }
    result = _result(
        sorted(set(errors)), paths, "PASS" if not errors and not claim_errors else "REFUSE"
    )
    result.update(
        {
            "claim_errors": claim_errors,
            "observation_count": len(valid),
            "censored_observation_count": censored,
            "sample_count": len(paths),
            "block_size": block_size,
            "seed": seed,
            "latency_percentiles_ms": percentiles,
            "p99_path": p99_path,
            "distributions": distributions,
            "joint_component_sampling": True,
        }
    )
    result["bootstrap_sha256"] = _digest(result)
    return result


def _percentile(values, quantile):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(quantile * len(ordered)) - 1)]


def _summary(values):
    if not values:
        return {"minimum": None, "p50": None, "p95": None, "p99": None, "maximum": None}
    return {
        "minimum": min(values),
        "p50": _percentile(values, 0.5),
        "p95": _percentile(values, 0.95),
        "p99": _percentile(values, 0.99),
        "maximum": max(values),
    }


def _result(errors, paths, claim):
    return {
        "schema": SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": errors,
        "readiness": claim,
        "paths": paths,
        "submitted_order": False,
        "safety": _safety(),
    }


def _safety():
    return {
        "offline_only": True,
        "order_submission": False,
        "order_creation": False,
        "persistence": False,
        "network_access": False,
        "runtime_write": False,
        "paper_execution": False,
        "demo_execution": False,
        "live_execution": False,
        "autopilot": False,
    }
