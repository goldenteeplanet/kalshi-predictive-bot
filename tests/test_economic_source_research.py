import hashlib
import json
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest

from kalshi_predictor.economic.source_research import (
    EconomicSourceError,
    build_economic_source_features,
)
from kalshi_predictor.research.bls import BLSOriginal, BLSResponse
from kalshi_predictor.research.fred import FREDOriginal, FREDResponse

NOW = datetime(2026, 9, 8, 12, tzinfo=UTC)


def bls(values=("300", "306"), series="CUUR0000SA0"):
    data = {
        "status": "REQUEST_SUCCEEDED",
        "Results": {
            "series": [
                {
                    "seriesID": series,
                    "data": [
                        {
                            "year": "2026",
                            "period": f"M{7 + i:02}",
                            "value": v,
                            "footnotes": [{"code": "P", "text": "Preliminary"}],
                        }
                        for i, v in enumerate(values)
                    ],
                }
            ]
        },
    }
    raw = json.dumps(data).encode()
    return BLSResponse(
        "observations",
        series,
        BLSOriginal(
            "https://api.bls.gov/publicAPI/v2/timeseries/data/",
            NOW,
            hashlib.sha256(raw).hexdigest(),
            raw,
        ),
        data,
    )


def fred():
    params = {
        "series_id": "CPIAUCSL",
        "file_type": "json",
        "realtime_start": "2025-09-08",
        "realtime_end": "2025-09-08",
        "observation_start": "2025-07-01",
        "observation_end": "2025-08-31",
    }
    data = {
        "observations": [
            {
                "date": f"2025-{month:02}-01",
                "value": value,
                "realtime_start": "2025-09-08",
                "realtime_end": "2025-09-08",
            }
            for month, value in [(7, "300"), (8, "303")]
        ]
    }
    raw = json.dumps(data).encode()
    return FREDResponse(
        "observations",
        "CPIAUCSL",
        FREDOriginal(
            str(httpx.URL("https://api.stlouisfed.org/fred/series/observations", params=params)),
            NOW,
            hashlib.sha256(raw).hexdigest(),
            raw,
        ),
        data,
    )


def rebound(response, data):
    raw = json.dumps(data).encode()
    return replace(
        response,
        data=data,
        original=replace(response.original, payload=raw, sha256=hashlib.sha256(raw).hexdigest()),
    )


def test_actual_previous_uses_existing_momentum_not_surprise():
    result = build_economic_source_features(bls(), decision_at=NOW)
    assert result.momentum_score == Decimal("0.02")
    assert result.concept == "cpi_all_items_level"
    assert result.units == "index_1982_84_100"
    assert result.contribution == "FEATURE_PRESENT"
    assert result.momentum_status == "ACTUAL_VS_PREVIOUS_MOMENTUM_NOT_CONSENSUS_SURPRISE"
    assert not hasattr(result, "surprise_score")
    assert not hasattr(result, "confidence_score")
    assert not hasattr(result, "probability")
    assert result.provider_timestamp is None
    assert result.release_identity is None
    assert result.research_only and not result.runtime_certified
    assert "Preliminary" in result.observations[-1].footnotes_json
    with pytest.raises(FrozenInstanceError):
        result.units = "percent"


def test_alfred_received_today_cannot_be_used_for_old_decision():
    response = fred()
    with pytest.raises(EconomicSourceError, match="NOT_RECEIVED_AT_DECISION"):
        build_economic_source_features(response, decision_at=NOW.replace(year=2025))
    result = build_economic_source_features(response, decision_at=NOW)
    assert result.momentum_score == Decimal("0.01")
    assert result.observations[0].realtime_start == "2025-09-08"
    assert result.received_at == NOW
    assert not result.historical_availability_verified


@pytest.mark.parametrize(
    "series,concept,units",
    [
        ("LNS14000000", "unemployment_rate", "percent"),
        ("CES0000000001", "nonfarm_payroll_level", "thousand_persons"),
        ("CES0500000003", "average_hourly_earnings_level", "usd_per_hour"),
        ("WPSFD4", "ppi_final_demand_level", "index_2009_11_100"),
    ],
)
def test_exact_concepts(series, concept, units):
    result = build_economic_source_features(bls(series=series), decision_at=NOW)
    assert (result.concept, result.units) == (concept, units)


@pytest.mark.parametrize("values", [("300", "-"), (".", "306"), ("300",)])
def test_missing_never_zero_and_no_spurious_momentum(values):
    result = build_economic_source_features(bls(values), decision_at=NOW)
    assert result.momentum_score is None


def test_score_is_bounded():
    result = build_economic_source_features(bls(("1", "100")), decision_at=NOW)
    assert result.momentum_score == 1


@pytest.mark.parametrize("change", ["hash", "body", "identity", "url", "clock"])
def test_original_failures(change):
    response = bls()
    if change == "hash":
        response = replace(response, original=replace(response.original, sha256="0" * 64))
    elif change == "body":
        response = replace(response, data={})
    elif change == "identity":
        response = replace(response, series_id="LNS14000000")
    elif change == "url":
        response = replace(response, original=replace(response.original, url="https://evil.test"))
    else:
        response = replace(
            response, original=replace(response.original, received_at=NOW + timedelta(seconds=1))
        )
    with pytest.raises(EconomicSourceError):
        build_economic_source_features(response, decision_at=NOW)


@pytest.mark.parametrize("change", ["duplicate", "annual", "future", "nan", "infinite"])
def test_invalid_rows(change):
    response = bls()
    data = response.data
    rows = data["Results"]["series"][0]["data"]
    if change == "duplicate":
        rows[1]["period"] = rows[0]["period"]
    elif change == "annual":
        rows[1]["period"] = "M13"
    elif change == "future":
        rows[1]["year"] = "2027"
    else:
        rows[1]["value"] = "NaN" if change == "nan" else "Infinity"
    with pytest.raises(EconomicSourceError):
        build_economic_source_features(rebound(response, data), decision_at=NOW)


def test_gap_does_not_become_monthly_momentum():
    response = bls()
    response.data["Results"]["series"][0]["data"][0]["period"] = "M05"
    result = build_economic_source_features(rebound(response, response.data), decision_at=NOW)
    assert result.momentum_score is None


@pytest.mark.parametrize("query", ["units=pch", "frequency=q", "api_key=secret"])
def test_fred_transform_rejected(query):
    response = fred()
    response = replace(
        response, original=replace(response.original, url=response.original.url + "&" + query)
    )
    with pytest.raises(EconomicSourceError, match="URL_OR_TRANSFORM"):
        build_economic_source_features(response, decision_at=NOW)


def test_wrong_vintage_rejected():
    response = fred()
    response.data["observations"][1]["realtime_start"] = "2025-10-01"
    response.data["observations"][1]["realtime_end"] = "2025-10-01"
    with pytest.raises(EconomicSourceError, match="VINTAGE_MISMATCH"):
        build_economic_source_features(rebound(response, response.data), decision_at=NOW)
