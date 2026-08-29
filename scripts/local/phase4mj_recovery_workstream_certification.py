"""Read-only repository certification for the Phase 4LK-4MI recovery workstream."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path

from scripts.local.phase4ma_recovery_state_machine import INVARIANTS

SCHEMA = "phase4mj.recovery-workstream-certification.v1"
TEST_SCHEMA = "phase4mj.focused-test-evidence.v1"
PHASE_META = {
    "4LK": ("e7c7f26", "runtime-configuration-snapshot", "phase4lk_runtime_config_snapshot"),
    "4LL": ("a7235f6", "runtime-drift-classifier", "phase4ll_runtime_drift_classifier"),
    "4LM": ("dc9edbc", "runtime-observation-ledger", "phase4lm_runtime_observation_ledger"),
    "4LN": ("3ad81db", "alert-state-contract", "phase4ln_alert_state_contract"),
    "4LO": ("6885314", "alert-delivery-envelope", "phase4lo_alert_delivery_envelope"),
    "4LP": ("d1b0e11", "alert-lifecycle", "phase4lp_alert_lifecycle"),
    "4LQ": ("d2f78d2", "alert-retention-contract", "phase4lq_alert_retention_contract"),
    "4LR": ("ae63cb1", "alert-evidence-bundle", "phase4lr_alert_evidence_bundle"),
    "4LS": (
        "afd64d9",
        "offline-verifier-mutation-audit",
        "phase4ls_offline_verifier_mutation_audit",
    ),
    "4LT": (
        "7d10b80",
        "canonicalization-invariance-audit",
        "phase4lt_canonicalization_invariance_audit",
    ),
    "4LU": ("0e00130", "keyless-evidence-contract", "phase4lu_keyless_evidence_contract"),
    "4LV": ("375676a", "offline-crypto-verifier", "phase4lv_offline_crypto_verifier"),
    "4LW": ("78ddd57", "verifier-dependency-readiness", "phase4lw_verifier_dependency_readiness"),
    "4LX": ("a7b5225", "attestation-composition-gate", "phase4lx_attestation_composition_gate"),
    "4LY": ("d2f31f3", "trust-policy-rotation", "phase4ly_trust_policy_rotation"),
    "4LZ": ("ada4e15", "disaster-recovery-simulation", "phase4lz_disaster_recovery_simulation"),
    "4MA": ("6d07b1c", "recovery-state-machine", "phase4ma_recovery_state_machine"),
    "4MB": ("6596351", "checkpoint-repair-planner", "phase4mb_checkpoint_repair_planner"),
    "4MC": ("e1bb022", "repair-plan-mutation-audit", "phase4mc_repair_plan_mutation_audit"),
    "4MD": ("ff37937", "repair-authorization-envelope", "phase4md_repair_authorization_envelope"),
    "4ME": ("236bc30", "nonce-consumption-ledger", "phase4me_nonce_consumption_ledger"),
    "4MF": ("0ace27c", "nonce-ledger-snapshot", "phase4mf_nonce_ledger_snapshot"),
    "4MG": ("059405b", "snapshot-restoration", "phase4mg_snapshot_restoration"),
    "4MH": (
        "0bc92ab",
        "restoration-differential-replay",
        "phase4mh_restoration_differential_replay",
    ),
    "4MI": ("1d7e9cf", "differential-mutation-audit", "phase4mi_differential_mutation_audit"),
}
EXPECTED_INVARIANTS = {
    "execution_enabled": False,
    "demo_execution_enabled": False,
    "autopilot_enabled": False,
    "paper_order_creation_enabled": False,
    "paper_order_kill_switch": True,
    "service_control_allowed": False,
    "runtime_write_allowed": False,
    "material_access_allowed": False,
}
FORBIDDEN = {
    "SUBPROCESS_EXECUTION": re.compile(
        r"\bsubprocess\.(?:run|Popen|call|check_call|check_output)\b"
    ),
    "SHELL_EXECUTION": re.compile(r"\bos\.system\b"),
    "NETWORK_CLIENT": re.compile(r"\b(?:requests|httpx)\.(?:get|post|put|delete|request)\b"),
    "WSL_CONTROL": re.compile(r"\bwsl(?:\.exe)?\b", re.IGNORECASE),
    "SERVICE_CONTROL": re.compile(r"\bsystemctl\b"),
    "ORDER_CREATION": re.compile(r"\b(?:create|place|submit)_(?:paper_)?order\b"),
}
SCHEMA_PATTERN = re.compile(r"phase4[a-z]{2}\.[a-z0-9_.-]+\.v[0-9]+")


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def phase_paths(phase: str) -> list[str]:
    _, doc_slug, script_stem = PHASE_META[phase]
    return [
        f"docs/phase{phase.lower()}-{doc_slug}.md",
        f"scripts/local/{script_stem}.py",
        f"tests/test_{script_stem}.py",
    ]


def make_test_evidence(
    *,
    phases: list[str],
    test_count: int,
    command: str,
    result_summary: str,
) -> dict[str, object]:
    body: dict[str, object] = {
        "schema": TEST_SCHEMA,
        "verdict": "PASS",
        "phases": phases,
        "test_count": test_count,
        "command_sha256": hashlib.sha256(command.encode()).hexdigest(),
        "result_summary": result_summary,
    }
    return {**body, "evidence_sha256": _digest(body)}


def _git(repo: Path, *args: str) -> tuple[int, str]:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        check=False,
        text=True,
    )
    return completed.returncode, completed.stdout.strip()


def _test_evidence_errors(evidence: object) -> list[str]:
    if not isinstance(evidence, dict):
        return ["TEST_EVIDENCE_NOT_OBJECT"]
    fields = {
        "schema",
        "verdict",
        "phases",
        "test_count",
        "command_sha256",
        "result_summary",
        "evidence_sha256",
    }
    errors: list[str] = []
    if set(evidence) != fields:
        errors.append("TEST_EVIDENCE_FIELD_SET_INVALID")
    body = {key: value for key, value in evidence.items() if key != "evidence_sha256"}
    if evidence.get("evidence_sha256") != _digest(body):
        errors.append("TEST_EVIDENCE_HASH_INVALID")
    if evidence.get("schema") != TEST_SCHEMA or evidence.get("verdict") != "PASS":
        errors.append("TEST_EVIDENCE_NOT_PASSING")
    if evidence.get("phases") != list(PHASE_META):
        errors.append("TEST_PHASE_COVERAGE_GAP")
    if type(evidence.get("test_count")) is not int or evidence.get("test_count", 0) < len(
        PHASE_META
    ):
        errors.append("TEST_COUNT_INSUFFICIENT")
    if not isinstance(evidence.get("result_summary"), str) or "passed" not in evidence.get(
        "result_summary", ""
    ):
        errors.append("TEST_RESULT_SUMMARY_INVALID")
    return sorted(set(errors))


def scan_capabilities(source: str) -> list[str]:
    return sorted(name for name, pattern in FORBIDDEN.items() if pattern.search(source))


def certify_workstream(
    repo: str | Path,
    test_evidence: object,
    *,
    expected_head: str | None = None,
) -> dict[str, object]:
    root = Path(repo).resolve()
    source_before = _digest(test_evidence)
    errors = _test_evidence_errors(test_evidence)
    code, head = _git(root, "rev-parse", "HEAD")
    if code != 0:
        errors.append("REPOSITORY_HEAD_UNAVAILABLE")
        head = ""
    all_paths = [path for phase in PHASE_META for path in phase_paths(phase)]
    code, status_output = _git(root, "status", "--porcelain=v1", "--", *all_paths)
    dirty_paths: set[str] = set()
    if code != 0:
        errors.append("WORKTREE_STATUS_UNAVAILABLE")
    else:
        dirty_paths = {line[3:] for line in status_output.splitlines() if len(line) >= 4}
    code, index_output = _git(root, "ls-files", "-s", "--", *all_paths)
    index_hashes: dict[str, str] = {}
    if code != 0:
        errors.append("INDEX_INVENTORY_UNAVAILABLE")
    else:
        for line in index_output.splitlines():
            metadata, relative = line.split("\t", 1)
            index_hashes[relative] = metadata.split()[1]
    expressions = [f"{meta[0]}^{{commit}}" for meta in PHASE_META.values()]
    code, resolved_output = _git(root, "rev-parse", *expressions)
    resolved = resolved_output.splitlines() if code == 0 else []
    if len(resolved) != len(PHASE_META):
        errors.append("PHASE_COMMITS_UNAVAILABLE")
        resolved = [""] * len(PHASE_META)
    certified_head = expected_head or (resolved[-1] if resolved else "")
    if not resolved or certified_head != resolved[-1]:
        errors.append("RANGE_HEAD_BINDING_MISMATCH")
    code, ancestry_output = _git(
        root,
        "rev-list",
        "--reverse",
        "--ancestry-path",
        f"{resolved[0]}^..{head}" if resolved and resolved[0] else head,
    )
    ancestry = ancestry_output.splitlines() if code == 0 else []
    ancestry_positions = {commit: index for index, commit in enumerate(ancestry)}
    code, subject_output = _git(root, "show", "-s", "--format=%H%x00%s", *resolved)
    subjects: dict[str, str] = {}
    if code == 0:
        for line in subject_output.splitlines():
            if "\x00" in line:
                commit, subject = line.split("\x00", 1)
                subjects[commit] = subject
    phase_records: list[dict[str, object]] = []
    prior_position = -1
    for phase_index, phase in enumerate(PHASE_META):
        phase_errors: list[str] = []
        commit = resolved[phase_index]
        if not commit:
            phase_errors.append("COMMIT_MISSING")
        else:
            position = ancestry_positions.get(commit)
            if position is None:
                phase_errors.append("COMMIT_NOT_ANCESTOR")
            elif position <= prior_position:
                phase_errors.append("PHASE_ANCESTRY_DIVERGED")
            else:
                prior_position = position
            if not subjects.get(commit, "").lower().startswith(f"phase{phase.lower()}"):
                phase_errors.append("COMMIT_SUBJECT_MISMATCH")
        records: list[dict[str, object]] = []
        schemas: set[str] = set()
        for relative in phase_paths(phase):
            path = root / relative
            if not path.is_file():
                phase_errors.append(f"MISSING_PATH:{relative}")
                continue
            index_hash = index_hashes.get(relative)
            if index_hash is None:
                phase_errors.append(f"UNTRACKED_PATH:{relative}")
                continue
            content_bytes = path.read_bytes()
            worktree_hash = hashlib.sha1(
                f"blob {len(content_bytes)}\0".encode() + content_bytes,
                usedforsecurity=False,
            ).hexdigest()
            if index_hash != worktree_hash:
                phase_errors.append(f"UNCOMMITTED_PATH:{relative}")
            if relative in dirty_paths:
                phase_errors.append(f"DIRTY_PATH:{relative}")
            content = path.read_text(encoding="utf-8")
            schemas.update(SCHEMA_PATTERN.findall(content))
            if relative.startswith("scripts/"):
                phase_errors.extend(f"CAPABILITY:{item}" for item in scan_capabilities(content))
            records.append(
                {
                    "path": relative,
                    "blob_sha1": index_hash,
                    "content_sha256": hashlib.sha256(content_bytes).hexdigest(),
                }
            )
        if not schemas:
            phase_errors.append("VERSIONED_SCHEMA_MISSING")
        phase_record: dict[str, object] = {
            "phase": phase,
            "verdict": "PASS" if not phase_errors else "REFUSE",
            "errors": sorted(set(phase_errors)),
            "introducing_commit": commit,
            "schemas": sorted(schemas),
            "files": records,
        }
        phase_record["evidence_sha256"] = _digest(phase_record)
        phase_records.append(phase_record)
        errors.extend(f"{phase}:{item}" for item in phase_errors)
    if INVARIANTS != EXPECTED_INVARIANTS:
        errors.append("FAIL_CLOSED_INVARIANTS_WEAKENED")
    residual_risks = [
        "production repair execution is intentionally absent",
        "durable nonce persistence remains an external future capability",
        "cryptographic key custody remains external",
        "WSL and service recovery remain separately supervised",
        "settlement-dependent validation remains deferred until authoritative settlement",
    ]
    errors = sorted(set(errors))
    result: dict[str, object] = {
        "schema": SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": errors,
        "repository": str(root),
        "repository_head_commit": head,
        "certified_range_head_commit": certified_head,
        "phase_range": [next(iter(PHASE_META)), next(reversed(PHASE_META))],
        "phase_count": len(phase_records),
        "passing_phase_count": sum(row["verdict"] == "PASS" for row in phase_records),
        "test_evidence_sha256": (
            test_evidence.get("evidence_sha256") if isinstance(test_evidence, dict) else None
        ),
        "fail_closed_invariants": EXPECTED_INVARIANTS,
        "phases": phase_records,
        "residual_risks": residual_risks,
        "deferred_capabilities": {
            "production_persistence": False,
            "production_compaction": False,
            "repair_execution": False,
            "runtime_write": False,
            "wsl_control": False,
            "service_control": False,
            "network_access": False,
            "order_capability": False,
        },
        "input_unchanged": _digest(test_evidence) == source_before,
        "safety": {"read_only": True, "offline": True},
    }
    result["certification_sha256"] = _digest(result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("test_evidence")
    parser.add_argument("--repo", default=".")
    parser.add_argument("--expected-head")
    args = parser.parse_args()
    with open(args.test_evidence, encoding="utf-8") as stream:
        evidence = json.load(stream)
    result = certify_workstream(args.repo, evidence, expected_head=args.expected_head)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["verdict"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
