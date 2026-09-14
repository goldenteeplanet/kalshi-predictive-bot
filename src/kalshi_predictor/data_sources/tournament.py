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
from decimal import Decimal
from typing import Any

from kalshi_predictor.config import Settings
from kalshi_predictor.evaluation.calibration import calibration_bins
from kalshi_predictor.evaluation.metrics import brier_score, log_loss
from kalshi_predictor.kalshi.orderbook import parse_orderbook
from kalshi_predictor.kalshi.protocol_math import DEFAULT_TAKER_RATE
from kalshi_predictor.opportunities.scoring import score_liquidity
from kalshi_predictor.overnight_paper.books import qualify_book
from kalshi_predictor.overnight_paper.provenance import Artifact, canonical_hash
from kalshi_predictor.overnight_paper.qualification import PUBLIC_BASE, compute_net_ev
from kalshi_predictor.overnight_paper.source_health import aware
from kalshi_predictor.paper.fees import single_buy_fees

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
    return single_buy_fees(price, multiplier, DEFAULT_TAKER_RATE)


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
    evidence_scope: str = "HASH_BOUND_DECLARATIONS_ONLY"
    contrast_type: str = "DECLARED_SOURCE_COMPARISON"


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
        row["kind"]
        not in {
            "paired-source-policy-v1",
            "weather-paired-source-policy-v1",
            "lagged-cpi-paired-policy-v1",
        }
        or not isinstance(row["source_id"], str)
        or not row["source_id"]
    ):
        raise ValueError("TOURNAMENT_POLICY_INVALID")
    if row["kind"] == "weather-paired-source-policy-v1":
        if row["source_id"] != "NWS" or not row["procedure_sha256"]:
            raise ValueError("TOURNAMENT_WEATHER_POLICY_REQUIRED")
    elif row["kind"] == "lagged-cpi-paired-policy-v1":
        if row["source_id"] != "FRED" or not row["procedure_sha256"]:
            raise ValueError("TOURNAMENT_ECONOMIC_POLICY_REQUIRED")
    elif any(key in row for key in ("procedure_sha256", "execution_receipt_sha256")):
        raise ValueError("TOURNAMENT_EXPLICIT_WEATHER_POLICY_REQUIRED")
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
    event_payload = _captured_payload(
        contexts[rule["event_original_sha256"]], "event-original-v1", at, max_age
    )
    market, series = market_payload["market"], series_payload["series"]
    event = event_payload["event"]
    if (
        market["ticker"] != anchor["ticker"]
        or market["event_ticker"] != anchor["event_id"]
        or market["status"] not in {"open", "active"}
        or not at < aware(market["close_time"])
        or series["ticker"] != rule["series_ticker"]
        or event["event_ticker"] != anchor["event_id"]
        or event["series_ticker"] != series["ticker"]
        # Event overrides supersede series fees; unsupported overrides must
        # never silently inherit the series formula (including zero/empty).
        or event.get("fee_type_override") is not None
        or event.get("fee_multiplier_override") is not None
        or ("series_ticker" in market and market["series_ticker"] != series["ticker"])
        or series["fee_type"] != "quadratic"
        or contexts[rule["market_original_sha256"]]["request_url"]
        != f"{PUBLIC_BASE}/markets/{anchor['ticker']}"
        or contexts[rule["series_original_sha256"]]["request_url"]
        != f"{PUBLIC_BASE}/series/{rule['series_ticker']}"
        or contexts[rule["event_original_sha256"]]["request_url"]
        != f"{PUBLIC_BASE}/events/{anchor['event_id']}"
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


_WEATHER_METHODS = {
    "off": "same_book_midpoint",
    "on": "kalshi_predictor.forecasting.weather_v2:WeatherV2Forecaster.forecast",
}


def _weather_receipt(
    pair: PairedForecast,
    anchor: dict[str, Any],
    snapshot: dict[str, Any],
    model: dict[str, Any],
    contexts: dict[str, dict[str, Any]],
    features: dict[str, dict[str, Any]],
    policy: dict[str, Any],
    at: datetime,
) -> None:
    """Replay recorded evidence, not execution attestation or trading authority."""
    rows = (pair.source_off.decode(), pair.source_on.decode())
    keys = ("procedure_sha256", "execution_receipt_sha256")
    if policy["kind"] != "weather-paired-source-policy-v1":
        if any(key in row for row in (anchor, *rows) for key in keys):
            raise ValueError("TOURNAMENT_EXPLICIT_WEATHER_POLICY_REQUIRED")
        return
    if anchor["procedure_sha256"] != policy["procedure_sha256"] or any(
        row[key] != anchor[key] for row in rows for key in keys
    ):
        raise ValueError("TOURNAMENT_WEATHER_RECEIPT_BINDING_INVALID")
    if contexts[anchor["execution_policy_sha256"]]["side"] != anchor["side"]:
        raise ValueError("TOURNAMENT_WEATHER_FROZEN_SIDE_INVALID")
    procedure = contexts[anchor["procedure_sha256"]]
    receipt = contexts[anchor["execution_receipt_sha256"]]
    if (
        procedure["kind"] != "weather-paired-procedure-v1"
        or not procedure["name"]
        or not procedure["version"]
        or procedure["source_id"] != "NWS"
        or procedure["variant_methods"] != _WEATHER_METHODS
        or procedure["contrast_type"] != "MARKET_BASELINE_VS_WEATHER_V2"
        or receipt["kind"] != "weather-preparation-execution-v1"
        or receipt["procedure_sha256"] != anchor["procedure_sha256"]
        or receipt["model_artifact_sha256"] != anchor["model_artifact_sha256"]
        or procedure["model_artifact_sha256"] != anchor["model_artifact_sha256"]
        or receipt["variant_methods"] != _WEATHER_METHODS
        or receipt["contrast_type"] != procedure["contrast_type"]
        or receipt["verification_scope"]
        != "FILESYSTEM_BUNDLE_AND_IMPORTED_ORIGINS_BEFORE_AFTER_PREPARATION"
        or receipt["atomic_filesystem_immutability"] is not False
        or receipt["research_only"] is not True
        or receipt["runtime_certified"] is not False
        or receipt["ticker"] != anchor["ticker"]
        or receipt["snapshot_id"] != anchor["snapshot_id"]
    ):
        raise ValueError("TOURNAMENT_WEATHER_PROCEDURE_INVALID")
    if not (
        aware(model["created_at"])
        <= aware(model["frozen_at"])
        <= aware(model["available_at"])
        <= aware(procedure["created_at"])
        <= aware(procedure["frozen_at"])
        <= aware(procedure["available_at"])
        <= aware(policy["committed_at"])
    ):
        raise ValueError("TOURNAMENT_WEATHER_FREEZE_INVALID")
    settings = receipt["settings_original"]
    if (
        not isinstance(settings, dict)
        or set(settings) != set(Settings.model_fields)
        or canonical_hash(settings) != receipt["settings_sha256"]
        or receipt["settings_sha256"] != procedure["settings_sha256"]
        or model["parameters"] != settings
        or model["parameters_sha256"] != canonical_hash(settings)
        or model["model_kind"] != "fixed_heuristic"
        or model["name"] != "weather_v2"
        or model["training_cutoff"] is not None
        or model["training_dataset_hashes"] != []
        or model.get("training_artifacts", []) != []
        or settings["weather_v2_knyc_observation_enabled"] is not False
        or any(
            settings[key]
            for key in (
                "kalshi_api_key_id",
                "kalshi_private_key_path",
                "postgres_password",
                "execution_confirmation_token",
            )
        )
    ):
        raise ValueError("TOURNAMENT_WEATHER_SETTINGS_INVALID")
    dependencies = model["code_dependencies"]
    bundle = contexts[model["code_sha256"]]
    if (
        not isinstance(dependencies, dict)
        or not dependencies
        or model["model_entrypoint"] != _WEATHER_METHODS["on"]
        or receipt["model_code_sha256"] != model["code_sha256"]
        or any(
            receipt[key] != dependencies
            for key in ("code_dependencies", "dependencies_before", "dependencies_after")
        )
        or bundle["schema"] != "weather-model-source-bundle-v1"
        or bundle["entrypoint"] != model["model_entrypoint"]
        or not isinstance(bundle["sources"], list)
        or len(bundle["sources"]) != len(dependencies)
    ):
        raise ValueError("TOURNAMENT_WEATHER_CODE_INVALID")
    seen = set()
    for source in bundle["sources"]:
        path = source["path"]
        if (
            not isinstance(path, str)
            or not path.startswith("src/")
            or not path.endswith(".py")
            or any(part in {"", ".", ".."} for part in path.split("/"))
            or "\\" in path
            or ":" in path
            or path in seen
            or source["sha256"] != dependencies[path]
            or hashlib.sha256(source["source"].encode("utf-8")).hexdigest() != source["sha256"]
        ):
            raise ValueError("TOURNAMENT_WEATHER_CODE_SOURCE_INVALID")
        seen.add(path)
    if seen != set(dependencies):
        raise ValueError("TOURNAMENT_WEATHER_CODE_CLOSURE_INVALID")
    started = aware(receipt["preparation_started_at"])
    generated = aware(receipt["forecast_generated_at"])
    available = aware(receipt["forecast_available_at"])
    finished = aware(receipt["preparation_finished_at"])
    baseline_at = aware(receipt["source_off_generated_at"])
    receipt_at = aware(receipt["receipt_generated_at"])
    if not started <= generated <= available <= finished <= baseline_at <= receipt_at <= at:
        raise ValueError("TOURNAMENT_WEATHER_EXECUTION_CLOCK_INVALID")
    envelope_hashes = receipt["source_envelope_hashes"]
    if (
        not isinstance(envelope_hashes, list)
        or not envelope_hashes
        or len(set(envelope_hashes)) != len(envelope_hashes)
        or any(sha not in contexts for sha in envelope_hashes)
        or receipt["book_envelope_sha256"] not in envelope_hashes
        or snapshot["capture_envelope_sha256"] != receipt["book_envelope_sha256"]
    ):
        raise ValueError("TOURNAMENT_WEATHER_CAPTURE_MANIFEST_INVALID")
    envelopes = [contexts[sha] for sha in envelope_hashes]
    if len({original["url"] for original in envelopes}) != len(envelopes) or any(
        not aware(original["received_at"]) <= started for original in envelopes
    ):
        raise ValueError("TOURNAMENT_WEATHER_CAPTURE_CLOCK_INVALID")
    rule = contexts[anchor["rule_sha256"]]
    for key in ("market_original_sha256", "series_original_sha256", "event_original_sha256"):
        wrapper = contexts[rule[key]]
        capture_hash = wrapper["capture_envelope_sha256"]
        capture = contexts[capture_hash]
        if (
            capture_hash not in envelope_hashes
            or capture["body"] != wrapper["provider_payload"]
            or capture["url"] != wrapper["request_url"]
            or aware(capture["received_at"]) != aware(wrapper["received_at"])
            or aware(capture["received_at"]) != aware(wrapper["available_at"])
        ):
            raise ValueError("TOURNAMENT_WEATHER_MARKET_SERIES_CAPTURE_INVALID")
    book_original = contexts[receipt["book_envelope_sha256"]]
    if (
        book_original["body"] != snapshot["provider_payload"]
        or book_original["url"] != snapshot["request_url"]
        or aware(book_original["received_at"]) != aware(snapshot["captured_at"])
        or receipt["original_book_sha256"] != canonical_hash(book_original["body"])
    ):
        raise ValueError("TOURNAMENT_WEATHER_ORIGINAL_BOOK_INVALID")
    book = parse_orderbook(book_original["body"])
    if book.best_yes_bid is None or book.best_yes_ask is None:
        raise ValueError("TOURNAMENT_WEATHER_MIDPOINT_REQUIRED")
    midpoint = (book.best_yes_bid + book.best_yes_ask) / 2
    forecast = receipt["forecast_original"]
    if (
        canonical_hash(forecast) != receipt["forecast_sha256"]
        or forecast["ticker"] != anchor["ticker"]
        or forecast["model_name"] != model["name"]
        or aware(forecast["forecasted_at"]) != generated
        or _decimal(forecast["market_mid_probability"]) != midpoint
        or _decimal(forecast["best_yes_bid"]) != book.best_yes_bid
        or _decimal(forecast["best_yes_ask"]) != book.best_yes_ask
        or _decimal(receipt["source_off_probability"]) != midpoint
        or _decimal(rows[0]["probability"]) != midpoint
        or _decimal(rows[1]["probability"]) != _decimal(forecast["yes_probability"])
        or _decimal(receipt["source_on_probability"]) != _decimal(forecast["yes_probability"])
        or aware(rows[0]["generated_at"]) != baseline_at
        or aware(rows[1]["generated_at"]) != generated
        or rows[0]["feature_hashes"] != []
        or len(features) != 1
    ):
        raise ValueError("TOURNAMENT_WEATHER_FORECAST_BINDING_INVALID")
    feature = next(iter(features.values()))
    original = contexts[feature["source_original_sha256"]]
    capture_sha = original["capture_envelope_sha256"]
    capture = contexts[capture_sha]
    if (
        capture_sha not in envelope_hashes
        or not capture["url"].startswith("https://api.weather.gov/")
        or not capture["url"].endswith("/forecast/hourly")
        or original["provider_payload"] != capture["body"]
        or aware(original["received_at"]) != aware(capture["received_at"])
        or aware(original["available_at"]) != aware(capture["received_at"])
        or feature["value"] != forecast["feature_json"]
        or aware(feature["observed_at"]) != aware(capture["body"]["properties"]["updateTime"])
        or any(
            not 0 <= (at - aware(capture["body"]["properties"][key])).total_seconds() <= 1800
            or aware(capture["body"]["properties"][key]) > generated
            for key in ("generatedAt", "updateTime")
        )
        or not aware(feature["available_at"]) <= generated
    ):
        raise ValueError("TOURNAMENT_WEATHER_SOURCE_FEATURE_INVALID")


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
    if policy["kind"] == "lagged-cpi-paired-policy-v1":
        from kalshi_predictor.data_sources.economic_pair import validate_economic_receipt

        validate_economic_receipt(pair, anchor, snapshot, model, contexts, policy, at)
    else:
        _weather_receipt(pair, anchor, snapshot, model, contexts, features, policy, at)
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
            evidence_scope=(
                "RECORDED_WEATHER_EXECUTION_HASH_BINDING_NOT_ATTESTATION"
                if frozen["kind"] == "weather-paired-source-policy-v1"
                else "RECORDED_ECONOMIC_EXECUTION_HASH_BINDING_NOT_ATTESTATION"
                if frozen["kind"] == "lagged-cpi-paired-policy-v1"
                else "HASH_BOUND_DECLARATIONS_ONLY"
            ),
            contrast_type=(
                "MARKET_BASELINE_VS_WEATHER_V2"
                if frozen["kind"] == "weather-paired-source-policy-v1"
                else "MARKET_BASELINE_VS_LAGGED_SA_CPI_MOMENTUM_ECONOMIC_V1"
                if frozen["kind"] == "lagged-cpi-paired-policy-v1"
                else "DECLARED_SOURCE_COMPARISON"
            ),
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
