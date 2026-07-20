from pathlib import Path

from kalshi_predictor.ui.deployment_harness_certification import certify_fail_closed_harness


def test_harness_uses_valid_unit_name_and_exits_after_one_rollback():
    report = certify_fail_closed_harness(
        Path("reports/phase_ui_obs5f_retry/deploy_ui_obs5f_retry.sh"),
        Path("deploy/systemd/kalshi-ui-status-collector.service.ui-obs5fa.preview"),
    )
    assert report["status"] == "PASSED"
    assert report["injected_failure"] == {
        "exit_code": 23, "rollback_count": 1, "continued_after_failure": False,
    }
