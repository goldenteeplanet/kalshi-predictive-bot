from pathlib import Path

SCRIPT = (
    Path(__file__).parents[1] / "scripts" / "local" / "kalshi-fixed-rate-refresh.sh"
).read_text(encoding="utf-8")


def test_scheduler_enforces_completion_based_cooldown() -> None:
    assert "KALSHI_MIN_POST_CYCLE_COOLDOWN_SECONDS:-60" in SCRIPT
    assert 'cooldown_start_epoch="$((now_epoch + MIN_POST_CYCLE_COOLDOWN_SECONDS))"' in SCRIPT
    assert 'next_start_epoch="$cooldown_start_epoch"' in SCRIPT


def test_weather_gate_uses_small_bounded_batches() -> None:
    assert "run_health_stage weather_gate_diagnostics 60 timeout 60s" in SCRIPT
    assert "run_health_stage weather_gate_post_preflight 60 timeout 60s" in SCRIPT
    assert SCRIPT.count("--deadline-seconds 50 --batch-size 2") == 2


def test_targeted_capture_is_rate_limited_and_sharded() -> None:
    assert "run_health_stage targeted_crypto_capture_major 75 timeout 75s" in SCRIPT
    assert "run_health_stage targeted_crypto_capture_alt 75 timeout 75s" in SCRIPT
    assert "--series KXBTC,KXETH" in SCRIPT
    assert "--series KXSOLE,KXXRP,KXDOGE" in SCRIPT
    assert SCRIPT.count("--coherence-ms 2500 --max-workers 3") == 2
    assert SCRIPT.count("--max-new-events 3 --max-events-attempted 6") == 2
