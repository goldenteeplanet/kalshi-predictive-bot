"""Offline portfolio correlated-exposure and joint-tail loss proof."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import UTC, datetime
from decimal import Decimal

SCHEMA = "phase4nh.portfolio-joint-tail-risk.v1"
GROUP_FIELDS = (
    "event_group",
    "underlying",
    "geography",
    "time_window",
    "data_source",
    "model_family",
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


def analyze_portfolio(
    snapshot: object,
    proposed_trades: object,
    scenarios: object,
    *,
    evaluated_at: str,
    maximum_snapshot_age_seconds: int,
    maximum_joint_loss: str,
    maximum_concentration: str,
) -> dict[str, object]:
    errors: list[str] = []
    now = _time(evaluated_at)
    if (
        not isinstance(snapshot, dict)
        or not isinstance(proposed_trades, list)
        or not isinstance(scenarios, list)
        or now is None
    ):
        return _result(["INPUT_INVALID"], [])
    captured = _time(snapshot.get("captured_at"))
    if (
        captured is None
        or captured > now
        or (now - captured).total_seconds() > maximum_snapshot_age_seconds
    ):
        errors.append("STALE_OR_INVALID_PORTFOLIO_SNAPSHOT")
    positions = snapshot.get("positions")
    if not isinstance(positions, list):
        errors.append("POSITIONS_MISSING")
        positions = []
    ordered_proposed = sorted(proposed_trades, key=lambda row: str(row.get("position_id")))
    combined = [*positions, *ordered_proposed]
    ids: set[str] = set()
    signatures: dict[str, int] = {}
    valid = []
    for index, row in enumerate(combined):
        row_errors = []
        if not isinstance(row, dict):
            errors.append(f"POSITION_{index}_INVALID")
            continue
        position_id = str(row.get("position_id"))
        if position_id in ids:
            row_errors.append("DUPLICATE_POSITION_OR_ORDER")
        ids.add(position_id)
        if any(row.get(field) is None for field in GROUP_FIELDS) or not isinstance(
            row.get("factor_exposures"), dict
        ):
            row_errors.append("GROUP_OR_FACTOR_FIELDS_MISSING")
        if index >= len(positions) and (
            row.get("pessimistic_fill_verified") is not True
            or row.get("worst_case_fee_verified") is not True
        ):
            row_errors.append("PROPOSED_TRADE_NOT_PESSIMISTICALLY_VERIFIED")
        signature = str(row.get("economic_exposure_signature"))
        signatures[signature] = signatures.get(signature, 0) + 1
        try:
            Decimal(str(row.get("expected_pnl")))
            Decimal(str(row.get("worst_case_fee")))
            {key: Decimal(str(value)) for key, value in row.get("factor_exposures", {}).items()}
        except Exception:
            row_errors.append("POSITION_ECONOMICS_INVALID")
        if row_errors:
            errors.extend(f"POSITION_{index}_{error}" for error in sorted(set(row_errors)))
        else:
            valid.append(row)
    duplicated_signatures = sorted(
        key for key, count in signatures.items() if key != "None" and count > 1
    )
    factor_totals: dict[str, Decimal] = {}
    for row in valid:
        for factor, exposure in row["factor_exposures"].items():
            factor_totals[factor] = factor_totals.get(factor, Decimal("0")) + Decimal(str(exposure))
    scenario_rows = []
    for scenario in scenarios:
        if not isinstance(scenario, dict) or not isinstance(scenario.get("factor_shocks"), dict):
            errors.append("SCENARIO_INVALID")
            continue
        pnl = Decimal("0")
        contributions = {}
        hedge_failure = scenario.get("hedge_failure") is True
        for row in valid:
            value = Decimal(str(row["expected_pnl"])) - Decimal(str(row["worst_case_fee"]))
            for factor, exposure in row["factor_exposures"].items():
                shock = Decimal(str(scenario["factor_shocks"].get(factor, 0)))
                contribution = Decimal(str(exposure)) * shock
                if hedge_failure and contribution > 0:
                    contribution = Decimal("0")
                value += contribution
            pnl += value
            contributions[row["position_id"]] = str(value)
        scenario_rows.append(
            {"name": scenario.get("name"), "pnl": str(pnl), "position_contributions": contributions}
        )
    scenario_pnls = sorted(Decimal(row["pnl"]) for row in scenario_rows)
    worst_pnl = min(scenario_pnls, default=Decimal("0"))
    tail_count = max(1, math.ceil(len(scenario_pnls) * 0.05)) if scenario_pnls else 1
    expected_shortfall = (
        sum(scenario_pnls[:tail_count], Decimal("0")) / tail_count
        if scenario_pnls
        else Decimal("0")
    )
    gross_factor = sum((abs(value) for value in factor_totals.values()), Decimal("0"))
    concentration = (
        max((abs(value) for value in factor_totals.values()), default=Decimal("0")) / gross_factor
        if gross_factor
        else Decimal("0")
    )
    perfect_correlation_loss = sum(
        (min(Decimal("0"), -abs(value)) for value in factor_totals.values()), Decimal("0")
    )
    covariance_stress = (
        sum(value * value for value in factor_totals.values()).sqrt()
        if factor_totals
        else Decimal("0")
    )
    marginal = {}
    for row in valid:
        adverse = sum(
            (-abs(Decimal(str(value))) for value in row["factor_exposures"].values()), Decimal("0")
        )
        marginal[row["position_id"]] = str(
            -(Decimal(str(row["expected_pnl"])) - Decimal(str(row["worst_case_fee"])) + adverse)
        )
    max_loss = Decimal(maximum_joint_loss)
    risk_used = max(Decimal("0"), -min(worst_pnl, perfect_correlation_loss))
    remaining = max_loss - risk_used
    readiness_errors = []
    if risk_used > max_loss:
        readiness_errors.append("JOINT_TAIL_LOSS_LIMIT_BREACHED")
    if concentration > Decimal(maximum_concentration):
        readiness_errors.append("PORTFOLIO_CONCENTRATION_LIMIT_BREACHED")
    if duplicated_signatures:
        readiness_errors.append("DUPLICATED_ECONOMIC_EXPOSURE")
    result = _result(sorted(set(errors)), scenario_rows)
    result.update(
        {
            "readiness": "PASS" if not errors and not readiness_errors else "REFUSE",
            "readiness_errors": readiness_errors,
            "ordered_proposed_position_ids": [row.get("position_id") for row in ordered_proposed],
            "group_exposure": _group_exposure(valid),
            "factor_exposure": {key: str(value) for key, value in sorted(factor_totals.items())},
            "duplicated_exposure_signatures": duplicated_signatures,
            "worst_case_pnl": str(worst_pnl),
            "expected_shortfall_95": str(expected_shortfall),
            "perfect_correlation_loss": str(perfect_correlation_loss),
            "covariance_stress": str(covariance_stress),
            "concentration": str(concentration),
            "marginal_risk_contribution": marginal,
            "drawdown_consumption": str(risk_used),
            "remaining_risk_capacity": str(remaining),
            "order_authorized": False,
        }
    )
    result["portfolio_audit_sha256"] = _digest(result)
    return result


def _group_exposure(rows):
    output = {}
    for field in GROUP_FIELDS:
        values = {}
        for row in rows:
            amount = sum(
                (abs(Decimal(str(value))) for value in row["factor_exposures"].values()),
                Decimal("0"),
            )
            key = str(row[field])
            values[key] = str(Decimal(values.get(key, "0")) + amount)
        output[field] = dict(sorted(values.items()))
    return output


def _result(errors, scenarios):
    return {
        "schema": SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": errors,
        "scenario_pnl": scenarios,
        "safety": _safety(),
    }


def _safety():
    return {
        "offline_only": True,
        "order_creation": False,
        "persistence": False,
        "network_access": False,
        "runtime_write": False,
        "paper_execution": False,
        "demo_execution": False,
        "live_execution": False,
        "autopilot": False,
    }
