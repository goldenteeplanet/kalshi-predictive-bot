from kalshi_predictor.weather import providers


def test_noaa_fetch_retries_with_bounded_backoff(monkeypatch) -> None:
    attempts = 0
    sleeps: list[float] = []

    class FailingClient:
        def __init__(self, **_):
            nonlocal attempts
            attempts += 1

        def __enter__(self):
            raise ConnectionError("temporary NOAA outage")

        def __exit__(self, *_):
            return None

    monkeypatch.setattr(providers.httpx, "Client", FailingClient)

    result = providers.fetch_noaa_hourly_forecast(
        location_key="new_york",
        latitude=40.77,
        longitude=-73.87,
        user_agent="test@example.com",
        max_attempts=3,
        retry_backoff_seconds=1,
        sleep_fn=sleeps.append,
    )

    assert attempts == 3
    assert sleeps == [1, 2]
    assert result.forecasts == []
    assert result.errors == ["NOAA failed after 3 attempt(s): temporary NOAA outage"]
