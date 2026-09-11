"""Explicit v3 analysis route; never inserts admission forecasts or paper decisions."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from kalshi_predictor.crypto.settlement_target import SettlementBenchmarkTarget
from kalshi_predictor.forecasting.crypto_v3_independent import (
    CryptoTarget,
    PriceObservation,
    compare_execution,
    forecast_independent,
)
from kalshi_predictor.forecasting.model_roles import research_model_role


@dataclass(frozen=True)
class ResearchExecutionScenario:
    yes_bid: float
    yes_ask: float
    yes_fee: float
    no_fee: float
    slippage: float
    uncertainty: float


@dataclass(frozen=True)
class CryptoResearchAnalysis:
    # Immutable bytes, deliberately not ForecastOutput or PreparedCandidate.
    payload: bytes

    def decode(self) -> dict:
        return json.loads(self.payload)


def analyze_crypto_research(
    *,
    prices: Sequence[PriceObservation],
    proxy_target: CryptoTarget,
    decision_at: datetime,
    scenario: ResearchExecutionScenario | None = None,
    settlement_target: SettlementBenchmarkTarget | None = None,
) -> CryptoResearchAnalysis:
    target_metadata = None
    if settlement_target is not None:
        if type(settlement_target) is not SettlementBenchmarkTarget:
            raise ValueError("EXACT_SETTLEMENT_TARGET_REQUIRED")
        target_metadata = settlement_target.validate(as_of=decision_at)
        if (
            settlement_target.symbol != proxy_target.symbol
            or settlement_target.rules.index_id != proxy_target.benchmark
            or settlement_target.rules.rule_sha256 != proxy_target.rule_sha256
            or settlement_target.rules.closing.end_ms
            != int(proxy_target.observation_at.timestamp() * 1000)
            or settlement_target.comparator != proxy_target.comparator
        ):
            raise ValueError("PROXY_TARGET_METADATA_MISMATCH")
        for name in ("threshold", "lower", "upper"):
            original = getattr(settlement_target, name)
            proxy = getattr(proxy_target, name)
            if (original is None) != (proxy is None) or (
                original is not None and Decimal(str(proxy)) != original
            ):
                raise ValueError("PROXY_STRIKE_MISMATCH")
    forecast = forecast_independent(prices, proxy_target, decision_at=decision_at)
    comparisons = {}
    if scenario is not None:
        if type(scenario) is not ResearchExecutionScenario:
            raise ValueError("EXACT_SCENARIO_REQUIRED")
        for name, row in forecast["comparisons"].items():
            if row["probability"] is None:
                continue
            comparisons[name] = compare_execution(
                row["probability"],
                yes_bid=scenario.yes_bid,
                yes_ask=scenario.yes_ask,
                yes_fee=scenario.yes_fee,
                no_fee=scenario.no_fee,
                slippage=scenario.slippage,
                uncertainty=scenario.uncertainty,
            )
    value = dict(
        schema="crypto-research-analysis-v1",
        model_role=research_model_role(forecast["model"]),
        forecast=forecast,
        declared_settlement_target=target_metadata,
        scenario_comparisons=comparisons,
        status="UNRECORDED_RESEARCH_ANALYSIS",
        comparison_authority="CALLER_SCENARIO_NOT_VERIFIED_BOOK_OR_COST",
        settlement_alignment="TERMINAL_PROXY_NOT_BENCHMARK_AVERAGE_FORECAST",
        first_blocker="SETTLEMENT_ALIGNMENT",
        paper_eligible=False,
        execution_authority=False,
        prediction_recorded_at=None,
        shadow_decision_written=False,
        blockers=[
            "SETTLEMENT_AVERAGE_PREDICTIVE_ENGINE_MISSING",
            "BENCHMARK_BASIS_UNCALIBRATED",
            "ORIGINAL_RULE_SEMANTICS_NOT_CERTIFIED",
            "VERIFIED_EXECUTION_COST_EVIDENCE_REQUIRED",
            "CALIBRATION_AND_MODEL_RELEASE_REQUIRED",
        ],
    )
    return CryptoResearchAnalysis(
        json.dumps(value, sort_keys=True, default=str, allow_nan=False).encode()
    )
