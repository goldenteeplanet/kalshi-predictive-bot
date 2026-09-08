from __future__ import annotations

import importlib.util
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest


def _module():
    path = Path(__file__).parents[1] / "scripts/local/phase4ar_capability_separation_audit.py"
    spec = importlib.util.spec_from_file_location("phase4ar_tested", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


NOW = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)


def _write(path: Path, source: str) -> Path:
    path.write_text(source, encoding="utf-8")
    return path


def test_separated_roles_pass_and_graph_local_dependency(tmp_path: Path):
    module = _module()
    review = _write(tmp_path / "review.py", "import json\ndef check(): return True\n")
    readiness = _write(
        tmp_path / "readiness.py",
        "import review\nimport sqlite3\n"
        "def read(p): return sqlite3.connect(f'file:{p}?mode=ro', uri=True)\n",
    )
    simulation = _write(
        tmp_path / "simulation.py",
        "import sqlite3\ndef run(p):\n c=sqlite3.connect(p)\n"
        " c.execute('UPDATE settlements SET settled_at=? WHERE ticker=?',(1,2))\n",
    )
    authorization = _write(tmp_path / "authorization.py", "def validate(x): return bool(x)\n")
    graph, verdict = module.build(
        [
            ("REVIEW", review),
            ("READINESS", readiness),
            ("SIMULATION", simulation),
            ("AUTHORIZATION_VALIDATION", authorization),
        ],
        now=NOW,
    )
    assert verdict["separation_verdict"] == "CAPABILITIES_SEPARATED"
    assert verdict["advancement_allowed"] is True
    assert any(edge["dependency"] == "review" for edge in graph["edges"])


@pytest.mark.parametrize(
    ("source", "capability"),
    [
        ("import subprocess\nsubprocess.run(['x'])\n", "SHELL_OR_SUBPROCESS"),
        ("import os\nos.system('x')\n", "SHELL_OR_SUBPROCESS"),
        ("import os\nos.environ['MODE']='live'\n", "ENVIRONMENT_SETTING_MUTATION"),
        ("import fcntl\nfcntl.flock(1,2)\n", "PRODUCTION_WRITER_LOCK"),
        ("eval('1')\n", "SERIALIZED_CALLBACK"),
        ("COMMAND='systemctl restart x'\n", "SERVICE_CONTROL"),
        ("def f(): return 'create_order'\n", "EXCHANGE_CLIENT"),
    ],
)
def test_forbidden_capabilities_fail_separation(tmp_path: Path, source: str, capability: str):
    module = _module()
    path = _write(tmp_path / "source.py", source)
    _, verdict = module.build([("SIMULATION", path)], now=NOW)
    assert verdict["separation_verdict"] == "SEPARATION_FAILED"
    assert capability in {row["capability"] for row in verdict["violations"]}


@pytest.mark.parametrize("role", ["REVIEW", "READINESS", "AUTHORIZATION_VALIDATION"])
def test_writable_database_and_mutation_sql_forbidden_outside_simulation(tmp_path: Path, role: str):
    module = _module()
    path = _write(
        tmp_path / f"{role}.py",
        "import sqlite3\nc=sqlite3.connect('x.db')\n"
        "c.execute('UPDATE settlements SET settled_at=1')\n",
    )
    _, verdict = module.build([(role, path)], now=NOW)
    capabilities = {row["capability"] for row in verdict["violations"]}
    assert {"DATABASE_OPEN_WRITABLE", "SETTLEMENT_MUTATION_SQL"} <= capabilities


def test_network_dependency_is_reported_but_not_hidden(tmp_path: Path):
    module = _module()
    path = _write(tmp_path / "review.py", "import httpx\n")
    graph, verdict = module.build([("REVIEW", path)], now=NOW)
    names = {row["capability"] for row in graph["nodes"][0]["capabilities"]}
    assert "NETWORK_CAPABLE_DEPENDENCY" in names
    assert verdict["advancement_allowed"] is True


def test_cli_options_are_inventory_only(tmp_path: Path):
    module = _module()
    path = _write(
        tmp_path / "review.py",
        "import argparse\np=argparse.ArgumentParser()\np.add_argument('--input')\n",
    )
    graph, _ = module.build([("REVIEW", path)], now=NOW)
    assert graph["nodes"][0]["cli_options"] == ["--input"]


def test_duplicate_empty_invalid_role_and_syntax_fail_closed(tmp_path: Path):
    module = _module()
    valid = _write(tmp_path / "valid.py", "x=1\n")
    with pytest.raises(ValueError, match="EMPTY_OR_DUPLICATED"):
        module.build([], now=NOW)
    with pytest.raises(ValueError, match="EMPTY_OR_DUPLICATED"):
        module.build([("REVIEW", valid), ("REVIEW", valid)], now=NOW)
    with pytest.raises(ValueError, match="ROLE_INVALID"):
        module.inspect_source("UNKNOWN", valid)
    broken = _write(tmp_path / "broken.py", "def nope(:\n")
    with pytest.raises(ValueError, match="UNREADABLE_OR_INVALID"):
        module.inspect_source("REVIEW", broken)


def test_real_phase_sources_pass_capability_separation():
    module = _module()
    root = Path(__file__).parents[1] / "scripts/local"
    sources = [
        ("REVIEW", root / "phase4aj_independent_proposal_review.py"),
        ("READINESS", root / "phase4ak_readiness_envelope.py"),
        ("SIMULATION", root / "phase4al_offline_protocol_simulation.py"),
        ("SIMULATION", root / "phase4am_property_invariant_model.py"),
        ("SIMULATION", root / "phase4an_crash_recovery_simulation.py"),
        ("SIMULATION", root / "phase4ao_concurrency_proof.py"),
        ("SIMULATION", root / "phase4ap_backup_restore_verification.py"),
        ("AUTHORIZATION_VALIDATION", root / "phase4aq_authorization_validator.py"),
    ]
    graph, verdict = module.build(sources, now=NOW)
    assert len(graph["nodes"]) == len(sources)
    assert verdict["separation_verdict"] == "CAPABILITIES_SEPARATED"


def test_artifacts_are_deterministic_hash_valid_and_non_authorizing(tmp_path: Path):
    module = _module()
    source = _write(tmp_path / "source.py", "import json\n")
    first = module.build([("REVIEW", source)], now=NOW)
    second = module.build([("REVIEW", source)], now=NOW)
    assert first == second
    graph, verdict = first
    assert graph["artifact_hash"] == module._hash(graph)
    assert verdict["manifest_hash"] == module._hash(verdict, "manifest_hash")
    rendered = json.dumps(first, sort_keys=True).lower()
    assert 'execution_authorized": true' not in rendered
    assert 'production_database_mutated": true' not in rendered
