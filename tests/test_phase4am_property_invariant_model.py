from __future__ import annotations

import importlib.util
import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest


def _module():
    path = Path(__file__).parents[1] / "scripts/local/phase4am_property_invariant_model.py"
    spec = importlib.util.spec_from_file_location("phase4am_tested", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


NOW = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)


def test_seeded_model_covers_every_scenario_and_has_no_counterexamples():
    module = _module()
    report, manifest = module.build(seeds=[17, 5], case_count=len(module.SCENARIOS), now=NOW)
    assert report["total_cases"] == 2 * len(module.SCENARIOS)
    assert all(row["case_count"] == 2 for row in report["scenario_coverage"])
    assert report["all_invariants_satisfied"] is True
    assert report["counterexample_count"] == 0
    assert manifest["counterexamples"] == []
    assert manifest["advancement_allowed"] is True


@pytest.mark.parametrize(
    ("scenario", "terminal", "reason"),
    [
        ("SETTLED_ALREADY", "REFUSED", "SETTLEMENT_ALREADY_TIMESTAMPED"),
        ("RESULT_INVALID", "REFUSED", "RESULT_ENCODING_INVALID"),
        ("RESULT_TYPE_INVALID", "REFUSED", "RESULT_ENCODING_INVALID"),
        ("MISSING_COLUMN", "REFUSED", "MISSING_COLUMN"),
        ("MALFORMED_ROW", "REFUSED", "MALFORMED_ROW"),
        ("DUPLICATE_IDENTITY", "REFUSED", "DUPLICATE_IDENTITY"),
        ("LINEAGE_MISMATCH", "REFUSED", "LINEAGE_MISMATCH"),
        ("TIMESTAMP_NAIVE", "REFUSED", "TIMESTAMP_INVALID_OR_NAIVE"),
        ("TIMESTAMP_MALFORMED", "REFUSED", "TIMESTAMP_INVALID_OR_NAIVE"),
        ("EXPIRATION_BEFORE", "REFUSED", "ARTIFACT_EXPIRED"),
        ("EXPIRATION_EQUAL", "REFUSED", "ARTIFACT_EXPIRED"),
        ("CAS_ZERO_ROWS", "ROLLED_BACK", "ROW_COUNT_MISMATCH"),
        ("CAS_MULTI_ROWS", "ROLLED_BACK", "ROW_COUNT_MISMATCH"),
        ("POSTCONDITION_FAILURE", "ROLLED_BACK", "POSTCONDITION_MISMATCH"),
    ],
)
def test_invalid_properties_refuse_or_roll_back_without_state_change(
    scenario: str, terminal: str, reason: str
):
    module = _module()
    index = module.SCENARIOS.index(scenario)
    case = module.generate_case(11, index, NOW)
    result = module.evaluate_case(case, NOW)
    assert result["transitions"][-1] == terminal
    assert reason in result["reason_codes"]
    assert result["after"] == result["before"]
    assert module.invariant_failures(case, result) == []


@pytest.mark.parametrize("scenario", ["VALID_SINGLE", "VALID_RESULT_NO", "VALID_TWO_ROWS"])
def test_valid_properties_commit_only_settled_at(scenario: str):
    module = _module()
    case = module.generate_case(23, module.SCENARIOS.index(scenario), NOW)
    result = module.evaluate_case(case, NOW)
    assert result["committed"] is True
    assert result["affected_rows"] == len(result["before"])
    for before, after in zip(result["before"], result["after"], strict=True):
        assert {key for key in before if before[key] != after[key]} == {"settled_at"}
    assert module.invariant_failures(case, result) == []


def test_expiration_after_boundary_is_accepted():
    module = _module()
    case = module.generate_case(3, module.SCENARIOS.index("EXPIRATION_AFTER"), NOW)
    assert module.evaluate_case(case, NOW)["committed"] is True


def test_generated_cases_and_artifacts_are_reproducible():
    module = _module()
    assert module.generate_case(99, 7, NOW) == module.generate_case(99, 7, NOW)
    first = module.build(seeds=[99, 1], case_count=72, now=NOW)
    second = module.build(seeds=[1, 99], case_count=72, now=NOW)
    assert first == second


