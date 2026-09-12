import copy
import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from kalshi_predictor.weather.miami_index import STATIONS, decode_miami_index

START = 1788739200000
RECEIPT = datetime.fromtimestamp((START + 3600000) / 1000, UTC)


def originals():
    stations = [{"station_id": s, "weight": 0.2, "offset_c": 0} for s in sorted(STATIONS)]
    configs = {
        "city": "miami",
        "units": "celsius",
        "calibrations": [
            {
                "config_version": "older",
                "published_at_ms": START - 60000,
                "effective_at_ms": START,
                "stations": stations,
                "city_reference_c": 0,
            },
            {
                "config_version": "newest",
                "published_at_ms": START + 240000,
                "effective_at_ms": START + 300000,
                "stations": stations,
                "city_reference_c": 0,
            },
        ],
    }
    index = {
        "city": "miami",
        "config_version": "newest",
        "timeseries": [
            {"t": START + 60000, "v": 81.68, "status": "normal", "contributors": 5},
            {"t": START + 300000, "status": "incomplete"},
            {"t": START + 600000, "v": 82.04, "status": "degraded", "contributors": 4},
        ],
    }
    return index, configs


def decode(index, configs, **kwargs):
    return decode_miami_index(
        json.dumps(index).encode(),
        json.dumps(configs).encode(),
        index_received_at=RECEIPT,
        calibrations_received_at=RECEIPT,
        index_units="fahrenheit",
        **kwargs,
    )


def test_original_hashes_availability_gaps_and_point_configuration():
    index, configs = originals()
    result = decode(index, configs)
    assert result.index_sha256 == hashlib.sha256(json.dumps(index).encode()).hexdigest()
    assert result.calibrations_sha256 == hashlib.sha256(json.dumps(configs).encode()).hexdigest()
    assert result.available_at == RECEIPT
    assert result.historical_public_availability == "UNKNOWN"
    assert [p.config_version for p in result.points] == ["older", "newest", "newest"]
    assert result.latest_config_version == "newest"
    assert result.latest_config_matches_last_point is True
    assert [p.value_f for p in result.points] == [Decimal("81.68"), None, Decimal("82.04")]
    assert len(result.canonical_points) == 2
    assert len(result.points) == 3  # no minute gap fill
    assert not any(p.station_observation_times_complete for p in result.points)


@pytest.mark.parametrize(
    "change",
    [
        "city",
        "units",
        "duplicate",
        "future",
        "precision",
        "quorum",
        "unknown_status",
        "incomplete_value",
        "config_future",
        "config_uncovered",
        "config_duplicate",
        "nonfinite",
    ],
)
def test_invalid_capture_refused(change):
    index, configs = originals()
    if change == "city":
        index["city"] = "new_york"
    elif change == "units":
        configs["units"] = "fahrenheit"
    elif change == "duplicate":
        index["timeseries"].append(copy.deepcopy(index["timeseries"][-1]))
    elif change == "future":
        index["timeseries"][-1]["t"] = START + 7200000
    elif change == "precision":
        index["timeseries"][0]["v"] = 81.681
    elif change == "quorum":
        index["timeseries"][0]["contributors"] = 3
    elif change == "unknown_status":
        index["timeseries"][0]["status"] = "okay"
    elif change == "incomplete_value":
        index["timeseries"][1]["v"] = 0
    elif change == "config_future":
        configs["calibrations"][0]["published_at_ms"] = START + 7200000
    elif change == "config_uncovered":
        index["timeseries"][0]["t"] = START - 120000
    elif change == "config_duplicate":
        configs["calibrations"].append(copy.deepcopy(configs["calibrations"][0]))
    elif change == "nonfinite":
        index["timeseries"][0]["v"] = float("nan")
    with pytest.raises(ValueError):
        decode(index, configs)


def test_missing_station_observation_clock_stays_unknown():
    index, configs = originals()
    index["timeseries"][0]["stations"] = [
        {
            "station_id": s,
            "source": "hf_asos",
            "temp_f": 81.68,
            "received_at_ms": START + 120000,
            "code": "ok",
        }
        for s in sorted(STATIONS)
    ]
    result = decode(index, configs)
    assert not result.points[0].station_observation_times_complete
    index["timeseries"][0]["stations"][0]["obs_time_ms"] = START
    with pytest.raises(ValueError, match="PRIMARY_EVENT_MINUTE"):
        decode(index, configs)


def test_late_published_configuration_preserved_without_historical_availability():
    index, configs = originals()
    configs["calibrations"][0]["published_at_ms"] = START + 120000
    result = decode(index, configs)
    assert result.late_published_configurations == ("older",)
    assert not result.points[0].configuration_published_by_event
    assert result.historical_public_availability == "UNKNOWN"


def test_naive_receipt_and_duplicate_json_keys_refused():
    index, configs = originals()
    with pytest.raises(ValueError, match="AWARE_RECEIPT"):
        decode_miami_index(
            json.dumps(index).encode(),
            json.dumps(configs).encode(),
            index_received_at=RECEIPT.replace(tzinfo=None),
            calibrations_received_at=RECEIPT,
        )


def test_endpoint_units_require_explicit_binding_and_reject_contradiction():
    index, configs = originals()
    with pytest.raises(ValueError, match="UNITS_MISMATCH"):
        decode_miami_index(
            json.dumps(index).encode(),
            json.dumps(configs).encode(),
            index_received_at=RECEIPT,
            calibrations_received_at=RECEIPT,
        )
    index["units"] = "celsius"
    with pytest.raises(ValueError, match="UNITS_MISMATCH"):
        decode(index, configs)
    with pytest.raises(ValueError, match="DUPLICATE_JSON"):
        decode_miami_index(
            b'{"city":"miami","city":"miami"}',
            json.dumps(configs).encode(),
            index_received_at=RECEIPT,
            calibrations_received_at=RECEIPT,
        )


def test_historical_header_disagreement_is_explicit_not_assigned_to_old_points():
    index, configs = originals()
    index["timeseries"] = index["timeseries"][:1]
    result = decode(index, configs)
    assert result.points[0].config_version == "older"
    assert result.latest_config_matches_last_point is False
