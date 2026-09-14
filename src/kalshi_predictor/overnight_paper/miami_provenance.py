"""Original Miami analytical bundles for provenance; no qualification authority."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from .miami_binding import MiamiOriginal, _at, _decode, bind_miami_forecast_to_contract
from .miami_source_gate import MiamiCaptureEvidence, MiamiGateContext, verify_miami_gate4
from .provenance import Artifact, canonical_hash

CLOCK_BASIS = "miami-original-replay-receipt-v1"
BUNDLE_URL = "urn:kalshi-paper:miami-original-replay-v1"


@dataclass(frozen=True)
class MiamiBundleGateContext:
    """Gate4 receives the same outer analytical artifact bound by gate9."""

    source: Artifact


def verify_miami_bundle_gate4(
    context: MiamiBundleGateContext,
    *,
    inputs: dict,
    sources: tuple[tuple[str, bytes], ...],
    now: datetime,
) -> bool:
    if sources != ((context.source.sha256, context.source.payload),):
        return False
    hashes = inputs.get("source_hashes")
    if (
        not isinstance(hashes, list)
        or any(type(value) is not str for value in hashes)
        or len(set(hashes)) != len(hashes)
        or context.source.sha256 not in hashes
    ):
        return False
    source = _decode(context.source)
    canonical_hash(source)  # Reject overflow-to-infinity even in unused parsed fields.
    original = _context(source["body"])
    json_originals = [
        original.frozen_prediction,
        original.recording_receipt,
        original.market.artifact,
        original.event.artifact,
        original.series.artifact,
        *original.catalog_receipts,
    ]
    for capture in original.captures:
        json_originals.extend(
            (
                capture.index.artifact,
                capture.calibrations.artifact,
                capture.index_receipt,
                capture.calibrations_receipt,
            )
        )
    for value in json_originals:
        canonical_hash(_decode(value))
    # Rule PDFs and original source code are deliberately not parsed as JSON.
    verified = verify_miami_provenance_binding(source, decision=inputs, now=now)
    return all(
        inputs.get(key) == verified["inputs"][key]
        for key in (
            "source_kind",
            "historical_public_availability",
            "miami_context_sha256",
        )
    )


def _artifact_row(value: Artifact) -> dict[str, str]:
    return {"sha256": value.sha256, "payload_hex": value.payload.hex()}


def _artifact(value: dict) -> Artifact:
    encoded = value["payload_hex"]
    if not isinstance(encoded, str) or not 0 < len(encoded) <= 16_000_000:
        raise ValueError("MIAMI_PROVENANCE_ORIGINAL_BUDGET")
    import hashlib

    raw = bytes.fromhex(encoded)
    if hashlib.sha256(raw).hexdigest() != value["sha256"]:
        raise ValueError("MIAMI_PROVENANCE_ORIGINAL_HASH")
    return Artifact(value["sha256"], raw)


def _original_row(value: MiamiOriginal) -> dict:
    return dict(
        artifact=_artifact_row(value.artifact),
        url=value.url,
        received_at=_at(value.received_at).isoformat(),
    )


def _original(value: dict) -> MiamiOriginal:
    return MiamiOriginal(_artifact(value["artifact"]), value["url"], _at(value["received_at"]))


def _context(body: dict) -> MiamiGateContext:
    if not isinstance(body, dict):
        raise ValueError("MIAMI_PROVENANCE_CONTEXT_TYPE")
    captures, code = body["captures"], body["code_originals"]
    if (
        not isinstance(captures, list)
        or not isinstance(code, list)
        or not 1 <= len(captures) <= 12
        or not 1 <= len(code) <= 5
        or type(body["current_capture"]) is not int
        or not 0 <= body["current_capture"] < len(captures)
    ):
        raise ValueError("MIAMI_PROVENANCE_CONTEXT_BUDGET")
    if not 1 <= len(body["rule_documents"]) <= 4 or len(body["catalog_receipts"]) != 3:
        raise ValueError("MIAMI_PROVENANCE_DOCUMENT_BUDGET")
    return MiamiGateContext(
        _artifact(body["frozen_prediction"]),
        _artifact(body["recording_receipt"]),
        tuple(
            MiamiCaptureEvidence(
                _original(c["index"]),
                _original(c["calibrations"]),
                _artifact(c["index_receipt"]),
                _artifact(c["calibrations_receipt"]),
            )
            for c in captures
        ),
        body["current_capture"],
        _original(body["market"]),
        _original(body["event"]),
        _original(body["series"]),
        (
            _artifact(body["catalog_receipts"][0]),
            _artifact(body["catalog_receipts"][1]),
            _artifact(body["catalog_receipts"][2]),
        ),
        tuple(_artifact(c) for c in body["rule_documents"]),
        tuple((c["path"], _artifact(c["artifact"])) for c in code),
    )


def miami_source_bundle(context: MiamiGateContext, *, decision_at: datetime) -> dict[str, Any]:
    """Preserve genuine original bytes; writing this dictionary does not freeze a model."""
    body = dict(
        frozen_prediction=_artifact_row(context.frozen_prediction),
        recording_receipt=_artifact_row(context.recording_receipt),
        captures=[
            dict(
                index=_original_row(c.index),
                calibrations=_original_row(c.calibrations),
                index_receipt=_artifact_row(c.index_receipt),
                calibrations_receipt=_artifact_row(c.calibrations_receipt),
            )
            for c in context.captures
        ],
        current_capture=context.current_capture,
        market=_original_row(context.market),
        event=_original_row(context.event),
        series=_original_row(context.series),
        catalog_receipts=[_artifact_row(c) for c in context.catalog_receipts],
        rule_documents=[_artifact_row(c) for c in context.rule_documents],
        code_originals=[dict(path=p, artifact=_artifact_row(a)) for p, a in context.code_originals],
    )
    recorded = _at(_decode(context.recording_receipt)["prediction_recorded_at"])
    received = max(_at(o.received_at) for o in (context.market, context.event, context.series))
    available = max(recorded, received)
    source = dict(
        url=BUNDLE_URL,
        clock_basis=CLOCK_BASIS,
        role="ANALYTICAL_SOURCE",
        settlement_truth=False,
        historical_public_availability="UNKNOWN",
        provider_generated_at=None,
        provider_updated_at=None,
        received_at=received.isoformat(),
        available_at=available.isoformat(),
        body=body,
    )
    verify_miami_provenance_source(source, decision_at=decision_at, now=decision_at)
    return source


def verify_miami_provenance_source(source: dict, *, decision_at: datetime, now: datetime) -> dict:
    """Recompute exact original forecast and source health, retaining separate clocks."""
    if (
        source.get("url") != BUNDLE_URL
        or source.get("clock_basis") != CLOCK_BASIS
        or source.get("role") != "ANALYTICAL_SOURCE"
        or source.get("settlement_truth") is not False
        or source.get("historical_public_availability") != "UNKNOWN"
        or source.get("provider_generated_at") is not None
        or source.get("provider_updated_at") is not None
    ):
        raise ValueError("MIAMI_PROVENANCE_SOURCE_IDENTITY")
    context = _context(source["body"])
    at = _at(decision_at)
    bound = bind_miami_forecast_to_contract(
        frozen_prediction=context.frozen_prediction,
        recording_receipt=context.recording_receipt,
        captures=tuple((c.index, c.calibrations) for c in context.captures),
        market=context.market,
        event=context.event,
        series=context.series,
        rule_documents=context.rule_documents,
        now=at,
    )
    saved = _decode(context.frozen_prediction)
    forecast = next(
        f for f in saved["prediction"]["forecasts"] if _at(f["target_at"]) == bound.target_at
    )
    originals = tuple(sorted(set((a.sha256, a.payload) for a in context.artifacts())))
    inputs = dict(
        category="Climate and Weather",
        series="KXTEMPMIAH",
        source_kind="miami-canonical-index-v1",
        model_name=forecast["model"],
        model_version="1",
        historical_public_availability="UNKNOWN",
        miami_context_sha256=context.fingerprint(),
        source_hashes=[s for s, _ in originals],
        decision_at=at.isoformat(),
        ticker=bound.ticker,
        event_id=bound.event_ticker,
        frozen_prediction_sha256=context.frozen_prediction.sha256,
        observation_time=bound.target_at.isoformat(),
        model_input_as_of=saved["model_input_as_of"],
        origin_at=forecast["origin_at"],
        forecast_probability=str(bound.probability_yes),
    )
    if not verify_miami_gate4(context, inputs=inputs, sources=originals, now=now):
        raise ValueError("MIAMI_PROVENANCE_ORIGINAL_REPLAY_OR_HEALTH")
    received = max(_at(o.received_at) for o in (context.market, context.event, context.series))
    available = max(bound.recorded_at, received)
    if (
        _at(source["received_at"]) != received
        or _at(source["available_at"]) != available
        or not available <= at <= _at(now)
    ):
        raise ValueError("MIAMI_PROVENANCE_AVAILABILITY")
    return dict(
        inputs=inputs,
        forecast=forecast,
        input_sha256=canonical_hash(inputs),
        observed_at=forecast["origin_at"],
        available_at=available.isoformat(),
    )


def verify_miami_provenance_binding(
    source: dict, *, decision: dict, now: datetime, forecast: dict | None = None
) -> dict:
    verified = verify_miami_provenance_source(
        source, decision_at=_at(decision["decision_at"]), now=now
    )
    expected = verified["inputs"]
    for key in (
        "ticker",
        "event_id",
        "series",
        "category",
        "model_name",
        "model_version",
        "origin_at",
        "observation_time",
        "model_input_as_of",
        "frozen_prediction_sha256",
    ):
        if decision.get(key) != expected[key]:
            raise ValueError("MIAMI_PROVENANCE_DECISION_BINDING:" + key)
    if decision.get("miami_input_sha256") != verified["input_sha256"] or Decimal(
        str(decision["forecast_probability"])
    ) != Decimal(expected["forecast_probability"]):
        raise ValueError("MIAMI_PROVENANCE_PROBABILITY_OR_INPUT_BINDING")
    if forecast is not None and (
        forecast.get("miami_input_sha256") != verified["input_sha256"]
        or forecast.get("model_name") != expected["model_name"]
        or forecast.get("model_version") != expected["model_version"]
        or Decimal(str(forecast["probability"])) != Decimal(expected["forecast_probability"])
        or not _at(verified["available_at"])
        <= _at(forecast["generated_at"])
        <= _at(decision["decision_at"])
    ):
        raise ValueError("MIAMI_PROVENANCE_FORECAST_BINDING")
    return verified


def miami_feature_record(source: dict, *, decision_at: datetime, now: datetime) -> dict:
    """Exact empirical inputs and original recording availability; no invented provider clock."""
    verified = verify_miami_provenance_source(source, decision_at=decision_at, now=now)
    return dict(
        name="miami_prior_day_original_forecast",
        value=verified["forecast"],
        source_sha256=canonical_hash(source),
        observed_at=verified["observed_at"],
        available_at=verified["available_at"],
    )