def test_different_seeds_change_case_root():
    module = _module()
    first, _ = module.build(seeds=[1], case_count=36, now=NOW)
    second, _ = module.build(seeds=[2], case_count=36, now=NOW)
    assert first["case_hashes_root"] != second["case_hashes_root"]


def test_counterexample_shrinking_is_deterministic_and_minimizes_rows():
    module = _module()
    case = module.generate_case(7, module.SCENARIOS.index("VALID_TWO_ROWS"), NOW)

    def synthetic_failure(value):
        return bool(value.get("rows"))

    first = module.shrink_counterexample(case, synthetic_failure)
    second = module.shrink_counterexample(case, synthetic_failure)
    assert first == second
    assert len(first["rows"]) == 1


def test_invariant_checker_detects_illegal_transition_and_unrelated_change():
    module = _module()
    case = module.generate_case(1, 0, NOW)
    result = module.evaluate_case(case, NOW)
    result["transitions"] = ["START", "COMMITTED"]
    result["after"][0]["payload"] = {"unrelated": "changed"}
    failures = module.invariant_failures(case, result)
    assert "STATE_TRANSITIONS_LEGAL" in failures
    assert "ONLY_CANONICAL_TIMESTAMP_CHANGES" in failures


@pytest.mark.parametrize(
    ("seeds", "count", "error"),
    [([], 1, "SEEDS_INVALID"), ([1, 1], 1, "SEEDS_INVALID"), ([1], 0, "COUNT_OUT_OF_RANGE")],
)
def test_invalid_run_configuration_fails_closed(seeds, count, error):
    with pytest.raises(ValueError, match=error):
        _module().build(seeds=seeds, case_count=count, now=NOW)


def test_naive_evaluation_time_fails_closed():
    with pytest.raises(ValueError, match="TIMEZONE_MISSING"):
        _module().build(seeds=[1], case_count=1, now=datetime(2026, 8, 25))


def test_atomic_pair_publication_and_restore(tmp_path: Path, monkeypatch):
    module = _module()
    report, manifest = module.build(seeds=[1], case_count=36, now=NOW)
    first, second = tmp_path / "coverage.json", tmp_path / "counterexamples.json"
    module.publish_pair(first, second, report, manifest)
    with pytest.raises(FileExistsError):
        module.publish_pair(first, second, report, manifest)
    old = first.read_bytes(), second.read_bytes()
    original, calls = module.os.replace, 0

    def fail_fourth(source, destination):
        nonlocal calls
        calls += 1
        if calls == 4:
            raise OSError("injected pair failure")
        return original(source, destination)

    monkeypatch.setattr(module.os, "replace", fail_fourth)
    with pytest.raises(OSError, match="injected pair failure"):
        module.publish_pair(first, second, report, manifest, replace=True)
    assert (first.read_bytes(), second.read_bytes()) == old
    assert not list(tmp_path.glob(".*.tmp")) and not list(tmp_path.glob(".*.bak"))


def test_outputs_are_hash_valid_non_authorizing_and_contain_no_sql(tmp_path: Path):
    module = _module()
    report, manifest = module.build(seeds=[1], case_count=36, now=NOW)
    assert report["artifact_hash"] == module._hash(report)
    assert manifest["manifest_hash"] == module._hash(manifest, "manifest_hash")
    rendered = json.dumps([report, manifest], sort_keys=True).lower()
    assert "update settlements" not in rendered
    assert 'execution_authorized": true' not in rendered
    assert 'production_database_mutated": true' not in rendered
    assert "credential" not in rendered


def test_cli_publishes_byte_identical_canonical_outputs(tmp_path: Path, monkeypatch, capsys):
    module = _module()
    first, second = tmp_path / "coverage.json", tmp_path / "counterexamples.json"
    monkeypatch.setattr(
        "sys.argv",
        [
            "phase4am",
            "--seed",
            "41",
            "--cases-per-seed",
            "36",
            "--evaluation-time",
            NOW.isoformat(),
            "--coverage-output",
            str(first),
            "--counterexample-output",
            str(second),
        ],
    )
    module.main()
    capsys.readouterr()
    assert first.read_bytes().endswith(b"\n") and second.read_bytes().endswith(b"\n")
    assert os.path.getsize(first) > 0 and os.path.getsize(second) > 0
