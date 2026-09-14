"""Synthetic end-to-end lineage checks; no model or rule certification."""

import json
from dataclasses import replace
from datetime import datetime

import pytest
from test_cf_guarded_source_bridge import NOW as CF_NOW
from test_cf_guarded_source_bridge import originals
from test_overnight_provenance import artifact
from test_paper_release_provenance import complete_inputs

from kalshi_predictor.crypto.cf_process_inputs import digest
from kalshi_predictor.overnight_paper.cf_source import (
    CLOCK_BASIS,
    CFSourceContext,
    build_cf_source,
    cf_feature_record,
    verify_cf_source,
)
from kalshi_predictor.overnight_paper.provenance import canonical_hash
from kalshi_predictor.overnight_paper.provenance_gate import verify_complete_provenance


def complete_cf_inputs():
    args = complete_inputs()
    now = args["now"]
    delta = now - CF_NOW
    raw, receipt, target = originals()
    body, rec = json.loads(raw), json.loads(receipt)
    body["data"]["serverTime"] = now.isoformat()
    for row in body["data"]["payload"]:
        row["time"] += int(delta.total_seconds() * 1000)
    raw = json.dumps(body).encode()
    for key in ("requested_at", "received_at", "recorded_at"):
        rec[key] = (datetime.fromisoformat(rec[key]) + delta).isoformat()
    rec["source_sha256"] = digest(raw)
    market = json.loads(target.market_original)
    market["market"]["close_time"] = (
        datetime.fromisoformat(market["market"]["close_time"].replace("Z", "+00:00")) + delta
    ).isoformat()
    window = target.rules.closing
    shift = int(delta.total_seconds() * 1000)
    target = replace(
        target, market_original=json.dumps(market).encode(),
        rule_received_at=target.rule_received_at + delta,
        market_received_at=target.market_received_at + delta,
        finality_deadline=target.finality_deadline + delta if target.finality_deadline else None,
        rules=replace(target.rules, closing=replace(
            window, start_ms=window.start_ms + shift, end_ms=window.end_ms + shift,
        )),
    )
    cf_context = CFSourceContext(target)
    source = build_cf_source(
        raw=raw, receipt=json.dumps(rec).encode(), target=target, decision_at=now,
    )
    checked = verify_cf_source(source, target=target, decision_at=now, now=now)
    feature = cf_feature_record(source, context=cf_context, decision_at=now, now=now)
    original = artifact(source)
    context = args["context"]
    identity = dict(
        ticker=target.rules.market_ticker, event_id=target.event_ticker,
        series="KXSOLE", category="Crypto",
    )
    bindings = dict(
        **identity, source_kind=CLOCK_BASIS, source_hashes=[original.sha256],
        cf_input_sha256=checked["input_sha256"],
        cf_target_sha256=canonical_hash(checked["target"]),
    )
    features = artifact(context.features_artifact.decode() | identity | dict(
        source_hashes=[original.sha256], records=[feature],
    ))
    rows = {key: value.decode() for key, value in context.artifacts.items()}
    rows["snapshot"].update(identity)
    rows["forecast"].update(bindings, features_artifact_sha256=features.sha256)
    artifacts = {key: artifact(row) for key, row in rows.items()}
    decision = args["decision"]
    decision.update(bindings, features_artifact_sha256=features.sha256)
    decision.update({key + "_artifact_sha256": value.sha256 for key, value in artifacts.items()})
    decision["source_timestamps"] = [dict(sha256=original.sha256, **{
        key: source[key] for key in (
            "provider_updated_at", "provider_generated_at", "available_at",
            "received_at", "clock_basis",
        )
    })]
    decision["feature_timestamps"] = [{key: feature[key] for key in (
        "name", "source_sha256", "observed_at", "available_at",
    )}]
    args["context"] = replace(
        context, artifacts=artifacts, features_artifact=features,
        source_artifacts=(original,), cf_context=cf_context,
    )
    args["decision_id"] = canonical_hash(decision)
    return args


def test_complete_cf_lineage_passes_without_certifying_skill_or_rules():
    result = verify_complete_provenance(**complete_cf_inputs())
    assert result.passed, result.blockers
    assert not result.model_calibration_verified
    assert not result.settlement_rules_verified


def test_missing_target_context_fails():
    args = complete_cf_inputs()
    args["context"] = replace(args["context"], cf_context=None)
    assert verify_complete_provenance(**args).blockers == ("CF_BRIDGE_CONTEXT_REQUIRED",)


@pytest.mark.parametrize("change", ["value", "missing", "duplicate", "forecast"])
def test_rehashed_false_feature_or_forecast_fails_semantically(change):
    args = complete_cf_inputs()
    context = args["context"]
    payload = context.features_artifact.decode()
    forecast = context.artifacts["forecast"].decode()
    if change == "value":
        payload["records"][0]["value"]["level"] = "999"
    elif change == "missing":
        payload["records"][0]["source_sha256"] = "0" * 64
    elif change == "duplicate":
        payload["records"] *= 2
    else:
        forecast["cf_input_sha256"] = "0" * 64
    features = artifact(payload)
    forecast["features_artifact_sha256"] = features.sha256
    artifacts = context.artifacts | {"forecast": artifact(forecast)}
    args["context"] = replace(context, features_artifact=features, artifacts=artifacts)
    args["decision"].update(
        features_artifact_sha256=features.sha256,
        forecast_artifact_sha256=artifacts["forecast"].sha256,
    )
    args["decision_id"] = canonical_hash(args["decision"])
    result = verify_complete_provenance(**args)
    assert not result.passed
    expected = {
        "value": "CF_FEATURE_ORIGINAL_BINDING", "missing": "CF_FEATURE_RECORD_REQUIRED",
        "duplicate": "CF_FEATURE_RECORD_REQUIRED", "forecast": "CF_BRIDGE_FORECAST_INPUT_BINDING",
    }
    assert expected[change] in result.blockers
