"""Offline adversarial backtest harness with a strict event-time firewall."""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

SCHEMA = "phase4mz.adversarial-backtest.v1"
SCENARIOS = (
    "BASELINE",
    "SHUFFLED_OUTCOMES",
    "TIMESTAMP_SHIFT",
    "FUTURE_DATA_INJECTION",
    "STALE_QUOTES",
    "WIDENED_SPREAD",
    "FEES",
    "LATENCY",
    "QUEUE_POSITION",
    "PARTIAL_FILLS",
    "REJECTED_FILLS",
    "MISSING_FEED",
    "EXTREME_MOVE",
    "CORRELATED_MARKETS",
    "REGIME_CHANGE",
    "PARAMETER_PERTURBATION",
    "UNPROFITABLE_CONTROL",
)


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else None


def run_adversarial_backtest(
    records: object,
    *,
    scenario: str,
    minimum_edge: str = "0.05",
    fee_per_contract: str = "0.01",
    max_quote_age_seconds: int = 300,
) -> dict[str, object]:
    errors: list[str] = []
    leakage: list[dict[str, object]] = []
    if not isinstance(records, list) or scenario not in SCENARIOS:
        return _result(["INPUT_OR_SCENARIO_INVALID"], [], leakage, scenario)
    rows = copy.deepcopy(records)
    _apply_scenario(rows, scenario)
    threshold = Decimal(minimum_edge) + (
        Decimal("0.04") if scenario == "PARAMETER_PERTURBATION" else 0
    )
    trades: list[dict[str, object]] = []
    for index, row in enumerate(rows):
        decision = _time(row.get("decision_time"))
        quote = _time(row.get("quote_time"))
        settlement = _time(row.get("settlement_time"))
        feature_times = row.get("feature_times")
        violations = []
        if (
            decision is None
            or quote is None
            or settlement is None
            or not isinstance(feature_times, list)
        ):
            errors.append(f"ROW_{index}_TIME_OR_FEATURE_SHAPE_INVALID")
            continue
        for name, timestamp in [
            ("quote", quote),
            *[(f"feature_{i}", _time(value)) for i, value in enumerate(feature_times)],
        ]:
            if timestamp is None:
                violations.append(f"{name}_TIME_INVALID")
            elif timestamp > decision:
                violations.append(f"{name}_AFTER_DECISION")
        if settlement <= decision:
            violations.append("SETTLEMENT_NOT_AFTER_DECISION")
        if violations:
            leakage.append({"row": index, "ticker": row.get("ticker"), "violations": violations})
            continue
        if (decision - quote).total_seconds() > max_quote_age_seconds:
            errors.append(f"ROW_{index}_STALE_QUOTE")
            continue
        required = ("yes_probability", "yes_ask", "no_ask", "outcome")
        if any(row.get(key) is None for key in required):
            errors.append(f"ROW_{index}_MISSING_FEED")
            continue
        probability = Decimal(str(row["yes_probability"]))
        yes_ask, no_ask = Decimal(str(row["yes_ask"])), Decimal(str(row["no_ask"]))
        candidates = [
            ("yes", probability - yes_ask, yes_ask),
            ("no", 1 - probability - no_ask, no_ask),
        ]
        side, edge, price = max(candidates, key=lambda value: value[1])
        if scenario == "UNPROFITABLE_CONTROL":
            side = "no" if side == "yes" else "yes"
            price = no_ask if side == "no" else yes_ask
            edge = -abs(edge)
        if edge < threshold and scenario != "UNPROFITABLE_CONTROL":
            continue
        fill_fraction = _fill_fraction(scenario, index)
        if fill_fraction == 0:
            errors.append(f"ROW_{index}_FILL_REJECTED")
            continue
        quantity = Decimal(str(row.get("quantity", 1))) * fill_fraction
        stressed_price = min(Decimal("0.99"), price + _price_stress(scenario))
        gross = (
            quantity if str(row["outcome"]).lower() == side else Decimal("0")
        ) - stressed_price * quantity
        fee = (
            Decimal("1") * quantity
            if scenario == "UNPROFITABLE_CONTROL"
            else Decimal(fee_per_contract) * quantity
            if scenario
            in {"BASELINE", "FEES", "LATENCY", "WIDENED_SPREAD", "QUEUE_POSITION", "PARTIAL_FILLS"}
            else Decimal("0")
        )
        net = gross - fee
        trades.append(
            {
                "ticker": row.get("ticker"),
                "market_group": row.get("market_group"),
                "decision_time": row.get("decision_time"),
                "side": side,
                "probability": str(probability),
                "outcome": row.get("outcome"),
                "quantity": str(quantity),
                "entry_price": str(stressed_price),
                "edge": str(edge),
                "gross_pnl": str(gross),
                "fees": str(fee),
                "net_pnl": str(net),
                "exposure": str(stressed_price * quantity + fee),
                "fill_fraction": str(fill_fraction),
            }
        )
    if leakage:
        errors.append("INFORMATION_LEAKAGE_DETECTED")
    return _result(sorted(set(errors)), trades, leakage, scenario, input_count=len(rows))


