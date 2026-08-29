from __future__ import annotations

import copy
from pathlib import Path

from scripts.local.phase4mj_recovery_workstream_certification import (
    EXPECTED_INVARIANTS,
    PHASE_META,
    _digest,
    certify_workstream,
    make_test_evidence,
    phase_paths,
    scan_capabilities,
)

ROOT = Path(__file__).resolve().parents[1]


def _evidence():
    return make_test_evidence(
        phases=list(PHASE_META),
        test_count=131,
        command="pytest phase4lk through phase4mi",
        result_summary="131 passed",
    )


def test_current_committed_workstream_certifies_deterministically() -> None:
    first = certify_workstream(ROOT, _evidence())
    second = certify_workstream(ROOT, _evidence())
    assert first == second
    assert first["verdict"] == "PASS"
    assert first["phase_count"] == 25
    assert first["passing_phase_count"] == 25
    assert all(row["verdict"] == "PASS" for row in first["phases"])


def test_inventory_has_exact_three_owned_paths_per_phase() -> None:
    paths = [path for phase in PHASE_META for path in phase_paths(phase)]
    assert len(paths) == 75
    assert len(paths) == len(set(paths))
    assert all((ROOT / path).is_file() for path in paths)


def test_missing_test_phase_and_stale_evidence_hash_fail_closed() -> None:
    missing = _evidence()
    missing["phases"] = missing["phases"][:-1]
    missing["evidence_sha256"] = _digest(
        {key: value for key, value in missing.items() if key != "evidence_sha256"}
    )
    result = certify_workstream(ROOT, missing)
    assert result["verdict"] == "REFUSE"
    assert "TEST_PHASE_COVERAGE_GAP" in result["errors"]
    stale = _evidence()
    stale["test_count"] += 1
    assert "TEST_EVIDENCE_HASH_INVALID" in certify_workstream(ROOT, stale)["errors"]


def test_wrong_head_binding_fails_closed() -> None:
    result = certify_workstream(ROOT, _evidence(), expected_head="0" * 40)
    assert result["verdict"] == "REFUSE"
    assert "RANGE_HEAD_BINDING_MISMATCH" in result["errors"]


def test_capability_scanner_detects_each_forbidden_class() -> None:
    source = "\n".join(
        [
            "subprocess.run([])",
            "os.system('x')",
            "requests.post('x')",
            "wsl.exe --shutdown",
            "systemctl restart x",
            "create_order()",
        ]
    )
    assert set(scan_capabilities(source)) == {
        "SUBPROCESS_EXECUTION",
        "SHELL_EXECUTION",
        "NETWORK_CLIENT",
        "WSL_CONTROL",
        "SERVICE_CONTROL",
        "ORDER_CREATION",
    }


def test_fail_closed_invariant_snapshot_is_exact() -> None:
    assert EXPECTED_INVARIANTS == {
        "execution_enabled": False,
        "demo_execution_enabled": False,
        "autopilot_enabled": False,
        "paper_order_creation_enabled": False,
        "paper_order_kill_switch": True,
        "service_control_allowed": False,
        "runtime_write_allowed": False,
        "material_access_allowed": False,
    }


def test_certifier_does_not_mutate_evidence_or_expose_capabilities() -> None:
    evidence = _evidence()
    before = copy.deepcopy(evidence)
    result = certify_workstream(ROOT, evidence)
    assert evidence == before
    assert result["input_unchanged"] is True
    assert result["safety"] == {"read_only": True, "offline": True}
    assert all(value is False for value in result["deferred_capabilities"].values())
