import json

from kalshi_predictor.benchmarking.oos_exposure_guard import (
    FROZEN_POSITION_SCALE,
    OOS_EXPOSURE_EPISODES,
    build_oos_exposure_guard_validation,
    write_oos_exposure_guard_validation,
)


def test_pmb25_is_deterministic_local_and_frozen(tmp_path):
    first = json.loads(write_oos_exposure_guard_validation(tmp_path / "a").read_text())
    second = json.loads(write_oos_exposure_guard_validation(tmp_path / "b").read_text())
    assert first == second
    assert first["frozen_position_scale"] == str(FROZEN_POSITION_SCALE)
    assert first["database_writes"] == 0
    assert first["cloud_access"] is False
    assert first["execution_enabled"] is False
    assert first["thresholds_changed"] is False
    assert first["policy_tuned_on_oos"] is False


def test_pmb25_uses_new_multicategory_episodes_with_complete_attribution():
    report = build_oos_exposure_guard_validation()
    assert report["summary"]["out_of_sample_episodes"] == len(OOS_EXPOSURE_EPISODES)
    assert report["summary"]["categories"] == ["crypto", "sports", "weather"]
    assert report["summary"]["new_episode_ids"] is True
    assert report["summary"]["all_attribution_complete"] is True


def test_pmb25_preserves_selection_and_roc_while_improving_drawdown():
    report = build_oos_exposure_guard_validation()
    assert report["summary"]["identical_trade_selection"] is True
    assert report["comparison"]["return_on_capital_preserved"] is True
    assert report["comparison"]["drawdown_improvement_survived"] is True
    assert report["summary"]["validation_passed"] is True
