from pathlib import Path

SCRIPT = (
    Path(__file__).parents[1] / "scripts" / "local" / "kalshi-fixed-rate-refresh.sh"
).read_text(encoding="utf-8")
WEATHER_PREP = (
    Path(__file__).parents[1] / "scripts" / "supported_weather_prepare.py"
).read_text(encoding="utf-8")
WEATHER_SNAPSHOT = (
    Path(__file__).parents[1] / "scripts" / "supported_weather_snapshot_forecast.py"
).read_text(encoding="utf-8")


def test_scheduler_enforces_completion_based_cooldown() -> None:
    assert "KALSHI_MIN_POST_CYCLE_COOLDOWN_SECONDS:-60" in SCRIPT
    assert 'cooldown_start_epoch="$((now_epoch + MIN_POST_CYCLE_COOLDOWN_SECONDS))"' in SCRIPT
    assert 'next_start_epoch="$cooldown_start_epoch"' in SCRIPT


def test_weather_gate_uses_small_bounded_batches() -> None:
    assert "run_health_stage weather_gate_diagnostics 75 timeout 75s" in SCRIPT
    assert "run_health_stage weather_gate_post_preflight 75 timeout 75s" in SCRIPT
    assert SCRIPT.count("--deadline-seconds 50 --batch-size 2") == 2
    assert "--opportunity-limit 20 --weather-decision-limit 8" in SCRIPT
    assert SCRIPT.count("--limit 8 --current-window-lookback-hours 3") == 2


def test_targeted_capture_is_rate_limited_and_sharded() -> None:
    assert 'targeted_capture_stage="targeted_crypto_capture_major"' in SCRIPT
    assert 'targeted_capture_stage="targeted_crypto_capture_alt"' in SCRIPT
    assert 'run_health_stage "$targeted_capture_stage" 180 timeout 180s' in SCRIPT
    assert '--coherence-ms 2500 --max-workers 2' in SCRIPT
    assert '--max-new-events 1 --max-events-attempted 2' in SCRIPT
    assert '--targeted-capture-max-buckets 25' in SCRIPT


def test_crypto_router_has_an_independent_scheduler_budget() -> None:
    assert "--defer-phase3bc-router" in SCRIPT
    assert "run_health_stage active_crypto_router 210 timeout 210s" in SCRIPT
    assert "phase3bc-crypto-clean-opportunity-router" in SCRIPT
    assert "active_crypto_ranking_finalize 240 timeout 240s" in SCRIPT


def test_supported_weather_prepare_bounds_feature_rebuilds() -> None:
    assert 'parser.add_argument("--feature-limit", type=int, default=200)' in WEATHER_PREP
    assert '"--limit",' in WEATHER_PREP
    assert 'str(args.feature_limit)' in WEATHER_PREP


def test_supported_weather_snapshot_fetches_outside_db_session_in_bounded_pool() -> None:
    assert 'parser.add_argument("--fetch-workers", type=int, default=4)' in WEATHER_SNAPSHOT
    assert "ThreadPoolExecutor(max_workers=min(workers" in WEATHER_SNAPSHOT
    assert WEATHER_SNAPSHOT.find("fetched = _fetch_tickers") < WEATHER_SNAPSHOT.find(
        "with session_factory() as session"
    )
    assert "--limit 8 --fetch-workers 4" in SCRIPT
    assert 'ticker.split("-", 1)[0]' in WEATHER_SNAPSHOT
