import hashlib
import json
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta

import pytest

from kalshi_predictor.overnight_paper.miami_binding import (
    BASE,
    INDEX,
    MiamiOriginal,
    bind_miami_forecast_to_contract,
)
from kalshi_predictor.overnight_paper.provenance import Artifact
from kalshi_predictor.weather.miami_forecast import forecast_miami_prior_day
from kalshi_predictor.weather.miami_index import STATIONS, decode_miami_index

ORIGIN = datetime(2026, 9, 10, 21, tzinfo=UTC)
CUTOFF = ORIGIN + timedelta(minutes=5)


def artifact(value):
    raw = json.dumps(value, sort_keys=True).encode()
    return Artifact(hashlib.sha256(raw).hexdigest(), raw)


def original(value, url):
    return MiamiOriginal(artifact(value), url, CUTOFF - timedelta(seconds=1))


@pytest.fixture
def inputs():
    def point(at, value):
        return dict(t=int(at.timestamp() * 1000), v=value, status="normal", contributors=5)

    points = [point(ORIGIN - timedelta(minutes=30), 79), point(ORIGIN, 80)]
    for day in (1, 2, 3):
        at = ORIGIN - timedelta(days=day)
        points.extend([point(at, 70), point(at + timedelta(hours=1), 70 + day)])
    index = original(
        dict(
            city="miami",
            config_version="config",
            units="fahrenheit",
            timeseries=sorted(points, key=lambda p: p["t"]),
        ),
        INDEX,
    )
    calibration = original(
        dict(
            city="miami",
            units="celsius",
            calibrations=[
                dict(
                    config_version="config",
                    effective_at_ms=0,
                    published_at_ms=0,
                    city_reference_c=0,
                    stations=[dict(station_id=s, weight=0.2, offset_c=0) for s in sorted(STATIONS)],
                )
            ],
        ),
        INDEX + "/calibrations",
    )
    capture = decode_miami_index(
        index.artifact.payload,
        calibration.artifact.payload,
        index_received_at=index.received_at,
        calibrations_received_at=calibration.received_at,
        index_units="fahrenheit",
    )
    forecast = forecast_miami_prior_day(
        [capture], origin_at=ORIGIN, model_input_as_of=CUTOFF, horizon_minutes=60
    )
    saved = artifact(
        dict(
            schema="frozen-research-prediction-v1",
            model_input_as_of=CUTOFF.isoformat(),
            model_committed_at=ORIGIN.isoformat(),
            input_received_at=index.received_at.isoformat(),
            target_at=forecast["target_at"],
            prediction=dict(forecasts=[forecast]),
        )
    )
    receipt = artifact(
        dict(
            schema="prediction-recording-receipt-v1",
            prediction_sha256=saved.sha256,
            prediction_recorded_at=(CUTOFF + timedelta(seconds=1)).isoformat(),
            clock_authority="POST_FSYNC_LOCAL_CLOCK_NOT_EXTERNAL_ATTESTATION",
        )
    )
    ticker = "KXTEMPMIAH-26SEP1018-T81.99"
    event = "KXTEMPMIAH-26SEP1018"
    market = original(
        dict(
            market=dict(
                ticker=ticker,
                event_ticker=event,
                close_time="2026-09-10T22:00:00Z",
                floor_strike=81.99,
                strike_type="greater",
                market_type="binary",
                rules_primary=(
                    "If the temperature recorded at Miami, FL for Sep 10, 2026 "
                    "at 6 PM EDT as reported by Synoptic Data, is above 81.99°, "
                    "then the market resolves to Yes."
                ),
                rules_secondary="Kalshi Weather Index Methodology",
            )
        ),
        BASE + "/markets/" + ticker,
    )
    return dict(
        frozen_prediction=saved,
        recording_receipt=receipt,
        captures=((index, calibration),),
        market=market,
        event=original(
            dict(event=dict(event_ticker=event, series_ticker="KXTEMPMIAH")),
            BASE + "/events/" + event,
        ),
        series=original(dict(series=dict(ticker="KXTEMPMIAH")), BASE + "/series/KXTEMPMIAH"),
        rule_documents=(artifact(dict(document="synthetic unreviewed rules")),),
        now=CUTOFF + timedelta(seconds=2),
    )