def run_scenario_matrix(records: list[dict[str, object]]) -> dict[str, object]:
    reports = [run_adversarial_backtest(records, scenario=scenario) for scenario in SCENARIOS]
    result = {
        "schema": SCHEMA,
        "scenario_count": len(reports),
        "reports": reports,
        "all_scenarios_exercised": [report["scenario"] for report in reports] == list(SCENARIOS),
        "execution_capability": False,
        "safety": _safety(),
    }
    result["matrix_sha256"] = _digest(result)
    return result


def _apply_scenario(rows: list[dict[str, object]], scenario: str) -> None:
    if scenario == "SHUFFLED_OUTCOMES":
        outcomes = [row.get("outcome") for row in reversed(rows)]
        for row, outcome in zip(rows, outcomes, strict=True):
            row["outcome"] = outcome
    elif scenario in {"TIMESTAMP_SHIFT", "FUTURE_DATA_INJECTION"} and rows:
        decision = _time(rows[0].get("decision_time"))
        if decision is not None:
            rows[0]["feature_times"] = [
                *(rows[0].get("feature_times") or []),
                (decision + timedelta(seconds=1)).isoformat(),
            ]
    elif scenario == "STALE_QUOTES":
        for row in rows:
            decision = _time(row.get("decision_time"))
            if decision is not None:
                row["quote_time"] = (decision - timedelta(hours=1)).isoformat()
    elif scenario == "WIDENED_SPREAD":
        for row in rows:
            row["yes_ask"] = str(
                min(Decimal("0.99"), Decimal(str(row["yes_ask"])) + Decimal("0.08"))
            )
            row["no_ask"] = str(min(Decimal("0.99"), Decimal(str(row["no_ask"])) + Decimal("0.08")))
    elif scenario == "MISSING_FEED" and rows:
        rows[0]["yes_ask"] = None
    elif scenario == "EXTREME_MOVE":
        for row in rows:
            row["outcome"] = (
                "no" if Decimal(str(row["yes_probability"])) >= Decimal("0.5") else "yes"
            )
    elif scenario == "CORRELATED_MARKETS":
        for row in rows:
            row["outcome"] = "no"
    elif scenario == "REGIME_CHANGE":
        for row in rows[len(rows) // 2 :]:
            row["outcome"] = "no" if row.get("outcome") == "yes" else "yes"


def _fill_fraction(scenario: str, index: int) -> Decimal:
    if scenario == "REJECTED_FILLS":
        return Decimal("0")
    if scenario == "PARTIAL_FILLS":
        return Decimal("0.5")
    if scenario == "QUEUE_POSITION":
        return Decimal("0.25") if index % 2 == 0 else Decimal("0")
    return Decimal("1")


def _price_stress(scenario: str) -> Decimal:
    return {"LATENCY": Decimal("0.05"), "WIDENED_SPREAD": Decimal("0.02")}.get(
        scenario, Decimal("0")
    )


def _result(errors, trades, leakage, scenario, input_count=0):
    net = [Decimal(str(row["net_pnl"])) for row in trades]
    gross = sum((Decimal(str(row["gross_pnl"])) for row in trades), Decimal("0"))
    total_net = sum(net, Decimal("0"))
    exposure = sum((Decimal(str(row["exposure"])) for row in trades), Decimal("0"))
    peak = cumulative = drawdown = Decimal("0")
    for value in net:
        cumulative += value
        peak = max(peak, cumulative)
        drawdown = max(drawdown, peak - cumulative)
    brier = (
        sum(
            (float(row["probability"]) - (1 if row["outcome"] == "yes" else 0)) ** 2
            for row in trades
        )
        / len(trades)
        if trades
        else None
    )
    result = {
        "schema": SCHEMA,
        "scenario": scenario,
        "verdict": "PASS" if not leakage else "REFUSE",
        "errors": errors,
        "leakage_violations": leakage,
        "input_count": input_count,
        "trade_count": len(trades),
        "gross_pnl": str(gross),
        "net_pnl": str(total_net),
        "max_drawdown": str(drawdown),
        "calibration_brier": brier,
        "turnover": str(exposure),
        "fill_rate": len(trades) / input_count if input_count else 0.0,
        "exposure": str(exposure),
        "trades": trades,
        "persisted": False,
        "safety": _safety(),
    }
    result["report_sha256"] = _digest(result)
    return result


def _safety() -> dict[str, bool]:
    return {
        "offline_only": True,
        "database_session": False,
        "persistence": False,
        "network_access": False,
        "runtime_write": False,
        "service_control": False,
        "paper_order_creation": False,
        "demo_execution": False,
        "live_execution": False,
        "autopilot": False,
    }
