from pathlib import Path


def test_guarded_cloud_deploy_handoff_contract() -> None:
    root = Path(__file__).parents[1]
    script = (root / "scripts/cloud/deploy-verified-commit.sh").read_text(encoding="utf-8")

    assert "[[ $EUID -eq 0 ]]" in script
    assert "exact 40-character Git commit SHA" in script
    assert 'systemctl stop "${TIMERS[@]}"' in script
    assert "kalshi-gh2-decision-refresh.service" in script
    assert "db-writer-monitor --json" in script
    assert '"safe_to_start_write": true' in script
    assert '"writer_count": 0' in script
    assert "status --porcelain" in script
    assert 'checkout --detach "$TARGET_SHA"' in script
    assert "rollback_on_error" in script
    assert "trap cleanup EXIT" in script
    assert "if (( rc != 0 ))" in script
    assert "UI_WAIT_SECONDS" in script
    assert "wait_for_ui" in script
    assert "sleep 2" in script
    assert "kalshi-gh1-websocket-watch.service" in script
    assert "curl --fail" in script
    assert "EXECUTION_ENABLED=true" not in script
    assert "AUTOPILOT_ENABLED=true" not in script
    assert "paper-order" not in script.lower().replace("paper-order creation", "")
