import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import pytest
from test_microstructure_research_capture import harness  # noqa: F401

from kalshi_predictor.crypto.shared_capture import CandleOriginal, SharedCryptoInputs
from kalshi_predictor.forecasting.crypto_v3_independent import CryptoTarget
from kalshi_predictor.microstructure import research_capture as capture

NOW = datetime(2026, 9, 10, 23, tzinfo=UTC)


def inputs():
    rows = [
        [int((NOW - timedelta(minutes=300 - i)).timestamp()), 99, 102, 100, 100 + (i % 7) / 10, 1]
        for i in range(300)
    ]
    raw = json.dumps(rows).encode()
    query = urlencode(
        {
            "granularity": 60,
            "start": (NOW - timedelta(minutes=300)).isoformat(),
            "end": NOW.isoformat(),
        }
    )
    return SharedCryptoInputs(
        CryptoTarget("SOL", "ABOVE", NOW + timedelta(hours=1), threshold=100),
        (
            CandleOriginal(
                raw,
                hashlib.sha256(raw).hexdigest(),
                "https://api.exchange.coinbase.com/products/SOL-USD/candles?" + query,
                NOW,
            ),
        ),
    )


def test_shared_actual_models_and_one_cutoff(harness):  # noqa: F811
    kwargs, urls, _, _ = harness
    original_get = kwargs["get"]

    def get(url):
        raw, status = original_get(url)
        value = json.loads(raw)
        if "market" in value:
            value["market"].update(strike_type="greater", floor_strike=100)
        return json.dumps(value).encode(), status

    kwargs.update(get=get, crypto_inputs=inputs())
    report = capture.run(**kwargs)
    assert len(urls) == 6
    assert report["independent_forecast_present"]
    proof = json.loads((kwargs["output"] / "code-proof.json").read_bytes())
    paths = {row["path"] for row in proof["files"]}
    assert {
        "src/kalshi_predictor/crypto/shared_capture.py",
        "src/kalshi_predictor/forecasting/crypto_v3_independent.py",
        "src/kalshi_predictor/crypto/distribution_model.py",
    } <= paths
    frozen = json.loads((kwargs["output"] / "frozen/prediction.json").read_bytes())
    prediction = frozen["prediction"]
    crypto = prediction["shared_crypto"]
    assert crypto["model"] == "crypto_v3_independent"
    assert set(crypto["comparisons"]) == {
        "gaussian_log_returns",
        "student_t_df3",
        "empirical_matched_horizon",
        "existing_distribution_zero_drift",
    }
    assert crypto["generated_at"] == frozen["model_input_as_of"]
    assert (
        prediction["microstructure"]["feature_json"]["model_input_as_of"] == crypto["generated_at"]
    )
    assert crypto["actual_computed_at"] >= frozen["model_input_as_of"]
    assert prediction["component"]["model_name"] == "market_implied_v1"
    assert prediction["ensemble"]["model_name"] == "ensemble_v2"
    assert not prediction["crypto_v2"]["model_invoked"]
    assert not report["execution_authority"]


@pytest.mark.parametrize("failure", ["hash", "future", "symbol", "stale", "duplicate", "unclosed"])
def test_invalid_originals(failure):
    value = inputs()
    original = value.originals[0]
    cutoff = NOW
    if failure == "hash":
        original = replace(original, sha256="0" * 64)
    elif failure == "future":
        original = replace(original, received_at=NOW + timedelta(seconds=1))
    elif failure == "symbol":
        original = replace(original, url=original.url.replace("SOL-USD", "BTC-USD"))
    elif failure == "stale":
        cutoff += timedelta(minutes=6)
    elif failure == "duplicate":
        value = replace(value, originals=(original, original))
    elif failure == "unclosed":
        original = replace(original, received_at=NOW - timedelta(seconds=1))
    if failure != "duplicate":
        value = replace(value, originals=(original,))
    with pytest.raises(ValueError):
        value.prices(cutoff)


def test_invalid_history_before_requests(harness):  # noqa: F811
    kwargs, urls, _, _ = harness
    value = inputs()
    kwargs["crypto_inputs"] = replace(
        value, originals=(replace(value.originals[0], sha256="0" * 64),)
    )
    with pytest.raises(ValueError, match="CANDLE_ORIGINAL"):
        capture.run(**kwargs)
    assert not urls


@pytest.mark.parametrize(
    "change",
    [
        {"floor_strike": 101},
        {"strike_type": "less"},
        {"ticker": "KXBTC-FUTURE"},
        {"close_time": "2026-09-11T01:00:00Z"},
    ],
)
def test_contract_binding(change):
    market = dict(
        ticker="KXSOLE-FUTURE",
        strike_type="greater",
        floor_strike=100,
        close_time="2026-09-11T00:00:00Z",
    )
    market.update(change)
    with pytest.raises(ValueError):
        inputs().bind_market(market)


def test_manifest_paths_and_original_identity(tmp_path):
    from dataclasses import asdict

    from kalshi_predictor.crypto.shared_capture_manifest import load_crypto_manifest

    value = inputs()
    original = value.originals[0]
    (tmp_path / "candles.json").write_bytes(original.raw)
    manifest = {
        "schema": "shared-crypto-input-manifest-v1",
        "target": asdict(value.target),
        "originals": [
            {
                "path": "candles.json",
                "sha256": original.sha256,
                "url": original.url,
                "received_at": original.received_at.isoformat(),
                "status": 200,
            }
        ],
    }
    path = tmp_path / "inputs.json"
    path.write_text(json.dumps(manifest, default=str))
    assert load_crypto_manifest(path).prices(NOW) == value.prices(NOW)
    manifest["originals"][0]["path"] = "../outside.json"
    path.write_text(json.dumps(manifest, default=str))
    with pytest.raises(ValueError, match="OUTSIDE_MANIFEST"):
        load_crypto_manifest(path)