def test_exact_replay_returns_immutable_unqualified_binding(inputs):
    result = bind_miami_forecast_to_contract(**inputs)
    assert result.probability_yes == 2 / 3
    assert result.status == "UNQUALIFIED"
    assert not result.paper_eligible and not result.execution_authority
    assert result.historical_public_availability == "UNKNOWN"
    assert result.configuration_versions == ("config",)
    assert len(result.source_hashes) == 5
    with pytest.raises(FrozenInstanceError):
        result.status = "PASS"


@pytest.mark.parametrize("field", ["frozen_prediction", "recording_receipt"])
def test_altered_original_bytes_rejected(inputs, field):
    inputs[field] = replace(inputs[field], payload=inputs[field].payload + b" ")
    with pytest.raises(ValueError, match="HASH_MISMATCH"):
        bind_miami_forecast_to_contract(**inputs)


@pytest.mark.parametrize("defect", ["probability", "target", "calibrated"])
def test_rehashed_forecast_cannot_evade_replay(inputs, defect):
    saved = inputs["frozen_prediction"].decode()
    f = saved["prediction"]["forecasts"][0]
    if defect == "probability":
        f["models"]["prior_day_increment_empirical"]["samples_f"] = [99, 99, 99]
    elif defect == "target":
        f["target_at"] = "2026-09-10T23:00:00Z"
    else:
        f["calibrated"] = True
    inputs["frozen_prediction"] = artifact(saved)
    receipt = inputs["recording_receipt"].decode()
    receipt["prediction_sha256"] = inputs["frozen_prediction"].sha256
    inputs["recording_receipt"] = artifact(receipt)
    with pytest.raises(
        ValueError, match="REPLAY_MISMATCH|EXACT_FORECAST_TARGET|EARLIEST_TARGET_FREEZE"
    ):
        bind_miami_forecast_to_contract(**inputs)


@pytest.mark.parametrize("defect", ["range", "wrong_series", "rules", "close", "cap"])
def test_contract_mismatch_rejected(inputs, defect):
    original_market = inputs["market"]
    value = original_market.artifact.decode()
    market = value["market"]
    if defect == "range":
        market["strike_type"] = "between"
    elif defect == "cap":
        market["cap_strike"] = 82
    elif defect == "wrong_series":
        market["event_ticker"] = "KXTEMPNYCH-26SEP1018"
    elif defect == "rules":
        market["rules_primary"] = market["rules_primary"].replace("above", "at or above")
    else:
        market["close_time"] = "2026-09-10T23:00:00Z"
    inputs["market"] = replace(original_market, artifact=artifact(value))
    with pytest.raises(ValueError, match="CONTRACT_IDENTITY|UNSUPPORTED_RULE"):
        bind_miami_forecast_to_contract(**inputs)


@pytest.mark.parametrize(
    "defect", ["future", "after_cutoff", "wrong_city", "wrong_url", "bad_hash"]
)
def test_canonical_original_guards(inputs, defect):
    index, calibration = inputs["captures"][0]
    if defect in ("future", "after_cutoff"):
        index = replace(
            index, received_at=CUTOFF + timedelta(seconds=10 if defect == "future" else 1)
        )
    elif defect == "wrong_city":
        value = index.artifact.decode()
        value["city"] = "new_york"
        index = replace(index, artifact=artifact(value))
    elif defect == "wrong_url":
        index = replace(index, url=INDEX.replace("external-api.", "external-api.demo."))
    else:
        index = replace(index, artifact=replace(index.artifact, sha256="a" * 64))
    inputs["captures"] = ((index, calibration),)
    with pytest.raises(ValueError, match="RECEIPT|CITY|SOURCE_URL|HASH_MISMATCH"):
        bind_miami_forecast_to_contract(**inputs)


def test_freeze_after_decision_rejected(inputs):
    receipt = inputs["recording_receipt"].decode()
    receipt["prediction_recorded_at"] = (inputs["now"] + timedelta(seconds=1)).isoformat()
    inputs["recording_receipt"] = artifact(receipt)
    with pytest.raises(ValueError, match="FREEZE_CHRONOLOGY"):
        bind_miami_forecast_to_contract(**inputs)


