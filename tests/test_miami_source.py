import hashlib
import json
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta

import pytest

from kalshi_predictor.overnight_paper.miami_binding import INDEX, MiamiOriginal
from kalshi_predictor.overnight_paper.miami_source import verify_miami_source
from kalshi_predictor.overnight_paper.provenance import Artifact
from kalshi_predictor.weather.miami_index import STATIONS

ORIGIN = datetime(2026, 9, 10, 21, tzinfo=UTC)
CUTOFF = ORIGIN + timedelta(minutes=5)


def artifact(value):
    raw = json.dumps(value).encode()
    return Artifact(hashlib.sha256(raw).hexdigest(), raw)


def receipt(original):
    return artifact(
        dict(
            url=original.url,
            status=200,
            sha256=original.artifact.sha256,
            received_at=original.received_at.isoformat(),
        )
    )


@pytest.fixture
def inputs():
    points = [
        dict(t=int(at.timestamp() * 1000), v=v, status="normal", contributors=5)
        for at, v in ((ORIGIN - timedelta(minutes=30), 82.76), (ORIGIN, 83.84))
    ]
    index = MiamiOriginal(
        artifact(
            dict(city="miami", units="fahrenheit", config_version="config", timeseries=points)
        ),
        INDEX + "?last_sec=7200",
        CUTOFF - timedelta(seconds=1),
    )
    cal = MiamiOriginal(
        artifact(
            dict(
                city="miami",
                units="celsius",
                calibrations=[
                    dict(
                        config_version="config",
                        effective_at_ms=0,
                        published_at_ms=0,
                        city_reference_c=0,
                        stations=[
                            dict(station_id=s, weight=0.2, offset_c=0) for s in sorted(STATIONS)
                        ],
                    )
                ],
            )
        ),
        INDEX + "/calibrations",
        CUTOFF - timedelta(seconds=1),
    )
    return dict(
        index=index,
        calibrations=cal,
        index_receipt=receipt(index),
        calibrations_receipt=receipt(cal),
        origin_at=ORIGIN,
        target_at=ORIGIN + timedelta(hours=1),
        model_input_as_of=CUTOFF,
        decision_at=CUTOFF,
        now=CUTOFF,
    )


def test_original_clock_fresh_later_clock_stale_without_authority(inputs):
    fresh = verify_miami_source(**inputs)
    assert fresh.state == "FRESH" and fresh.source_healthy
    assert fresh.scope == "SOURCE_HEALTH_ONLY"
    assert not fresh.paper_eligible and not fresh.execution_authority
    assert fresh.provider_generated_at is None and fresh.provider_updated_at is None
    assert fresh.historical_public_availability == "UNKNOWN"
    with pytest.raises(FrozenInstanceError):
        fresh.state = "PASS"
    inputs["now"] = ORIGIN + timedelta(minutes=36)
    stale = verify_miami_source(**inputs)
    assert stale.state == "STALE" and not stale.source_healthy
    assert len(stale.reasons) == 2
    assert stale.index_sha256 == fresh.index_sha256


@pytest.mark.parametrize("defect", ["hash", "future", "receipt_relabel", "bad_status", "wrong_url"])
def test_original_receipt_errors(inputs, defect):
    index = inputs["index"]
    if defect == "hash":
        index = replace(
            index, artifact=replace(index.artifact, payload=index.artifact.payload + b" ")
        )
    elif defect == "future":
        index = replace(index, received_at=CUTOFF + timedelta(seconds=1))
        inputs["index_receipt"] = receipt(index)
    elif defect == "receipt_relabel":
        index = replace(index, received_at=CUTOFF)
    elif defect == "bad_status":
        row = inputs["index_receipt"].decode()
        row["status"] = 500
        inputs["index_receipt"] = artifact(row)
    else:
        index = replace(index, url=INDEX + "?from=1&to=2")
        inputs["index_receipt"] = receipt(index)
    inputs["index"] = index
    assert verify_miami_source(**inputs).state == "INVALID"


@pytest.mark.parametrize("defect", ["quorum", "incomplete", "gap", "header", "city"])
def test_point_errors(inputs, defect):
    index = inputs["index"]
    row = index.artifact.decode()
    if defect == "quorum":
        row["timeseries"][-1]["contributors"] = 3
    elif defect == "incomplete":
        row["timeseries"][-1]["status"] = "incomplete"
        del row["timeseries"][-1]["v"]
    elif defect == "gap":
        row["timeseries"].pop(0)
    elif defect == "city":
        row["city"] = "new_york"
    else:
        row["config_version"] = "other"
    index = replace(index, artifact=artifact(row))
    inputs["index"] = index
    inputs["index_receipt"] = receipt(index)
    assert verify_miami_source(**inputs).state == "INVALID"


def test_late_configuration_cannot_support_origin(inputs):
    cal = inputs["calibrations"]
    row = cal.artifact.decode()
    row["calibrations"][0]["published_at_ms"] = int(
        (ORIGIN + timedelta(seconds=1)).timestamp() * 1000
    )
    cal = replace(cal, artifact=artifact(row))
    inputs["calibrations"] = cal
    inputs["calibrations_receipt"] = receipt(cal)
    assert verify_miami_source(**inputs).state == "INVALID"


@pytest.mark.parametrize("field", ["origin_at", "target_at"])
def test_exact_hour_and_target_not_occurrence_plus_five(inputs, field):
    inputs[field] += timedelta(minutes=5)
    assert verify_miami_source(**inputs).state == "INVALID"


def test_fresh_download_does_not_refresh_old_origin(inputs):
    late = ORIGIN + timedelta(minutes=36)
    for field in ("index", "calibrations"):
        inputs[field] = replace(inputs[field], received_at=late)
        inputs[field + "_receipt"] = receipt(inputs[field])
    inputs.update(model_input_as_of=late, decision_at=late, now=late)
    result = verify_miami_source(**inputs)
    assert result.state == "STALE"
    assert result.reasons == ("MIAMI_SOURCE_ORIGIN_OLDER_THAN_TEN_MINUTES",)
