import json

import httpx
import pytest
from test_paper_release_preparation import original_inputs

from kalshi_predictor.overnight_paper import discovery
from kalshi_predictor.overnight_paper.acquisition import collect_weather_preparation


@pytest.mark.parametrize("stale", [False, True])
def test_actual_public_archive_preserves_originals_and_skips_stale_book(
    tmp_path, monkeypatch, stale
):
    ticker, sources, _ = original_inputs(stale=stale)
    originals = {json.loads(s.payload)["url"]: json.loads(s.payload)["body"] for s in sources}
    calls = []
    client = httpx.Client

    def response(request):
        calls.append(str(request.url))
        assert request.method == "GET"
        assert "authorization" not in request.headers
        return httpx.Response(200, json=originals[str(request.url)])

    monkeypatch.setattr(discovery.time, "sleep", lambda _: None)
    monkeypatch.setattr(
        discovery.httpx,
        "Client",
        lambda **kwargs: client(transport=httpx.MockTransport(response), **kwargs),
    )
    captured = collect_weather_preparation(tmp_path / "capture", ticker=ticker)
    assert all(source.valid() for source in captured)
    assert len(captured) == (6 if stale else 7)
    assert any(url.endswith("/orderbook") for url in calls) is not stale
    assert all("original_body_sha256" in json.loads(source.payload) for source in captured)


@pytest.mark.parametrize(
    "url",
    [
        "https://api.weather.gov.evil.example/stations/KNYC",
        "https://api.weather.gov/stations/KNYC?redirect=evil",
        "https://user:password@api.weather.gov/stations/KNYC",
        "https://api.weather.gov/../../portfolio/orders",
    ],
)
def test_weather_extension_refuses_other_origins_and_routes(tmp_path, url):
    public = discovery.PublicArchive(tmp_path / "capture")
    with pytest.raises(ValueError):
        public.get(url)
    assert public.receipts == []
