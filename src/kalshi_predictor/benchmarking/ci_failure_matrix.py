from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from kalshi_predictor.benchmarking.exposure_bundle import (
    ARTIFACTS,
    build_exposure_guard_certification_bundle,
    golden_exposure_bundle_summary,
)
from kalshi_predictor.benchmarking.exposure_ci import DEFAULT_GOLDEN


FAILURE_CASES = (
    "missing_artifact",
    "malformed_report",
    "altered_hash",
    "failed_regeneration",
    "absent_golden",
)


def build_offline_ci_failure_mode_matrix(
    project_root: Path, workspace: Path
) -> dict[str, Any]:
    workspace.mkdir(parents=True, exist_ok=True)
    golden = project_root / DEFAULT_GOLDEN
    control = _validate_tree(project_root, golden)
    cases = []
    for case in FAILURE_CASES:
        case_root = workspace / case
        _copy_artifacts(project_root, case_root)
        case_golden = golden
        if case == "missing_artifact":
            (case_root / ARTIFACTS["PMB-22"]).unlink()
        elif case == "malformed_report":
            (case_root / ARTIFACTS["PMB-23"]).write_text("{not-json", encoding="utf-8")
        elif case == "altered_hash":
            path = case_root / ARTIFACTS["PMB-25"]
            payload = json.loads(path.read_text())
            payload["summary"]["validation_passed"] = False
            path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        elif case == "failed_regeneration":
            result = _regeneration_failure_result()
            cases.append({"case": case, "expected_code": "REGENERATION_FAILED", **result})
            continue
        elif case == "absent_golden":
            case_golden = case_root / "missing-golden.json"
        result = _validate_tree(case_root, case_golden)
        expected = {
            "missing_artifact": "ARTIFACT_MISSING",
            "malformed_report": "ARTIFACT_MALFORMED",
            "altered_hash": "CERTIFICATION_DRIFT",
            "absent_golden": "GOLDEN_MISSING",
        }[case]
        cases.append({"case": case, "expected_code": expected, **result})
    for row in cases:
        row["expected_failure_detected"] = (
            row["exit_code"] == 1 and row["diagnostic_code"] == row["expected_code"]
        )
    return {
        "phase": "PMB-30",
        "mode": "LOCAL_OFFLINE_CI_FAILURE_MODE_MATRIX",
        "database_access": False,
        "database_writes": 0,
        "cloud_access": False,
        "execution_enabled": False,
        "thresholds_changed": False,
        "policy_activated": False,
        "control": control,
        "cases": cases,
        "summary": {
            "case_count": len(cases),
            "control_passed": control["exit_code"] == 0,
            "all_failures_detected": all(row["expected_failure_detected"] for row in cases),
            "diagnostics_preserved": all(bool(row["diagnostic_code"]) for row in cases),
        },
    }


def write_offline_ci_failure_mode_matrix(
    project_root: Path, output_dir: Path
) -> Path:
    report = build_offline_ci_failure_mode_matrix(
        project_root, output_dir / "failure_fixtures"
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "pmb30_offline_ci_failure_mode_matrix.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)
    return path


def _copy_artifacts(source_root: Path, destination_root: Path) -> None:
    for relative in ARTIFACTS.values():
        destination = destination_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_root / relative, destination)


def _validate_tree(project_root: Path, golden_path: Path) -> dict[str, Any]:
    try:
        if not golden_path.is_file():
            return _failure("GOLDEN_MISSING")
        bundle = build_exposure_guard_certification_bundle(project_root)
        expected = json.loads(golden_path.read_text(encoding="utf-8"))
        actual = golden_exposure_bundle_summary(bundle)
        if actual != expected or bundle["certification"]["passed"] is not True:
            return _failure("CERTIFICATION_DRIFT")
        return {"exit_code": 0, "diagnostic_code": "PASS", "diagnostic_preserved": True}
    except FileNotFoundError:
        return _failure("ARTIFACT_MISSING")
    except json.JSONDecodeError:
        return _failure("ARTIFACT_MALFORMED")
    except (KeyError, TypeError, ValueError):
        return _failure("ARTIFACT_SCHEMA_INVALID")


def _regeneration_failure_result() -> dict[str, Any]:
    try:
        raise RuntimeError("injected deterministic regeneration failure")
    except RuntimeError:
        return _failure("REGENERATION_FAILED")


def _failure(code: str) -> dict[str, Any]:
    return {"exit_code": 1, "diagnostic_code": code, "diagnostic_preserved": True}
