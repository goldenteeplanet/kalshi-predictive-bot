"""Pure prospective paired-source research; no readiness or purchase authorization.

Original hashes and visibility clocks are checked here. Provider authenticity,
dependency-cluster design and externally attested collection remain separate
review obligations. Point-estimate improvement is not statistical significance.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import ROUND_CEILING, Decimal
from typing import Any

from kalshi_predictor.evaluation.calibration import calibration_bins
from kalshi_predictor.evaluation.metrics import brier_score, log_loss
from kalshi_predictor.kalshi.protocol_math import DEFAULT_TAKER_RATE
from kalshi_predictor.opportunities.scoring import score_liquidity
from kalshi_predictor.overnight_paper.books import qualify_book
from kalshi_predictor.overnight_paper.provenance import Artifact, canonical_hash
from kalshi_predictor.overnight_paper.qualification import PUBLIC_BASE, compute_net_ev
from kalshi_predictor.overnight_paper.source_health import aware

_COHORT_KEYS = (
    "event_id",
    "independent_event_id",
    "ticker",
    "snapshot_id",
    "snapshot_sha256",
    "decision_at",
    "model_name",
    "model_version",
    "model_artifact_sha256",
    "model_kind",
    "training_cutoff",
    "model_frozen_at",
    "rule_version",
    "rule_sha256",
    "execution_policy_sha256",
    "side",
    "executable_price",
    "estimated_fee",
    "trade_fee",
    "rounding_allowance",
    "slippage",
    "uncertainty",
    "event_window_start",
    "event_window_end",
)

FEE_ROUNDING_SOURCE_URL = "https://docs.kalshi.com/getting_started/fee_rounding"
CONSERVATIVE_FEE_MODEL = "kalshi-quadratic-taker-cent-aligned-single-fill-v1"


def conservative_single_fill_fees(price: Decimal, multiplier: Decimal) -> dict[str, Decimal]:
    """Conservative research assumption: cent balance, one buy, no old accumulator.

    The official unversioned fee-rounding document specifies ceil6dp trade fees
    then balance-grid alignment. Account membership is unknown here; .01 is an
    explicit conservative assumption, not evidence about the user's account.
    Zero initial accumulator produces no first-fill rebate. Do not apply this
    formula to multiple fills, sell orders or actual account reconciliation.
    """
    if (
        not isinstance(price, Decimal)
        or not isinstance(multiplier, Decimal)
        or not price.is_finite()
        or not multiplier.is_finite()
        or not 0 <= price <= 1
        or multiplier < 0
    ):
        raise ValueError("TOURNAMENT_FEE_INPUT_INVALID")
    trade_fee = (DEFAULT_TAKER_RATE * multiplier * price * (1 - price)).quantize(
        Decimal("0.000001"), rounding=ROUND_CEILING
    )
    debit = (price + trade_fee).quantize(Decimal("0.01"), rounding=ROUND_CEILING)
    rounding = debit - price - trade_fee
    return dict(
        trade_fee=trade_fee,
        rounding_allowance=rounding,
        estimated_fee=debit - price,
        total_debit=debit,
    )


@dataclass(frozen=True)
class PairedForecast:
    anchor: Artifact
    source_off: Artifact
    source_on: Artifact
    outcome: Artifact | None
    features: tuple[Artifact, ...]
    context_originals: tuple[Artifact, ...]


@dataclass(frozen=True)
class TournamentEvaluation:
    status: str
    blockers: tuple[str, ...]
    independent_event_n: int | None = None
    paired_decision_n: int | None = None
    purged_decision_ids: tuple[str, ...] = ()
    selected_decision_ids: tuple[str, ...] = ()
    metrics: dict[str, float | int] | None = None
    measurements: dict[str, float | int | None] = field(
        default_factory=lambda: {
            "monthly_cost_usd": None,
            "latency_p95_ms": None,
            "failure_rate": None,
            "coverage_fraction": None,
        }
    )
    value_score: None = None
    purchase_verdict: str = "UNAVAILABLE_PENDING_STATISTICAL_AND_ECONOMIC_REVIEW"
    actual_shadow_pnl: None = None
    actual_paper_pnl: None = None
    verified_hashes: tuple[str, ...] = ()


def _number(value: Any, *, maximum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        raise ValueError("TOURNAMENT_NUMBER_INVALID")
    result = float(value)
    if not math.isfinite(result) or result < 0 or (maximum is not None and result > maximum):
        raise ValueError("TOURNAMENT_NUMBER_INVALID")
    return result


def _decode(artifact: Artifact, hashes: set[str]) -> dict[str, Any]:
    if type(artifact) is not Artifact or not 0 < len(artifact.payload) <= 1_048_576:
        raise ValueError("TOURNAMENT_ORIGINAL_INVALID")
    row = artifact.decode()
    if not isinstance(row, dict):
        raise ValueError("TOURNAMENT_ORIGINAL_INVALID")
    hashes.add(artifact.sha256)
    return row


def _policy(row: dict[str, Any]) -> None:
    if (
        row["kind"] != "paired-source-policy-v1"
        or not isinstance(row["source_id"], str)
        or not row["source_id"]
    ):
        raise ValueError("TOURNAMENT_POLICY_INVALID")
    if not aware(row["committed_at"]) < aware(row["holdout_start"]) < aware(row["holdout_end"]):
        raise ValueError("TOURNAMENT_POLICY_NOT_PRECOMMITTED")
    for key in ("minimum_independent_events", "calibration_bin_count"):
        if type(row[key]) is not int or not 1 <= row[key] <= 10_000:
            raise ValueError("TOURNAMENT_SAMPLE_POLICY_REQUIRED")
    if row["calibration_bin_count"] > 100:
        raise ValueError("TOURNAMENT_BIN_LIMIT")
    for key in (
        "minimum_brier_improvement",
        "minimum_log_loss_improvement",
        "minimum_ece_improvement",
        "minimum_mean_net_ev_delta",
        "minimum_mean_counterfactual_pnl_delta",
        "opportunity_minimum_net_ev",
    ):
        _number(row[key])
    _number(row["maximum_source_on_ece"], maximum=1)


def _decimal(value: Any, *, maximum: float | None = 1) -> Decimal:
    _number(value, maximum=maximum)
    return Decimal(str(value))


def _captured_payload(
    original: dict[str, Any], kind: str, at: datetime, max_age: int
) -> dict[str, Any]:
    if (
        original["kind"] != kind
        or not isinstance(original["provider_payload"], dict)
        or canonical_hash(original["provider_payload"]) != original["provider_payload_sha256"]
        or not aware(original["received_at"]) <= aware(original["available_at"]) <= at
        or not 0 <= (at - aware(original["received_at"])).total_seconds() <= max_age
    ):
        raise ValueError("TOURNAMENT_EXECUTION_ORIGINAL_INVALID")
    return original["provider_payload"]


def _execution_context(
    anchor: dict[str, Any],
    snapshot: dict[str, Any],
    rule: dict[str, Any],
    contexts: dict[str, dict[str, Any]],
    policy: dict[str, Any],
    at: datetime,
) -> None:
    """Reproduce one-contract taker costs at decision time, never replay time.

    Captured market/series metadata supplies tick, liquidity and fee inputs;
    immutable research policy supplies slippage/uncertainty assumptions. This
    is executable-book research evidence, not Phase 3M/3N or trading authority.
    """
    if anchor["execution_policy_sha256"] != policy["execution_policy_sha256"]:
        raise ValueError("TOURNAMENT_EXECUTION_POLICY_MISMATCH")
    execution = contexts[anchor["execution_policy_sha256"]]
    max_age = execution["max_quote_age_seconds"]
    if (
        execution["kind"] != "execution-policy-v1"
        or execution["fee_model"] != CONSERVATIVE_FEE_MODEL
        or execution["cost_assumption"] != "CONSERVATIVE_CENT_BALANCE_SINGLE_FILL"
        or _decimal(execution["balance_precision"]) != Decimal("0.01")
        or _decimal(execution["starting_rounding_accumulator"]) != 0
        or _decimal(execution["rebate_assumption"]) != 0
        or type(execution["contracts"]) is not int
        or execution["contracts"] != 1
        or type(max_age) is not int
        or not 0 < max_age <= 60
        or not aware(execution["committed_at"]) <= aware(policy["committed_at"])
        or not aware(rule["available_at"]) <= at
    ):
        raise ValueError("TOURNAMENT_EXECUTION_POLICY_INVALID")
    fee_original = contexts[execution["fee_rounding_original_sha256"]]
    fee_bytes = bytes.fromhex(fee_original["raw_payload_hex"])
    if (
        fee_original["kind"] != "fee-rounding-original-v1"
        or fee_original["source_url"] != FEE_ROUNDING_SOURCE_URL
        or fee_original["source_version"] != "UNVERSIONED_DOCUMENT_CAPTURE"
        or execution["fee_interpretation_version"] != "ceil6dp-cent-buy-zero-accumulator-v1"
        or not 0 < len(fee_bytes) <= 500_000
        or hashlib.sha256(fee_bytes).hexdigest() != fee_original["raw_payload_sha256"]
        or not aware(fee_original["received_at"]) <= aware(execution["committed_at"])
    ):
        raise ValueError("TOURNAMENT_FEE_DOCUMENT_BINDING_INVALID")
    max_spread = _decimal(execution["max_spread"])
    market_payload = _captured_payload(
        contexts[rule["market_original_sha256"]], "market-original-v1", at, max_age
    )
    series_payload = _captured_payload(
        contexts[rule["series_original_sha256"]], "series-original-v1", at, max_age
    )
    market, series = market_payload["market"], series_payload["series"]
    if (
        market["ticker"] != anchor["ticker"]
        or market["event_ticker"] != anchor["event_id"]
        or market["status"] not in {"open", "active"}
        or not at < aware(market["close_time"])
        or series["ticker"] != rule["series_ticker"]
        or ("series_ticker" in market and market["series_ticker"] != series["ticker"])
        or series["fee_type"] != "quadratic"
        or contexts[rule["market_original_sha256"]]["request_url"]
        != f"{PUBLIC_BASE}/markets/{anchor['ticker']}"
        or contexts[rule["series_original_sha256"]]["request_url"]
        != f"{PUBLIC_BASE}/series/{rule['series_ticker']}"
        or snapshot["request_url"] != f"{PUBLIC_BASE}/markets/{anchor['ticker']}/orderbook"
        or snapshot["clock_basis"] != "public_rest_receipt"
        or not isinstance(snapshot["provider_payload"], dict)
        or canonical_hash(snapshot["provider_payload"]) != snapshot["provider_payload_sha256"]
        or not 0 <= (at - aware(snapshot["captured_at"])).total_seconds() <= max_age
    ):
        raise ValueError("TOURNAMENT_BOOK_OR_FEE_ORIGINAL_INVALID")
    if anchor["side"] not in {"BUY_YES", "BUY_NO"}:
        raise ValueError("TOURNAMENT_SIDE_INVALID")
    ranges = market["price_ranges"]
    if not isinstance(ranges, list) or not ranges or any(not isinstance(x, dict) for x in ranges):
        raise ValueError("TOURNAMENT_ORIGINAL_TICK_RULES_REQUIRED")
    book = qualify_book(
        snapshot["provider_payload"],
        received_at=aware(snapshot["captured_at"]),
        now=at,
        max_spread=max_spread,
        liquidity_score=score_liquidity(
            volume=market["volume_fp"],
            open_interest=market["open_interest_fp"],
            liquidity=market["liquidity_dollars"],
        ),
        price_ranges=ranges,
    )
    selected = book["sides"]["YES" if anchor["side"] == "BUY_YES" else "NO"]
    if not selected["executable"]:
        raise ValueError("TOURNAMENT_SELECTED_SIDE_NOT_EXECUTABLE")
    price = _decimal(anchor["executable_price"])
    fees = conservative_single_fill_fees(price, _decimal(series["fee_multiplier"], maximum=None))
    if (
        price != Decimal(selected["ask"])
        or any(
            _decimal(anchor[key]) != fees[key]
            for key in ("estimated_fee", "trade_fee", "rounding_allowance")
        )
        or _decimal(anchor["slippage"]) != _decimal(execution["slippage"])
        or _decimal(anchor["uncertainty"]) != _decimal(execution["uncertainty"])
    ):
        raise ValueError("TOURNAMENT_EXECUTABLE_COST_BINDING_INVALID")


def _pair(
    pair: PairedForecast, policy: dict[str, Any], hashes: set[str], now: datetime
) -> dict[str, Any]:
    anchor = _decode(pair.anchor, hashes)
    decision_id = canonical_hash(anchor)
    at = aware(anchor["decision_at"])
    contexts = {a.sha256: _decode(a, hashes) for a in pair.context_originals}
    if len(contexts) != len(pair.context_originals):
        raise ValueError("TOURNAMENT_DUPLICATE_CONTEXT")
    snapshot = contexts[anchor["snapshot_sha256"]]
    model = contexts[anchor["model_artifact_sha256"]]
    rule = contexts[anchor["rule_sha256"]]
    if (
        snapshot["id"] != anchor["snapshot_id"]
        or snapshot["ticker"] != anchor["ticker"]
        or not aware(snapshot["captured_at"]) <= aware(snapshot["available_at"]) <= at
        or model["name"] != anchor["model_name"]
        or model["version"] != anchor["model_version"]
        or model["model_kind"] != anchor["model_kind"]
        or model["training_cutoff"] != anchor["training_cutoff"]
        or model["frozen_at"] != anchor["model_frozen_at"]
        or any(rule[k] != anchor[k] for k in ("event_id", "ticker", "rule_version"))
    ):
        raise ValueError("TOURNAMENT_CONTEXT_BINDING_INVALID")
    _execution_context(anchor, snapshot, rule, contexts, policy, at)
    if not aware(policy["holdout_start"]) <= at < aware(policy["holdout_end"]) or at > now:
        raise ValueError("TOURNAMENT_COHORT_OUTSIDE_HOLDOUT")
    if not aware(anchor["event_window_start"]) <= at < aware(anchor["event_window_end"]):
        raise ValueError("TOURNAMENT_EVENT_WINDOW_INVALID")
    if (
        not anchor["independent_event_id"]
        or anchor["model_artifact_sha256"] != policy["model_artifact_sha256"]
    ):
        raise ValueError("TOURNAMENT_MODEL_OR_EVENT_BINDING_INVALID")
    if anchor["model_kind"] == "fixed_heuristic":
        if anchor["training_cutoff"] is not None:
            raise ValueError("TOURNAMENT_HEURISTIC_HAS_NO_TRAINING_CUTOFF")
        cutoff = aware(anchor["model_frozen_at"])
    elif anchor["model_kind"] == "trained":
        cutoff = aware(anchor["training_cutoff"])
    else:
        raise ValueError("TOURNAMENT_MODEL_KIND_INVALID")
    if not cutoff <= aware(anchor["model_frozen_at"]) <= aware(policy["committed_at"]):
        raise ValueError("TOURNAMENT_MODEL_NOT_FROZEN_BEFORE_HOLDOUT")
    features = {artifact.sha256: _decode(artifact, hashes) for artifact in pair.features}
    if len(features) != len(pair.features):
        raise ValueError("TOURNAMENT_DUPLICATE_FEATURE")
    for feature in features.values():
        source = contexts[feature["source_original_sha256"]]
        generated = aware(feature["generated_at"])
        if (
            source["kind"] != "source-original-v1"
            or source["source_id"] != feature["source_id"]
            or not aware(source["available_at"]) <= aware(source["received_at"]) <= generated
            or canonical_hash(source["provider_payload"]) != source["provider_payload_sha256"]
        ):
            raise ValueError("TOURNAMENT_FEATURE_SOURCE_BINDING_INVALID")
        if (
            feature["kind"] != "feature-v1"
            or not aware(feature["observed_at"])
            <= generated
            <= aware(feature["available_at"])
            <= at
        ):
            raise ValueError("TOURNAMENT_FUTURE_FEATURE")
    probabilities = []
    feature_sets = []
    for enabled, artifact in ((False, pair.source_off), (True, pair.source_on)):
        row = _decode(artifact, hashes)
        if (
            row["kind"] != "paired-forecast-v1"
            or row["source_enabled"] is not enabled
            or row["source_id"] != policy["source_id"]
            or row["anchor_sha256"] != pair.anchor.sha256
            or row["decision_id"] != decision_id
            or any(row[key] != anchor[key] for key in _COHORT_KEYS)
        ):
            raise ValueError("TOURNAMENT_PAIRED_COHORT_MISMATCH")
        if (
            not cutoff <= aware(row["generated_at"]) <= at
            or not 0 <= (aware(row["recorded_at"]) - at).total_seconds() <= 60
            or aware(row["recorded_at"]) > now
        ):
            raise ValueError("TOURNAMENT_FORECAST_VISIBILITY_INVALID")
        source_hashes = row["feature_hashes"]
        if (
            not isinstance(source_hashes, list)
            or len(set(source_hashes)) != len(source_hashes)
            or any(sha not in features for sha in source_hashes)
        ):
            raise ValueError("TOURNAMENT_FEATURE_HASH_BINDING_INVALID")
        if any(
            aware(features[sha]["available_at"]) > aware(row["generated_at"])
            for sha in source_hashes
        ):
            raise ValueError("TOURNAMENT_FEATURE_AFTER_FORECAST")
        feature_sets.append(set(source_hashes))
        probabilities.append(_number(row["probability"], maximum=1))
    off, on = feature_sets
    added = on - off
    if (
        not off <= on
        or not added
        or on != set(features)
        or any(features[sha]["source_id"] == policy["source_id"] for sha in off)
        or any(features[sha]["source_id"] != policy["source_id"] for sha in added)
    ):
        raise ValueError("TOURNAMENT_SOURCE_ABLATION_INVALID")
    result: dict[str, Any] = dict(
        anchor=anchor, decision_id=decision_id, probabilities=probabilities, outcome=None
    )
    if pair.outcome is not None:
        outcome = _decode(pair.outcome, hashes)
        if canonical_hash(outcome["provider_payload"]) != outcome["provider_payload_sha256"]:
            raise ValueError("TOURNAMENT_OUTCOME_ORIGINAL_HASH_INVALID")
        original = outcome["provider_payload"]
        if (
            original["kind"] != "final-original-v1"
            or original["status"] != "final"
            or any(original[key] != outcome[key] for key in ("ticker", "event_id", "result"))
            or aware(original["final_at"]) != aware(outcome["final_at"])
            or aware(original["available_at"]) != aware(outcome["available_at"])
            or not aware(original["final_at"])
            <= aware(original["available_at"])
            <= aware(original["received_at"])
        ):
            raise ValueError("TOURNAMENT_OUTCOME_ORIGINAL_BINDING_INVALID")
        if (
            outcome["kind"] != "outcome-v1"
            or outcome["decision_id"] != decision_id
            or any(outcome[k] != anchor[k] for k in ("event_id", "ticker", "rule_version"))
            or outcome["result"] not in {"yes", "no"}
        ):
            raise ValueError("TOURNAMENT_OUTCOME_IDENTITY_INVALID")
        if (
            not aware(anchor["event_window_end"])
            <= aware(outcome["final_at"])
            <= aware(outcome["available_at"])
        ):
            raise ValueError("TOURNAMENT_OUTCOME_CLOCK_INVALID")
        if aware(original["received_at"]) <= now:
            result["outcome"] = int(outcome["result"] == "yes")
    return result


def _measurements(
    artifact: Artifact | None, source_id: str, now: datetime, hashes: set[str]
) -> dict[str, float | int | None]:
    result: dict[str, float | int | None] = {
        "monthly_cost_usd": None,
        "latency_p95_ms": None,
        "failure_rate": None,
        "coverage_fraction": None,
    }
    if artifact is None:
        return result
    row = _decode(artifact, hashes)
    if (
        row["kind"] != "provider-measurements-v1"
        or row["source_id"] != source_id
        or aware(row["measured_at"]) > now
    ):
        raise ValueError("TOURNAMENT_MEASUREMENT_BINDING_INVALID")
    for key in ("monthly_cost_usd", "latency_p95_ms", "coverage_fraction"):
        if row.get(key) is not None:
            result[key] = _number(row[key], maximum=1 if key == "coverage_fraction" else None)
    attempts, failures = row.get("request_count"), row.get("failure_count")
    if attempts is not None or failures is not None:
        if type(attempts) is not int or type(failures) is not int or not 0 <= failures <= attempts:
            raise ValueError("TOURNAMENT_RELIABILITY_INVALID")
        result["failure_rate"] = failures / attempts if attempts else None
    return result


def _scores(rows: list[dict[str, Any]], policy: dict[str, Any]) -> dict[str, float | int]:
    outcomes = [row["outcome"] for row in rows]
    result: dict[str, float | int] = {}
    for i, variant in enumerate(("off", "on")):
        probabilities = [row["probabilities"][i] for row in rows]
        bins = calibration_bins(outcomes, probabilities, n_bins=policy["calibration_bin_count"])
        result["brier_" + variant] = brier_score(outcomes, probabilities)
        result["log_loss_" + variant] = log_loss(outcomes, probabilities)
        result["ece_" + variant] = sum(
            b.count * abs(b.avg_predicted_probability - b.observed_frequency) for b in bins
        ) / len(rows)
        evs, pnls = [], []
        opportunities = 0
        for row, probability in zip(rows, probabilities, strict=True):
            anchor = row["anchor"]
            if anchor["side"] not in {"BUY_YES", "BUY_NO"}:
                raise ValueError("TOURNAMENT_SIDE_INVALID")
            yes = anchor["side"] == "BUY_YES"
            cost = sum(
                _number(anchor[key], maximum=1)
                for key in ("executable_price", "estimated_fee", "slippage")
            )
            ev = float(
                compute_net_ev(
                    model_probability=Decimal(str(probability))
                    if yes
                    else Decimal("1") - Decimal(str(probability)),
                    executable_price=_decimal(anchor["executable_price"]),
                    estimated_fee=_decimal(anchor["estimated_fee"]),
                    slippage_allowance=_decimal(anchor["slippage"]),
                    uncertainty_buffer=_decimal(anchor["uncertainty"]),
                ).net_ev
            )
            admitted = ev > _number(policy["opportunity_minimum_net_ev"])
            opportunities += int(admitted)
            evs.append(ev)
            pnls.append((int(row["outcome"] == int(yes)) - cost) if admitted else 0.0)
        result["mean_net_ev_" + variant] = sum(evs) / len(rows)
        result["opportunities_" + variant] = opportunities
        result["mean_counterfactual_policy_pnl_" + variant] = sum(pnls) / len(rows)
    for score in ("brier", "log_loss", "ece"):
        result[score + "_improvement"] = result[score + "_off"] - result[score + "_on"]
    for score in ("mean_net_ev", "opportunities", "mean_counterfactual_policy_pnl"):
        result[score + "_delta"] = result[score + "_on"] - result[score + "_off"]
    return result


def evaluate_tournament(
    *,
    policy: Artifact,
    pairs: tuple[PairedForecast, ...],
    as_of: datetime,
    measurements: Artifact | None = None,
) -> TournamentEvaluation:
    hashes: set[str] = set()
    try:
        now = aware(as_of)
        frozen = _decode(policy, hashes)
        _policy(frozen)
        if aware(frozen["committed_at"]) > now:
            raise ValueError("TOURNAMENT_POLICY_NOT_YET_COMMITTED")
        measured = _measurements(measurements, frozen["source_id"], now, hashes)
        if len(pairs) > 10_000:
            raise ValueError("TOURNAMENT_COHORT_LIMIT")
        rows = [_pair(pair, frozen, hashes, now) for pair in pairs]
        if len({row["decision_id"] for row in rows}) != len(rows):
            raise ValueError("TOURNAMENT_DUPLICATE_DECISION")
        selected: list[dict[str, Any]] = []
        purged = []
        for row in sorted(
            rows, key=lambda row: (aware(row["anchor"]["decision_at"]), row["decision_id"])
        ):
            anchor = row["anchor"]
            if any(
                anchor["independent_event_id"] == other["anchor"]["independent_event_id"]
                or (
                    aware(anchor["event_window_start"]) < aware(other["anchor"]["event_window_end"])
                    and aware(other["anchor"]["event_window_start"])
                    < aware(anchor["event_window_end"])
                )
                for other in selected
            ):
                purged.append(row["decision_id"])
            else:
                selected.append(row)
        reasons = []
        if now < aware(frozen["holdout_end"]):
            reasons.append("HOLDOUT_WINDOW_NOT_COMPLETE")
        if any(row["outcome"] is None for row in selected):
            reasons.append("PAIRED_OUTCOMES_INCOMPLETE")
        if len(selected) < frozen["minimum_independent_events"]:
            reasons.append("INSUFFICIENT_INDEPENDENT_EVENTS")
        common = TournamentEvaluation(
            status="NOT_ENOUGH_DATA",
            blockers=tuple(reasons),
            independent_event_n=len(selected),
            paired_decision_n=len(rows),
            purged_decision_ids=tuple(purged),
            selected_decision_ids=tuple(row["decision_id"] for row in selected),
            measurements=measured,
            verified_hashes=tuple(sorted(hashes)),
        )
        if reasons:
            return common
        metrics = _scores(selected, frozen)
        thresholds = {
            "brier_improvement": "minimum_brier_improvement",
            "log_loss_improvement": "minimum_log_loss_improvement",
            "ece_improvement": "minimum_ece_improvement",
            "mean_net_ev_delta": "minimum_mean_net_ev_delta",
            "mean_counterfactual_policy_pnl_delta": "minimum_mean_counterfactual_pnl_delta",
        }
        failed = [
            key.upper() + "_BELOW_POLICY"
            for key, threshold in thresholds.items()
            if metrics[key] < _number(frozen[threshold])
        ]
        if metrics["ece_on"] > _number(frozen["maximum_source_on_ece"], maximum=1):
            failed.append("CALIBRATION_ABOVE_POLICY")
        return replace(
            common,
            status="POINT_ESTIMATES_FAIL_POLICY"
            if failed
            else "POINT_ESTIMATES_MEET_POLICY_REVIEW_REQUIRED",
            blockers=tuple(failed),
            metrics=metrics,
        )
    except (ValueError, TypeError, KeyError, AttributeError, ArithmeticError, RecursionError):
        return TournamentEvaluation(
            "INVALID_EVIDENCE",
            ("TOURNAMENT_EVIDENCE_INVALID",),
            verified_hashes=tuple(sorted(hashes)),
        )
