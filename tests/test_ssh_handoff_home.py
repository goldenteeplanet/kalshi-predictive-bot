import os
import subprocess

from kalshi_predictor.phase3bb_r39_cloud_auto_login_admin_bootstrap import (
    SSH_CONFIG_ENV_VAR,
    SSH_CONFIG_TOKEN,
    _render_ssh_config_handoff,
)


def run_script(*, execute=False):
    script = _render_ssh_config_handoff(
        {"cloud_target": {"ssh_target": "fixture@example.invalid", "identity_file": "/tmp/fixture"}}
    )
    env = {
        key: value for key, value in os.environ.items() if key not in {"HOME", SSH_CONFIG_ENV_VAR}
    }
    if execute:
        env[SSH_CONFIG_ENV_VAR] = SSH_CONFIG_TOKEN
    return subprocess.run(
        ["bash", "-c", script], env=env, capture_output=True, text=True, timeout=5
    )


def test_dry_run_needs_no_home_directory():
    result = run_script()
    assert result.returncode == 0
    assert "dry-run" in result.stdout
    assert "add Host kalshi-cloud" in result.stdout


def test_execution_without_home_stops_before_configuration_writes():
    result = run_script(execute=True)
    assert result.returncode != 0
    assert "Set HOME before applying SSH configuration" in result.stderr
