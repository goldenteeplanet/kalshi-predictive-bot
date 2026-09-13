"""Original-bound current forecast intake; pure research, never paper admission."""

from __future__ import annotations

import json
from datetime import datetime

from kalshi_predictor.crypto.cf_process_inputs import _json, _time, digest, estimate_cf_process
from kalshi_predictor.crypto.settlement_average_model import forecast_benchmark_average
from kalshi_predictor.crypto.settlement_target import SettlementBenchmarkTarget

MODEL = "crypto_settlement_average_research_v1"
VERSION = "CURRENT_CF_RESEARCH_INTAKE_V1"


def target_binding(target: SettlementBenchmarkTarget, *, as_of: datetime) -> str:
    """Binds original market/rule bytes and every declared computational assumption."""
    value = target.validate(as_of=as_of, include_sol_rule_binding=True)
    return digest(json.dumps(value, sort_keys=True, default=str).encode())


def prepare_current_research_forecast(
    *, target: SettlementBenchmarkTarget, cf_original: bytes, cf_receipt: bytes,
    protocol_original: bytes, protocol_sha256: str, as_of: datetime,
) -> dict:
    """Derive one forecast from current CF originals under a predeclared protocol.

The protocol must be durably preserved by the caller before its only permitted
CF GET. Internal receipt clocks are provenance, not external attestation. This
function neither fetches nor persists, and creates no calibrated/model release.
"""
    protocol = _json(protocol_original, protocol_sha256)
    if set(protocol) != {
        "version", "model", "target_sha256", "declared_at", "not_before",
        "not_after", "max_cf_gets", "retries", "scope",
    } or (
        protocol["version"] != VERSION or protocol["model"] != MODEL
        or type(protocol["max_cf_gets"]) is not int or protocol["max_cf_gets"] != 1
        or type(protocol["retries"]) is not int or protocol["retries"] != 0
        or protocol["scope"] != "UNCALIBRATED_CURRENT_RESEARCH_ONLY"
    ):
        raise ValueError("CURRENT_RESEARCH_PROTOCOL_INVALID")
    binding = target_binding(target, as_of=as_of)
    if protocol["target_sha256"] != binding:
        raise ValueError("CURRENT_RESEARCH_TARGET_BINDING")
    receipt = _json(cf_receipt, digest(cf_receipt))
    if receipt.get("profile") != "LATEST_1HZ":
        raise ValueError("CURRENT_RESEARCH_LATEST_PROFILE_REQUIRED")
    declared, start, end, requested = (
        _time(protocol["declared_at"]), _time(protocol["not_before"]),
        _time(protocol["not_after"]), _time(receipt["requested_at"]),
    )
    if not declared <= start <= requested <= as_of < end or (end-start).total_seconds() > 60:
        raise ValueError("CURRENT_RESEARCH_PROSPECTIVE_CLOCK")
    market = json.loads(target.market_original)["market"]
    # Research must be prospective to its observation, not gated by a payout
    # deadline. Guarded paper separately requires the certified final bound in
    # overnight_paper.timing; expected/close timestamps never satisfy that gate.
    observation = _time(market["close_time"])
    if market.get("status") not in ("open", "active") or not (
        0 < (observation-as_of).total_seconds() <= 72*3600
        and 0 <= (as_of-target.market_received_at).total_seconds() <= 300
    ):
        raise ValueError("CURRENT_RESEARCH_MARKET_FRESHNESS_OR_HORIZON")
    estimate = estimate_cf_process(
        cf_original, cf_receipt, source_sha256=digest(cf_original),
        receipt_sha256=digest(cf_receipt), target=target, as_of=as_of,
    )
    forecast = forecast_benchmark_average(target, estimate.process, as_of=as_of)
    record = {
        "version": VERSION, "model": MODEL, "ticker": target.rules.market_ticker,
        "event": target.event_ticker, "probability_yes": str(forecast["probability"]),
        "forecast_at": as_of.isoformat(), "target_sha256": binding,
        "protocol_sha256": protocol_sha256, "cf_sha256": digest(cf_original),
        "cf_receipt_sha256": digest(cf_receipt), "forecast": forecast,
        "process_evidence": estimate.evidence, "scope": "CURRENT_UNCALIBRATED_RESEARCH",
        "calibrated": False, "rule_certified": False, "paper_eligible": False,
        "execution_authority": False, "database_writes": 0,
        "external_timestamp_attestation": False,
        "research_horizon_basis": "ORIGINAL_BOUND_OBSERVATION_CLOSE",
        "research_observation_time": observation.isoformat(),
        "paper_horizon_verified": False,
        "paper_horizon_blocker": "CERTIFIED_FINAL_SETTLEMENT_BOUND_REQUIRED",
    }
    record["research_forecast_id"] = digest(
        json.dumps(record, sort_keys=True, default=str).encode())
    return record
