import json

import pytest

from kalshi_predictor.provenance.triage import build_offline_provenance_triage


def test_prov15d_groups_and_prioritizes_exact_failures():
    report = {"phase": "PROV-15B", "rows": [
        _row("BTC-A", "crypto_v2", "2.0.0", [
            "OBSERVATION_REFERENCE_MISSING", "SNAPSHOT_STALE"
        ], None, 121),
        _row("NYC-A", "weather_v2", "2.1.0", ["OBSERVATION_STALE"], 901, 20),
        _row("BTC-B", "crypto_v2", "2.0.0", ["OBSERVATION_REFERENCE_MISSING"], None, 10),
    ]}
    triage = build_offline_provenance_triage(report)
    assert triage["groups"]["by_model"] == {"crypto_v2": 3, "weather_v2": 1}
    assert triage["groups"]["by_revision"] == {
        "crypto_v2@2.0.0": 3, "weather_v2@2.1.0": 1
    }
    assert triage["priorities"][0]["cause"] == "OBSERVATION_REFERENCE_MISSING"
    assert triage["priorities"][0]["affected_tickers"] == ["BTC-A", "BTC-B"]
    assert triage["database_access"] is False


def test_prov15d_is_deterministic_and_does_not_mutate_source():
    report = {"phase": "PROV-15B", "rows": [
        _row("NYC-A", "weather_v2", "2.0.0", ["SNAPSHOT_TIMESTAMP_INVALID"], 60, None)
    ]}
    before = json.dumps(report, sort_keys=True)
    assert build_offline_provenance_triage(report) == build_offline_provenance_triage(report)
    assert json.dumps(report, sort_keys=True) == before


def test_prov15d_rejects_malformed_report_rows():
    with pytest.raises(ValueError, match="rows list"):
        build_offline_provenance_triage({})
    with pytest.raises(ValueError, match="failures must be a list"):
        build_offline_provenance_triage({"rows": [{}]})


def _row(ticker, model, version, failures, observation_age, snapshot_age):
    return {
        "ticker": ticker, "model_name": model, "model_version": version,
        "failures": failures, "observation_age_seconds": observation_age,
        "snapshot_age_seconds": snapshot_age,
    }
