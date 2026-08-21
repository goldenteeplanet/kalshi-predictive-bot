from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCK_PATH = "/var/lib/kalshi-bot/kalshi-writer.lock"
GH1_STATE_DIRECTORY = "/var/lib/kalshi-bot-gh1/staging"


def test_guarded_runtime_writers_share_one_lock() -> None:
    gh1 = (ROOT / "scripts/cloud/kalshi-gh1-drain.sh").read_text(encoding="utf-8")
    weather = (ROOT / "scripts/cloud/kalshi-nyc-weather-refresh.sh").read_text(encoding="utf-8")

    assert LOCK_PATH in gh1
    assert LOCK_PATH in weather
    assert "db-writer-monitor --json" in gh1
    assert "db-writer-monitor --json" in weather
    assert "gh1-websocket-orderbook-drain --apply" in gh1
    assert (
        "for location in new_york chicago miami austin los_angeles boston washington_dc"
        in weather
    )
    assert 'ingest-weather --location-key "$location"' in weather
    assert 'build-weather-features --location-key "$location" --limit 200' in weather


def test_systemd_units_use_guarded_writers_and_paper_only_flags() -> None:
    gh1_drain = (ROOT / "deploy/systemd/kalshi-gh1-websocket-drain.service").read_text(
        encoding="utf-8"
    )
    gh1_watch = (ROOT / "deploy/systemd/kalshi-gh1-websocket-watch.service").read_text(
        encoding="utf-8"
    )
    weather = (ROOT / "deploy/systemd/kalshi-nyc-weather-runtime-refresh.service").read_text(
        encoding="utf-8"
    )
    timer = (ROOT / "deploy/systemd/kalshi-nyc-weather-runtime-refresh.timer").read_text(
        encoding="utf-8"
    )

    assert "scripts/cloud/kalshi-gh1-drain.sh" in gh1_drain
    assert "StateDirectory=kalshi-bot-gh1" in gh1_drain
    assert f"KALSHI_WEBSOCKET_STAGING_DIR={GH1_STATE_DIRECTORY}" in gh1_drain
    assert "StateDirectory=kalshi-bot-gh1" in gh1_watch
    assert f"KALSHI_WEBSOCKET_STAGING_DIR={GH1_STATE_DIRECTORY}" in gh1_watch
    assert "/var/lib/kalshi-bot-gh1/watch/status.json" in gh1_watch
    assert "--max-markets-per-series 30" in gh1_watch
    assert "--max-quoted-per-series 6" in gh1_watch
    assert "--discovery-refresh-seconds 180" in gh1_watch
    assert "reports/phase_gh1" not in gh1_drain
    assert "reports/phase_gh1" not in gh1_watch
    assert "scripts/cloud/kalshi-nyc-weather-refresh.sh" in weather
    assert "Environment=EXECUTION_ENABLED=false" in weather
    assert "Environment=AUTOPILOT_ENABLED=false" in weather
    assert "OnCalendar=*-*-* *:00/15:00" in timer


def test_weather_and_gh2_timers_keep_restart_safe_writer_separation() -> None:
    weather_timer = (
        ROOT / "deploy/systemd/kalshi-nyc-weather-runtime-refresh.timer"
    ).read_text(encoding="utf-8")
    weather_service = (
        ROOT / "deploy/systemd/kalshi-nyc-weather-runtime-refresh.service"
    ).read_text(encoding="utf-8")
    gh2_timer = (ROOT / "deploy/systemd/kalshi-gh2-decision-refresh.timer").read_text(
        encoding="utf-8"
    )

    assert "OnCalendar=*-*-* *:00/15:00" in weather_timer
    assert "OnCalendar=*-*-* *:06/15:00" in gh2_timer
    assert "OnBootSec=" not in weather_timer + gh2_timer
    assert "OnUnitActiveSec=" not in weather_timer + gh2_timer
    assert "RandomizedDelaySec=30s" in weather_timer
    assert "RandomizedDelaySec=30s" in gh2_timer
    assert "Persistent=false" in weather_timer
    assert "Persistent=false" in gh2_timer

    minimum_separation_seconds = (6 * 60) - 30
    weather_timeout_seconds = 5 * 60
    assert "TimeoutStartSec=5min" in weather_service
    assert minimum_separation_seconds > weather_timeout_seconds
