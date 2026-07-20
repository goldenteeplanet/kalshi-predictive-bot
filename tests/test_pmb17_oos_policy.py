import json

from kalshi_predictor.benchmarking.oos_policy import (
    OOS_EPISODES,
    build_oos_robust_policy_validation,
    write_oos_robust_policy_validation,
)


def test_pmb17_is_deterministic_and_uses_new_episode_ids(tmp_path):
    first = json.loads(write_oos_robust_policy_validation(tmp_path / "a").read_text())
    second = json.loads(write_oos_robust_policy_validation(tmp_path / "b").read_text())
    assert first == second
    assert first["summary"]["out_of_sample_episodes"] == 9
    assert all(values[0].startswith("oos-") for values in OOS_EPISODES)
    assert first["summary"]["frozen_policy_used"] is True
    assert first["database_writes"] == 0
    assert first["execution_enabled"] is False
    assert first["thresholds_changed"] is False


def test_pmb17_validates_capital_and_drawdown_benefit_without_tuning():
    report = build_oos_robust_policy_validation()
    assert report["comparison"]["capital_benefit_survived"] is True
    assert report["comparison"]["drawdown_benefit_survived"] is True
    assert report["summary"]["capital_and_drawdown_benefit_survived"] is True
    assert report["robust_policy"]["trade_count"] <= report["baseline"]["trade_count"]


def test_pmb17_covers_weather_crypto_sports_with_complete_attribution():
    report = build_oos_robust_policy_validation()
    assert report["summary"]["categories"] == ["crypto", "sports", "weather"]
    assert report["summary"]["all_attribution_complete"] is True
    assert {row["category"] for row in report["baseline"]["rows"]} == {
        "crypto", "weather", "sports"
    }
