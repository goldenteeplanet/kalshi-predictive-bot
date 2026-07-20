from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LOCK_PATH = "/var/lib/kalshi-bot/kalshi-writer.lock"


def test_guarded_runtime_writers_share_one_lock() -> None:
    gh1 = (ROOT / "scripts/cloud/kalshi-gh1-drain.sh").read_text(encoding="utf-8")
    weather = (ROOT / "scripts/cloud/kalshi-nyc-weather-refresh.sh").read_text(
        encoding="utf-8"
    )

    assert LOCK_PATH in gh1
    assert LOCK_PATH in weather
    assert "db-writer-monitor --json" in gh1
    assert "db-writer-monitor --json" in weather
    assert "gh1-websocket-orderbook-drain --apply" in gh1
    assert "ingest-weather --location-key new_york" in weather
    assert "build-weather-features --location-key new_york" in weather


def test_systemd_units_use_guarded_writers_and_paper_only_flags() -> None:
    gh1 = (ROOT / "deploy/systemd/kalshi-gh1-websocket-drain.service").read_text(
        encoding="utf-8"
    )
    weather = (
        ROOT / "deploy/systemd/kalshi-nyc-weather-runtime-refresh.service"
    ).read_text(encoding="utf-8")
    timer = (
        ROOT / "deploy/systemd/kalshi-nyc-weather-runtime-refresh.timer"
    ).read_text(encoding="utf-8")

    assert "scripts/cloud/kalshi-gh1-drain.sh" in gh1
    assert "scripts/cloud/kalshi-nyc-weather-refresh.sh" in weather
    assert "Environment=EXECUTION_ENABLED=false" in weather
    assert "Environment=AUTOPILOT_ENABLED=false" in weather
    assert "OnUnitActiveSec=15min" in timer
