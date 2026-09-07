import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
WORKFLOW = ROOT / ".github/workflows/phase4-required-safety.yml"
SCRIPT = ROOT / "scripts/local/phase4fg_repository_secret_scan.py"
X = importlib.util.spec_from_file_location("m", SCRIPT)
assert X and X.loader
M = importlib.util.module_from_spec(X)
X.loader.exec_module(M)


def test_workflow_has_all_required_checks():
    text = WORKFLOW.read_text()
    for token in (
        "ruff:",
        "focused-tests:",
        "cumulative-safety-tests:",
        "mutation-and-secret-scan:",
        "artifact-validation:",
    ):
        assert token in text


def test_workflow_is_non_deploying_and_read_only():
    text = WORKFLOW.read_text()
    assert "permissions:\n  contents: read" in text
    for token in (
        "deploy",
        "environment:",
        "workflow_dispatch",
        "schedule:",
        "id-token: write",
        "contents: write",
        "pull-requests: write",
        "secrets.",
        "/home/james",
    ):
        assert token not in text


def test_actions_are_full_sha_pinned():
    import re

    refs = re.findall(r"uses:\s+[^@\s]+@([^\s]+)", WORKFLOW.read_text())
    assert refs and all(re.fullmatch(r"[0-9a-f]{40}", x) for x in refs)


def test_exact_python_and_complete_suite():
    text = WORKFLOW.read_text()
    assert 'python-version: "3.11.9"' in text and "tests/test_phase4*.py" in text


def test_scanner_clean_fixture(tmp_path):
    (tmp_path / "a.py").write_text('token = "short-placeholder"')
    assert M.scan(tmp_path) == []


@pytest.mark.parametrize(
    "secret",
    [
        'api_key = "abcdefghijklmnopqrstuvwxyz123456"',
        'token: "ABCDEFGHIJKLMNOPQRSTUVWXYZ123456"',
        "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890",
    ],
)
def test_scanner_detects_high_confidence_secret(tmp_path, secret):
    (tmp_path / "a.py").write_text(secret)
    assert M.scan(tmp_path) == ["a.py:1"]


def test_allowlist_is_explicit(tmp_path):
    (tmp_path / "a.py").write_text(
        'token = "abcdefghijklmnopqrstuvwxyz123456"  # pragma: allowlist secret'
    )
    assert M.scan(tmp_path) == []


def test_scanner_has_no_network_or_runtime_surface():
    s = SCRIPT.read_text()
    assert "requests" not in s and "subprocess" not in s and "/home/james" not in s
