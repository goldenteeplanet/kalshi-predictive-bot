import json
from functools import partial

import httpx
import pytest

from kalshi_predictor.research import provider_trial
from kalshi_predictor.research.bls import BLSResearchClient
from kalshi_predictor.research.oddpool import OddpoolResearchClient


def test_capture_archives_original_and_keeps_key_out_of_outputs(tmp_path, monkeypatch):
    secret = "synthetic_trial_key_123456"
    key_file = tmp_path / "key.txt"
    key_file.write_text(secret)
    raw = b'[{"market_id":"EXAMPLE","exchange":"kalshi","status":"active","question":"Example?"}]'

    def serve(request):
        assert request.headers["X-API-Key"] == secret
        assert request.method == "GET"
        return httpx.Response(200, content=raw, headers={"Content-Type": "application/json"})

    monkeypatch.setattr(
        provider_trial,
        "OddpoolResearchClient",
        partial(OddpoolResearchClient, transport=httpx.MockTransport(serve)),
    )
    output = tmp_path / "captures"
    result = provider_trial.capture("oddpool", key_file, output)
    assert result["state"] == "CAPTURED"
    assert result["runtime_certified"] is False
    assert next(output.glob("*.original.json")).read_bytes() == raw
    assert all(secret not in path.read_text() for path in output.iterdir())


def test_entitlement_failure_is_recorded_without_raw_server_error(tmp_path, monkeypatch):
    key_file = tmp_path / "key.txt"
    key_file.write_text("synthetic_trial_key_123456")
    monkeypatch.setattr(
        provider_trial,
        "OddpoolResearchClient",
        partial(
            OddpoolResearchClient,
            transport=httpx.MockTransport(
                lambda request: httpx.Response(403, text="private server detail")
            ),
        ),
    )
    output = tmp_path / "captures"
    result = provider_trial.capture("oddpool", key_file, output)
    assert result["state"] == "PROVIDER_ERROR"
    assert not list(output.glob("*.original.json"))
    stored = json.loads(next(output.glob("*.summary.json")).read_text())
    assert "private server detail" not in json.dumps(stored)


def test_invalid_key_never_becomes_a_request(tmp_path):
    key_file = tmp_path / "key.txt"
    key_file.write_text("a label and multiple words")
    with pytest.raises(ValueError, match="PROVIDER_KEY_FILE_INVALID"):
        provider_trial.capture("oddpool", key_file, tmp_path / "captures")


def test_bls_capture_preserves_period_and_footnotes_without_vintage_claim(tmp_path, monkeypatch):
    secret = "a" * 32
    key_file = tmp_path / "key.txt"
    key_file.write_text(secret)

    def serve(request):
        query = json.loads(request.content)
        assert request.method == "POST"
        assert query["registrationkey"] == secret
        assert secret not in str(request.url)
        return httpx.Response(
            200,
            json={
                "status": "REQUEST_SUCCEEDED",
                "message": [],
                "Results": {
                    "series": [
                        {
                            "seriesID": "CUUR0000SA0",
                            "data": [
                                {
                                    "year": query["startyear"],
                                    "period": "M01",
                                    "periodName": "January",
                                    "value": "300.123",
                                    "footnotes": [{"code": "P", "text": "Preliminary."}],
                                }
                            ],
                        }
                    ]
                },
            },
        )

    monkeypatch.setattr(
        provider_trial,
        "BLSResearchClient",
        partial(BLSResearchClient, transport=httpx.MockTransport(serve)),
    )
    output = tmp_path / "captures"
    result = provider_trial.capture("bls", key_file, output)
    assert result["state"] == "CAPTURED"
    assert result["samples"][0]["footnotes"][0]["code"] == "P"
    assert result["historical_availability_verified"] is False
    assert result["publication_time_verified"] is False
    assert result["research_features"]["contribution"] == "FEATURE_PRESENT"
    assert result["research_features"]["source_sha256"] == result["source_sha256"]
    assert result["research_features"]["runtime_certified"] is False
    assert all(secret not in path.read_text() for path in output.iterdir())
