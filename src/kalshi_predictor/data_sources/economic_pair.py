"""Pure replay of recorded lagged-CPI execution, never model re-execution.

Original hashes bind recorded evidence, not cryptographic execution attestation.
No HTTP, Session, paper admission or independently invented probability model.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from kalshi_predictor.config import Settings
from kalshi_predictor.data.schema import EconomicFeature, MarketSnapshot
from kalshi_predictor.data_sources import economic_execution
from kalshi_predictor.economic.source_research import build_economic_source_features
from kalshi_predictor.forecasting.economic_v1 import _economic_adjustment
from kalshi_predictor.kalshi.orderbook import parse_orderbook
from kalshi_predictor.overnight_paper.provenance import Artifact, canonical_hash
from kalshi_predictor.overnight_paper.source_health import aware
from kalshi_predictor.research.fred import FREDOriginal, FREDResponse
from kalshi_predictor.utils.decimals import clamp_probability

if TYPE_CHECKING:
    from kalshi_predictor.data_sources.tournament import PairedForecast

POLICY_KIND = "lagged-cpi-paired-policy-v1"
CONTRAST = economic_execution.CONTRAST
EVIDENCE_SCOPE = "RECORDED_ECONOMIC_EXECUTION_HASH_BINDING_NOT_ATTESTATION"


def _artifact(value: Any) -> Artifact:
    return economic_execution._artifact(value)


def _decimal(value: Any) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, str | int | Decimal):
        raise ValueError("ECONOMIC_PAIR_DECIMAL_REQUIRED")
    result = Decimal(value)
    if not result.is_finite():
        raise ValueError("ECONOMIC_PAIR_NONFINITE")
    return result


def _original(pair: PairedForecast, digest: str) -> Artifact:
    values = [value for value in pair.context_originals if value.sha256 == digest]
    if len(values) != 1 or hashlib.sha256(values[0].payload).hexdigest() != digest:
        raise ValueError("ECONOMIC_PAIR_ORIGINAL_REQUIRED")
    return values[0]


def validate_economic_receipt(
    pair: PairedForecast,
    anchor: dict[str, Any],
    snapshot: dict[str, Any],
    model: dict[str, Any],
    contexts: dict[str, dict[str, Any]],
    frozen: dict[str, Any],
    at: datetime,
) -> None:
    """Verify immutable recorded semantics; general cohort/cost checks stay in tournament.

    The caller must decode and hash-verify the supplied contexts. This helper
    also verifies raw source/code artifacts itself, preserving original bytes.
    """
    if (
        anchor != pair.anchor.decode()
        or set(contexts) != {value.sha256 for value in pair.context_originals}
        or len(contexts) != len(pair.context_originals)
        or any(value.decode() != contexts[value.sha256] for value in pair.context_originals)
        or snapshot != contexts[anchor["snapshot_sha256"]]
        or model != contexts[anchor["model_artifact_sha256"]]
    ):
        raise ValueError("ECONOMIC_PAIR_DECODED_CONTEXT_SUBSTITUTION")
    p = contexts[anchor["procedure_sha256"]]
    r = contexts[anchor["execution_receipt_sha256"]]
    off, on = pair.source_off.decode(), pair.source_on.decode()
    if (
        frozen["kind"] != POLICY_KIND
        or frozen["source_id"] != "FRED"
        or frozen["procedure_sha256"] != anchor["procedure_sha256"]
        or frozen["model_artifact_sha256"] != anchor["model_artifact_sha256"]
        or p["kind"] != "lagged-cpi-procedure-v1"
        or not p["name"]
        or not p["version"]
        or p["variant_methods"] != economic_execution.METHODS
        or p["contrast_type"] != economic_execution.CONTRAST
        or r["kind"] != "lagged-cpi-execution-v1"
        or r["procedure_sha256"] != anchor["procedure_sha256"]
        or r["model_artifact_sha256"] != anchor["model_artifact_sha256"]
        or p["model_artifact_sha256"] != anchor["model_artifact_sha256"]
        or r["variant_methods"] != p["variant_methods"]
        or r["contrast_type"] != p["contrast_type"]
        or r["ephemeral_session"] is not True
        or r["research_only"] is not True
        or r["runtime_certified"] is not False
        or r["settlement_eligible"] is not False
        or r["fee_execution_verified"] is not False
        or r["atomic_filesystem_immutability"] is not False
        or r["verification_scope"]
        != "FILESYSTEM_BUNDLE_AND_IMPORTED_ORIGINS_BEFORE_AFTER_EXECUTION"
        or any(
            row[key] != anchor[key]
            for row in (off, on)
            for key in ("procedure_sha256", "execution_receipt_sha256")
        )
        or contexts[anchor["execution_policy_sha256"]]["side"] != anchor["side"]
    ):
        raise ValueError("ECONOMIC_PAIR_PROCEDURE_BINDING_INVALID")
    for row in (model, p):
        if (
            not aware(row["created_at"])
            <= aware(row["frozen_at"])
            <= aware(row["available_at"])
            <= aware(frozen["committed_at"])
        ):
            raise ValueError("ECONOMIC_PAIR_FREEZE_INVALID")
    if aware(model["available_at"]) > aware(p["frozen_at"]):
        raise ValueError("ECONOMIC_PAIR_MODEL_FREEZE_INVALID")
    settings = r["settings_original"]
    if (
        not isinstance(settings, dict)
        or set(settings) != set(Settings.model_fields)
        or model["name"] != "economic_v1"
        or not model["version"]
        or model["model_kind"] != "fixed_heuristic"
        or model["training_cutoff"] is not None
        or model["training_dataset_hashes"] != []
        or model.get("training_artifacts", []) != []
        or model["parameters"] != settings
        or model["parameters_sha256"] != canonical_hash(settings)
        or r["settings_sha256"] != canonical_hash(settings)
        or p["settings_sha256"] != r["settings_sha256"]
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
        raise ValueError("ECONOMIC_PAIR_SETTINGS_INVALID")
    code = _original(pair, model["code_sha256"]).decode()
    dependencies = model["code_dependencies"]
    if (
        not isinstance(dependencies, dict)
        or not dependencies
        or model["model_entrypoint"] != economic_execution.ENTRYPOINT
        or r["model_code_sha256"] != model["code_sha256"]
        or any(
            r[key] != dependencies
            for key in ("code_dependencies", "dependencies_before", "dependencies_after")
        )
        or code["schema"] != "economic-research-source-bundle-v1"
        or code["entrypoint"] != economic_execution.ENTRYPOINT
        or code["scope"] != "RESEARCH_DEPENDENCY_FINGERPRINT_NOT_ADMISSION_BOUNDARY"
        or len(code["sources"]) != len(dependencies)
        or set(code["runtime_versions"]) != {"SQLAlchemy", "pydantic", "pydantic-settings"}
        or any(not isinstance(v, str) or not v for v in code["runtime_versions"].values())
        or not code["python_version"]
    ):
        raise ValueError("ECONOMIC_PAIR_CODE_BINDING_INVALID")
    seen = set()
    modules = set()
    for item in code["sources"]:
        path = item["path"]
        if (
            not path.startswith("src/")
            or "\\" in path
            or ":" in path
            or any(part in {"", ".", ".."} for part in path.split("/"))
            or path
            not in {
                "src/" + item["module"].replace(".", "/") + ".py",
                "src/" + item["module"].replace(".", "/") + "/__init__.py",
            }
            or path in seen
            or item["module"] in modules
            or hashlib.sha256(item["source"].encode()).hexdigest() != item["sha256"]
            or dependencies[path] != item["sha256"]
        ):
            raise ValueError("ECONOMIC_PAIR_CODE_SOURCE_INVALID")
        seen.add(path)
        modules.add(item["module"])
    required = {
        "kalshi_predictor.forecasting.economic_v1",
        "kalshi_predictor.economic.repository",
        "kalshi_predictor.economic.features",
        "kalshi_predictor.economic.source_research",
        "kalshi_predictor.data_sources.economic_execution",
        "kalshi_predictor.kalshi.orderbook",
    }
    if seen != set(dependencies) or not required <= modules:
        raise ValueError("ECONOMIC_PAIR_REQUIRED_CODE_MISSING")
    started, finished = aware(r["execution_started_at"]), aware(r["execution_finished_at"])
    forecast = r["forecast_original"]
    feature_record, link = r["feature_record_original"], r["link_original"]
    generated, available = aware(r["forecast_generated_at"]), aware(r["forecast_available_at"])
    if not (
        aware(p["available_at"])
        <= started
        <= aware(r["source_off_generated_at"])
        <= aware(forecast["feature_json"]["input_cutoff"])
        <= generated
        <= available
        <= finished
        <= aware(r["receipt_generated_at"])
        <= at
    ):
        raise ValueError("ECONOMIC_PAIR_EXECUTION_CLOCK_INVALID")
    if (
        not started
        <= aware(feature_record["generated_at"])
        <= aware(feature_record["created_at"])
        <= aware(forecast["feature_json"]["input_cutoff"])
    ):
        raise ValueError("ECONOMIC_PAIR_FEATURE_CLOCK_INVALID")
    if not started <= aware(link["detected_at"]) <= aware(forecast["feature_json"]["input_cutoff"]):
        raise ValueError("ECONOMIC_PAIR_LINK_CLOCK_INVALID")
    rule = contexts[anchor["rule_sha256"]]
    captures = {}
    for kind in ("market", "event", "series", "book"):
        digest = r[kind + "_envelope_sha256"]
        capture = contexts[digest]
        wrapper = snapshot if kind == "book" else contexts[rule[kind + "_original_sha256"]]
        if (
            wrapper["capture_envelope_sha256"] != digest
            or wrapper["provider_payload"] != capture["body"]
            or wrapper["request_url"] != capture["url"]
            or aware(wrapper["received_at"]) != aware(capture["received_at"])
            or aware(wrapper["available_at"]) != aware(capture["received_at"])
            or not aware(p["available_at"]) <= aware(capture["received_at"]) <= started
            or aware(frozen["committed_at"]) > aware(capture["received_at"])
            or not 0 <= (at - aware(capture["received_at"])).total_seconds() <= 60
        ):
            raise ValueError("ECONOMIC_PAIR_CAPTURE_BINDING_INVALID")
        captures[kind] = capture
    market = captures["market"]["body"]["market"]
    event, series = captures["event"]["body"]["event"], captures["series"]["body"]["series"]
    mapping = p["contract_mapping"]
    target = date.fromisoformat(mapping["target_month"] + "-01")
    if (
        r["contract_mapping"] != mapping
        or r["ticker"] != anchor["ticker"]
        or p["ticker"] != anchor["ticker"]
        or r["event_ticker"] != anchor["event_id"]
        or anchor["independent_event_id"] != anchor["event_id"]
        or mapping["event_ticker"] != "KXCPI-" + target.strftime("%y%b").upper()
        or mapping["event_ticker"] != anchor["event_id"]
        or mapping["series_ticker"] != "KXCPI"
        or mapping["source_series"] != "CPIAUCSL"
        or mapping["concept"] != "headline_cpi_u"
        or mapping["seasonal_adjustment"] != "SA"
        or mapping["outcome_units"] != "published_one_decimal_monthly_percent_change"
        or mapping["comparator"] != "strictly_greater"
        or mapping["revision_policy"] != "exclude_revisions_after_expiration"
        or mapping["contract_terms_sha256"] != economic_execution.TERMS_SHA256
        or market["ticker"] != anchor["ticker"]
        or market["event_ticker"] != anchor["event_id"]
        or event["event_ticker"] != anchor["event_id"]
        or event["series_ticker"] != "KXCPI"
        or series["ticker"] != "KXCPI"
        or series["contract_terms_url"] != "https://assets.kalshi.com/contract_terms/CPI.pdf"
        or market.get("series_ticker", "KXCPI") != "KXCPI"
        or event.get("fee_type_override") is not None
        or event.get("fee_multiplier_override") is not None
        or market["strike_type"] != "greater"
        or market["status"] not in {"active", "open"}
        or not at < aware(market["close_time"]) <= aware(mapping["scheduled_release_at"])
        or aware(anchor["event_window_end"]) != aware(mapping["scheduled_release_at"])
        or aware(anchor["event_window_start"]) != aware(frozen["holdout_start"])
    ):
        raise ValueError("ECONOMIC_PAIR_CONTRACT_MAPPING_INVALID")
    strike = _decimal(mapping["strike_percent"])
    expected = (
        f"If the Consumer Price Index (CPI) increases by more than {strike:.1f}% "
        f"(single-decimal) in {target.strftime('%B %Y')}, then the market resolves to Yes."
    )
    if _decimal(str(market["floor_strike"])) != strike or market["rules_primary"] != expected:
        raise ValueError("ECONOMIC_PAIR_PRIMARY_RULE_MISMATCH")
    for kind, path in (
        ("market", "/markets/" + anchor["ticker"]),
        ("event", "/events/" + anchor["event_id"]),
        ("series", "/series/KXCPI"),
        ("book", "/markets/" + anchor["ticker"] + "/orderbook"),
    ):
        if captures[kind]["url"] != economic_execution.BASE + path:
            raise ValueError("ECONOMIC_PAIR_CAPTURE_URL_INVALID")
    raw = _original(pair, r["source_original_sha256"])
    response = FREDResponse(
        "observations",
        "CPIAUCSL",
        FREDOriginal(r["source_url"], aware(r["source_received_at"]), raw.sha256, raw.payload),
        raw.decode(),
    )
    inputs = build_economic_source_features(response, decision_at=started)
    if (
        economic_execution._json(asdict(inputs)) != r["source_features"]
        or not aware(p["available_at"]) <= inputs.received_at <= started
        or aware(frozen["committed_at"]) > inputs.received_at
        or len(inputs.observations) != 2
        or inputs.momentum_score is None
        or [x.observation_date.isoformat() for x in inputs.observations] != p["lagged_periods"]
        or (target.year - inputs.observations[-1].observation_date.year) * 12
        + target.month
        - inputs.observations[-1].observation_date.month
        != 1
    ):
        raise ValueError("ECONOMIC_PAIR_SOURCE_REPLAY_INVALID")
    if len(pair.features) != 1:
        raise ValueError("ECONOMIC_PAIR_ONE_FEATURE_REQUIRED")
    feature = pair.features[0].decode()
    source_wrapper = contexts[feature["source_original_sha256"]]
    if (
        feature["source_id"] != "FRED"
        or feature["value"] != r["source_features"]
        or feature["clock_basis"] != "LOCAL_RECEIPT_NOT_PROVIDER_PUBLICATION"
        or aware(feature["observed_at"]) != inputs.received_at
        or aware(feature["generated_at"]) != aware(feature_record["generated_at"])
        or aware(feature["available_at"]) != aware(feature_record["created_at"])
        or source_wrapper["capture_original_sha256"] != raw.sha256
        or source_wrapper["provider_payload"] != raw.decode()
        or source_wrapper["source_url"] != r["source_url"]
        or aware(source_wrapper["received_at"]) != inputs.received_at
        or aware(source_wrapper["available_at"]) != inputs.received_at
    ):
        raise ValueError("ECONOMIC_PAIR_FEATURE_ORIGINAL_INVALID")
    book = parse_orderbook(captures["book"]["body"])
    if book.best_yes_bid is None or book.best_yes_ask is None:
        raise ValueError("ECONOMIC_PAIR_MIDPOINT_REQUIRED")
    midpoint = (book.best_yes_bid + book.best_yes_ask) / 2
    values = forecast["feature_json"]
    # Existing pure arithmetic, no Session, forecast call, or duplicate model.
    # This prevents coordinated probability/adjustment declarations from
    # overriding the source score already reconstructed above.
    adjustment = _economic_adjustment(
        MarketSnapshot(ticker=anchor["ticker"], raw_market_json=json.dumps(market)),
        EconomicFeature(
            surprise_score=str(inputs.momentum_score),
            direction=inputs.momentum_direction,
            confidence_score="70",
        ),
    )
    if _decimal(values["adjustment"]) != adjustment or _decimal(
        forecast["yes_probability"]
    ) != clamp_probability(midpoint + adjustment):
        raise ValueError("ECONOMIC_PAIR_EXISTING_ARITHMETIC_MISMATCH")
    event_key = "lagged_cpi_" + anchor["procedure_sha256"]
    for name in ("snapshot", "feature", "link"):
        if type(r["ephemeral_" + name + "_id"]) is not int or r["ephemeral_" + name + "_id"] < 1:
            raise ValueError("ECONOMIC_PAIR_EPHEMERAL_ID_INVALID")
    if (
        canonical_hash(forecast) != r["forecast_sha256"]
        or forecast["ticker"] != anchor["ticker"]
        or forecast["model_name"] != "economic_v1"
        or aware(forecast["forecasted_at"]) != generated
        or _decimal(forecast["market_mid_probability"]) != midpoint
        or _decimal(forecast["best_yes_bid"]) != book.best_yes_bid
        or _decimal(forecast["best_yes_ask"]) != book.best_yes_ask
        or _decimal(r["source_off_probability"]) != midpoint
        or _decimal(off["probability"]) != midpoint
        or _decimal(r["source_on_probability"]) != _decimal(forecast["yes_probability"])
        or _decimal(on["probability"]) != _decimal(forecast["yes_probability"])
        or not 0 <= _decimal(on["probability"]) <= 1
        or aware(on["generated_at"]) != generated
        or aware(off["generated_at"]) != aware(r["source_off_generated_at"])
        or off["feature_hashes"] != []
        or on["feature_hashes"] != [pair.features[0].sha256]
        or r["original_book_sha256"] != canonical_hash(captures["book"]["body"])
        or anchor["snapshot_id"] != r["ephemeral_snapshot_id"]
        or values["snapshot_id"] != anchor["snapshot_id"]
        or values["economic_feature_id"] != r["ephemeral_feature_id"]
        or feature_record["id"] != r["ephemeral_feature_id"]
        or link["id"] != r["ephemeral_link_id"]
        or link["ticker"] != anchor["ticker"]
        or any(row["event_key"] != event_key for row in (link, feature_record, values))
        or _decimal(feature_record["surprise_score"]) != inputs.momentum_score
        or feature_record["direction"] != inputs.momentum_direction
        or _decimal(feature_record["confidence_score"]) != Decimal(70)
        or _decimal(values["surprise_score"]) != inputs.momentum_score
        or values["direction"] != feature_record["direction"]
        or values["confidence_score"] != feature_record["confidence_score"]
        or r["feature_original"]["forecast_value"] is not None
        or r["feature_original"]["actual_value"] != inputs.observations[1].raw_value
        or r["feature_original"]["previous_value"] != inputs.observations[0].raw_value
        or _decimal(r["feature_original"]["surprise_score"]) != inputs.momentum_score
        or r["feature_original"]["direction"] != inputs.momentum_direction
        or _decimal(r["feature_original"]["confidence_score"]) != Decimal(70)
        or any(row["category"] != "cpi" for row in (link, feature_record, values))
        or _decimal(link["confidence"]) != 100
        or values["link_confidence"] != link["confidence"]
        or json.loads(link["raw_json"]) != mapping
        or json.loads(feature_record["raw_json"])
        != (r["feature_original"] | dict(meaning=inputs.momentum_status, source_sha256=raw.sha256))
        or aware(values["feature_generated_at"]) != aware(feature_record["generated_at"])
        or aware(values["feature_created_at"]) != aware(feature_record["created_at"])
        or aware(values["link_detected_at"]) != aware(link["detected_at"])
        or aware(values["snapshot_captured_at"]) != aware(captures["book"]["received_at"])
        or _decimal(values["market_mid"]) != midpoint
        or _decimal(values["final_probability"]) != _decimal(forecast["yes_probability"])
    ):
        raise ValueError("ECONOMIC_PAIR_ACTUAL_FORECAST_BINDING_INVALID")


def build_economic_pair(
    *,
    receipt: Artifact,
    model: Artifact,
    procedure: Artifact,
    model_code: bytes,
    source: FREDResponse,
    market: Artifact,
    event: Artifact,
    series: Artifact,
    book: Artifact,
    policy: Artifact,
    execution_policy: Artifact,
    fee_original: Artifact,
    decision_at: datetime,
    recorded_at: datetime,
) -> PairedForecast:
    """Normalize preserved execution evidence; cost admission remains separate.

    The builder returns evidence even when the actual series fee type is not
    supported by the general execution policy. It does not certify opportunity.
    """
    from kalshi_predictor.data_sources.tournament import (
        PairedForecast,
        conservative_single_fill_fees,
    )

    r, m, p = receipt.decode(), model.decode(), procedure.decode()
    if (
        type(source) is not FREDResponse
        or source.kind != "observations"
        or source.series_id != "CPIAUCSL"
    ):
        raise ValueError("ECONOMIC_PAIR_EXACT_FRED_RESPONSE_REQUIRED")
    frozen, cost = policy.decode(), execution_policy.decode()
    at, recorded = aware(decision_at), aware(recorded_at)
    if (
        frozen["execution_policy_sha256"] != execution_policy.sha256
        or cost["fee_rounding_original_sha256"] != fee_original.sha256
    ):
        raise ValueError("ECONOMIC_PAIR_COST_POLICY_BINDING_INVALID")

    def wrap(value: Artifact, kind: str, **extra: Any) -> Artifact:
        row = value.decode()
        return _artifact(
            dict(
                kind=kind,
                request_url=row["url"],
                received_at=row["received_at"],
                available_at=row["received_at"],
                provider_payload=row["body"],
                provider_payload_sha256=canonical_hash(row["body"]),
                capture_envelope_sha256=value.sha256,
                **extra,
            )
        )

    mw, ew, sw = (
        wrap(market, "market-original-v1"),
        wrap(event, "event-original-v1"),
        wrap(series, "series-original-v1"),
    )
    snapshot = wrap(
        book,
        "book-original-v1",
        id=r["ephemeral_snapshot_id"],
        ticker=r["ticker"],
        captured_at=book.decode()["received_at"],
        clock_basis="public_rest_receipt",
    )
    source_original = Artifact(source.original.sha256, source.original.payload)
    source_wrapper = _artifact(
        dict(
            kind="source-original-v1",
            source_id="FRED",
            source_url=source.original.url,
            received_at=source.original.received_at,
            available_at=source.original.received_at,
            provider_payload=source.data,
            provider_payload_sha256=canonical_hash(source.data),
            capture_original_sha256=source.original.sha256,
            provider_timestamp=None,
        )
    )
    feature = _artifact(
        dict(
            kind="feature-v1",
            source_id="FRED",
            source_original_sha256=source_wrapper.sha256,
            observed_at=source.original.received_at,
            clock_basis="LOCAL_RECEIPT_NOT_PROVIDER_PUBLICATION",
            generated_at=r["feature_record_original"]["generated_at"],
            available_at=r["feature_record_original"]["created_at"],
            value=r["source_features"],
        )
    )
    mr = market.decode()["body"]["market"]
    rule_version = canonical_hash(
        {key: mr.get(key) for key in ("rules_primary", "rules_secondary")}
    )
    rule = _artifact(
        dict(
            ticker=r["ticker"],
            event_id=r["event_ticker"],
            rule_version=rule_version,
            series_ticker="KXCPI",
            available_at=market.decode()["received_at"],
            market_original_sha256=mw.sha256,
            event_original_sha256=ew.sha256,
            series_original_sha256=sw.sha256,
            settlement_certified=False,
            version_basis="CAPTURED_RULE_TEXT_SHA256",
        )
    )
    parsed = parse_orderbook(book.decode()["body"])
    if (
        parsed.best_yes_bid is None
        or parsed.best_yes_ask is None
        or cost["side"] not in {"BUY_YES", "BUY_NO"}
    ):
        raise ValueError("ECONOMIC_PAIR_BOOK_SIDE_REQUIRED")
    price = parsed.best_yes_ask if cost["side"] == "BUY_YES" else Decimal(1) - parsed.best_yes_bid
    fees = conservative_single_fill_fees(
        price, Decimal(str(series.decode()["body"]["series"]["fee_multiplier"]))
    )
    anchor = _artifact(
        dict(
            event_id=r["event_ticker"],
            independent_event_id=r["event_ticker"],
            ticker=r["ticker"],
            snapshot_id=r["ephemeral_snapshot_id"],
            snapshot_sha256=snapshot.sha256,
            decision_at=at,
            model_name=m["name"],
            model_version=m["version"],
            model_kind=m["model_kind"],
            training_cutoff=m["training_cutoff"],
            model_frozen_at=m["frozen_at"],
            model_artifact_sha256=model.sha256,
            rule_version=rule_version,
            rule_sha256=rule.sha256,
            execution_policy_sha256=execution_policy.sha256,
            side=cost["side"],
            executable_price=str(price),
            estimated_fee=str(fees["estimated_fee"]),
            trade_fee=str(fees["trade_fee"]),
            rounding_allowance=str(fees["rounding_allowance"]),
            slippage=cost["slippage"],
            uncertainty=cost["uncertainty"],
            event_window_start=frozen["holdout_start"],
            event_window_end=p["contract_mapping"]["scheduled_release_at"],
            procedure_sha256=procedure.sha256,
            execution_receipt_sha256=receipt.sha256,
        )
    )
    common = anchor.decode() | dict(
        kind="paired-forecast-v1",
        anchor_sha256=anchor.sha256,
        decision_id=canonical_hash(anchor.decode()),
        source_id="FRED",
        recorded_at=recorded,
    )
    off = _artifact(
        common
        | dict(
            source_enabled=False,
            probability=r["source_off_probability"],
            feature_hashes=[],
            generated_at=r["source_off_generated_at"],
        )
    )
    on = _artifact(
        common
        | dict(
            source_enabled=True,
            probability=r["source_on_probability"],
            feature_hashes=[feature.sha256],
            generated_at=r["forecast_generated_at"],
        )
    )
    originals = (
        snapshot,
        mw,
        ew,
        sw,
        rule,
        source_wrapper,
        source_original,
        model,
        procedure,
        receipt,
        Artifact(hashlib.sha256(model_code).hexdigest(), model_code),
        market,
        event,
        series,
        book,
        execution_policy,
        fee_original,
    )
    pair = PairedForecast(anchor, off, on, None, (feature,), originals)
    contexts = {value.sha256: value.decode() for value in originals}
    if (
        len(contexts) != len(originals)
        or not at <= recorded
        or (recorded - at).total_seconds() > 60
    ):
        raise ValueError("ECONOMIC_PAIR_CONTEXT_OR_RECORD_CLOCK_INVALID")
    validate_economic_receipt(pair, anchor.decode(), snapshot.decode(), m, contexts, frozen, at)
    return pair