def test_duplicate_json_keys_do_not_bind(inputs):
    source = inputs["market"]
    raw = (
        b'{"market": {}, "market": '
        + json.dumps(source.artifact.decode()["market"]).encode()
        + b"}"
    )
    inputs["market"] = replace(source, artifact=Artifact(hashlib.sha256(raw).hexdigest(), raw))
    with pytest.raises(ValueError, match="DUPLICATE_JSON_KEY"):
        bind_miami_forecast_to_contract(**inputs)


def test_rule_original_hash_is_checked(inputs):
    inputs["rule_documents"] = (Artifact("0" * 64, b"modified PDF"),)
    with pytest.raises(ValueError, match="RULE_DOCUMENT_HASH"):
        bind_miami_forecast_to_contract(**inputs)


@pytest.mark.parametrize("current", [False, True])
def test_historical_header_is_reported_but_current_mismatch_rejected(inputs, current):
    index, calibration = inputs["captures"][0]
    cal = calibration.artifact.decode()
    new = dict(cal["calibrations"][0])
    new.update(
        config_version="later",
        effective_at_ms=int((ORIGIN + timedelta(minutes=1)).timestamp() * 1000),
        published_at_ms=int(ORIGIN.timestamp() * 1000),
    )
    cal["calibrations"].append(new)
    calibration = replace(calibration, artifact=artifact(cal))
    history = index.artifact.decode()
    history["config_version"] = "later"
    if not current:
        history["timeseries"] = [
            p for p in history["timeseries"] if p["t"] < int(ORIGIN.timestamp() * 1000)
        ]
    historical = replace(index, artifact=artifact(history))
    inputs["captures"] = (
        ((historical, calibration),)
        if current
        else ((historical, calibration), (index, calibration))
    )
    if current:
        with pytest.raises(ValueError, match="CONFIGURATION_HEADER_MISMATCH"):
            bind_miami_forecast_to_contract(**inputs)
        return
    captures = [
        decode_miami_index(
            i.artifact.payload,
            c.artifact.payload,
            index_received_at=i.received_at,
            calibrations_received_at=c.received_at,
            index_units="fahrenheit",
        )
        for i, c in inputs["captures"]
    ]
    saved = inputs["frozen_prediction"].decode()
    saved["prediction"]["forecasts"] = [
        forecast_miami_prior_day(
            captures, origin_at=ORIGIN, model_input_as_of=CUTOFF, horizon_minutes=60
        )
    ]
    inputs["frozen_prediction"] = artifact(saved)
    receipt = inputs["recording_receipt"].decode()
    receipt["prediction_sha256"] = inputs["frozen_prediction"].sha256
    inputs["recording_receipt"] = artifact(receipt)
    result = bind_miami_forecast_to_contract(**inputs)
    assert result.historical_header_mismatches == (historical.artifact.sha256,)
    assert result.probability_yes == 2 / 3
    assert not result.paper_eligible


@pytest.mark.parametrize("defect", ["wrong_envelope_target", "recorded_after_earliest"])
def test_earliest_frozen_target_preserved(inputs, defect):
    saved = inputs["frozen_prediction"].decode()
    saved["target_at"] = "2026-09-10T21:30:00Z"
    receipt = inputs["recording_receipt"].decode()
    if defect == "recorded_after_earliest":
        earlier = dict(saved["prediction"]["forecasts"][0])
        earlier["target_at"] = saved["target_at"]
        saved["prediction"]["forecasts"].append(earlier)
        receipt["prediction_recorded_at"] = "2026-09-10T21:31:00Z"
        inputs["now"] = datetime(2026, 9, 10, 21, 32, tzinfo=UTC)
    inputs["frozen_prediction"] = artifact(saved)
    receipt["prediction_sha256"] = inputs["frozen_prediction"].sha256
    inputs["recording_receipt"] = artifact(receipt)
    with pytest.raises(ValueError, match="EARLIEST_TARGET_FREEZE"):
        bind_miami_forecast_to_contract(**inputs)
