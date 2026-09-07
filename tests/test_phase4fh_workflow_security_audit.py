import importlib.util
import json
from pathlib import Path

import pytest

S = Path(__file__).parents[1] / "scripts/local/phase4fh_workflow_security_audit.py"
X = importlib.util.spec_from_file_location("m", S)
assert X and X.loader
M = importlib.util.module_from_spec(X)
X.loader.exec_module(M)


def write(root, text, name="x.yml"):
    (root / name).write_text(text)


SAFE = """name: safe
on: [push]
permissions:
  contents: read
jobs:
  x:
    runs-on: ubuntu-24.04
    steps:
      - uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683
      - run: python -m pytest
"""


def test_safe_workflow_advances(tmp_path):
    write(tmp_path, SAFE)
    r = M.audit(tmp_path, "a" * 64)
    assert r["advancement_allowed"] and not r["findings"]


@pytest.mark.parametrize(
    ("fragment", "code"),
    [
        ("pull_request_target:\n", "PR_TARGET"),
        ("      - run: echo ${{ github.event.pull_request.title }}\n", "SCRIPT_INJECTION"),
        ("      - run: echo ${{ secrets.TOKEN }}\n", "SECRET_CONTEXT"),
        ("  contents: write\n", "WRITE_TOKEN"),
    ],
)
def test_high_risk_fails_closed(tmp_path, fragment, code):
    write(tmp_path, SAFE + fragment)
    r = M.audit(tmp_path, "a" * 64)
    assert not r["advancement_allowed"] and any(code in x["code"] for x in r["findings"])


def test_missing_permissions_fails(tmp_path):
    write(tmp_path, "name: x\non: [push]\njobs: {}\n")
    assert not M.audit(tmp_path, "a" * 64)["advancement_allowed"]


def test_floating_action_is_medium(tmp_path):
    write(tmp_path, SAFE.replace("11bd71901bbe5b1630ceea73d27597364c9af683", "v4"))
    r = M.audit(tmp_path, "a" * 64)
    assert r["advancement_allowed"] and r["findings"][0]["severity"] == "MEDIUM"


def test_real_workflows_have_no_high_severity():
    r = M.audit(S.parents[2] / ".github/workflows", "a" * 64)
    assert r["advancement_allowed"], r["findings"]


@pytest.mark.parametrize(
    "field",
    [
        "workflows_changed",
        "production_database_mutated",
        "services_controlled",
        "execution_authorized",
    ],
)
def test_read_only(field, tmp_path):
    write(tmp_path, SAFE)
    assert M.audit(tmp_path, "a" * 64)[field] is False


def test_atomic(tmp_path):
    d = tmp_path / "w"
    d.mkdir()
    write(d, SAFE)
    p = tmp_path / "x"
    r = M.audit(d, "a" * 64)
    M.publish(p, r)
    assert json.loads(p.read_text()) == r
