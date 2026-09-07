from pathlib import Path


def test_cloud_deployment_smoke_is_read_only_and_checks_runtime_ownership() -> None:
    root = Path(__file__).parents[1]
    script = (root / "scripts/cloud/verify-paper-deployment.sh").read_text(encoding="utf-8")

    assert "exact deployed 40-character Git commit SHA" in script
    assert "curl --fail" in script
    assert "db-writer-monitor --json" in script
    assert "kalshi-gh1-websocket-watch.service" in script
    assert "kalshi-gh2-decision-refresh.timer" in script
    assert "kalshi-nyc-weather-runtime-refresh.timer" in script
    assert "kalshi-r5-bounded.timer" in script
    assert "EXECUTION_ENABLED=false" in script
    assert "AUTOPILOT_ENABLED=false" in script
    assert "EXECUTION_KILL_SWITCH=true" in script
    assert 'git -c safe.directory="$APP_PATH" -C "$APP_PATH" rev-parse HEAD' in script
    assert "git config --global" not in script
    for mutation in (
        "systemctl start",
        "systemctl stop",
        "systemctl restart",
        "systemctl enable",
        "systemctl disable",
    ):
        assert mutation not in script


def test_gh4_rehearsal_is_local_and_simulated() -> None:
    root = Path(__file__).parents[1]
    script = (root / "scripts/local/gh4-paper-lifecycle-rehearsal.sh").read_text(
        encoding="utf-8"
    )

    assert "tests/test_phase_gh4.py" in script
    assert "tests/test_paper_strategy.py" in script
    assert "tests/test_paper_ledger_pnl.py" in script
    assert "tests/test_phase_3m_dynamic_position_sizing.py" in script
    assert "tests/test_phase_3n_advanced_risk.py" in script
    assert "tests/test_phase_3aa_r2_exact_settlement_harvest.py" in script
    assert "tests/test_phase_3ai_link_reconciliation.py" in script
    assert "ssh " not in script
    assert "kalshi-cloud" not in script
