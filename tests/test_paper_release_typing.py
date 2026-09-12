"""Synthetic command artifacts test the release policy, not actual type cleanliness."""

import copy
import json
from dataclasses import replace

import pytest
from test_overnight_activation import ref

from kalshi_predictor.overnight_paper import activation, release_typing


@pytest.fixture
def evidence(tmp_path, monkeypatch):
    for target in release_typing.TYPING_TARGETS:
        path = tmp_path / target
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# synthetic source for policy binding\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[tool.mypy]\ncheck_untyped_defs=true\n")
    sha = "a" * 40
    tests = ref("pytest", b"100 passed, 1 skipped")
    lint = ref("ruff", b"All checks passed!")
    checks = ref(
        "checks",
        json.dumps(
            {
                "check_runs": [
                    {
                        "name": "test",
                        "head_sha": sha,
                        "conclusion": "success",
                    }
                ]
            }
        ).encode(),
    )
    typing = ref(
        "mypy",
        (f"Success: no issues found in {len(release_typing.TYPING_TARGETS)} source files").encode(),
    )
    report = {
        "sha": sha,
        "pytest_command": "pytest",
        "lint_command": "ruff check .",
        "required_checks": ["test"],
        "mypy_command": release_typing.typing_command(),
        "typing_scope": "PAPER_RELEASE_PATH_ONLY",
        "mypy_policy": release_typing.typing_manifest(tmp_path),
        "artifacts": {
            "pytest": tests.sha256,
            "ruff": lint.sha256,
            "hosted_checks": checks.sha256,
            "mypy": typing.sha256,
        },
    }
    monkeypatch.setattr(activation, "_verify_import_origins", lambda repo: None)
    monkeypatch.setattr(
        activation.subprocess,
        "check_output",
        lambda args, **kwargs: sha if "rev-parse" in args else "",
    )
    release = activation.ExactReleaseEvidence(
        tmp_path,
        sha,
        ref("report", json.dumps(report).encode()),
        tests,
        lint,
        checks,
        typing,
    )
    return release, report


def changed(release, report):
    return replace(release, report=ref("report", json.dumps(report).encode()))


def test_exact_code_owned_scope_passes_actual_release_parser(evidence):
    release, _ = evidence
    release.verify()


@pytest.mark.parametrize("mutation", ["drop", "extra", "flags", "hash", "label", "version"])
def test_altered_scoped_evidence_cannot_release(evidence, mutation):
    release, original = evidence
    report = copy.deepcopy(original)
    if mutation == "drop":
        report["mypy_policy"]["targets"].pop()
    elif mutation == "extra":
        report["mypy_policy"]["targets"].append("src/unreviewed.py")
    elif mutation == "flags":
        report["mypy_command"] += " --ignore-errors"
    elif mutation == "hash":
        report["mypy_policy"]["source_sha256"][release_typing.TYPING_TARGETS[0]] = "0" * 64
    elif mutation == "label":
        report["typing_scope"] = "GLOBAL_CLEAN"
    else:
        report["mypy_policy"]["version"] = "unreviewed-policy"
    with pytest.raises(ValueError, match="MYPY"):
        changed(release, report).verify()


@pytest.mark.parametrize("target", [release_typing.TYPING_TARGETS[0], "pyproject.toml"])
def test_current_source_or_checker_config_change_requires_new_evidence(evidence, target):
    release, _ = evidence
    (release.repository / target).write_text("# changed after verification\n", encoding="utf-8")
    with pytest.raises(ValueError, match="MYPY_REVIEWED_SOURCE_SCOPE"):
        release.verify()


@pytest.mark.parametrize(
    "output",
    [
        b"Success: no issues found in 1 source file",
        b"Success: no issues found in 88 source files\nx.py:1: error: broken",
    ],
)
def test_wrong_count_or_hidden_errors_reject(evidence, output):
    release, report = evidence
    typing = ref("mypy", output)
    report["artifacts"]["mypy"] = typing.sha256
    with pytest.raises(ValueError, match="MYPY"):
        replace(changed(release, report), mypy_output=typing).verify()


def test_true_full_global_command_remains_supported(evidence):
    release, report = evidence
    report["mypy_command"] = "mypy src"
    report.pop("typing_scope")
    report.pop("mypy_policy")
    changed(release, report).verify()


@pytest.mark.parametrize("target", [
    "src/kalshi_predictor/crypto/account_fee_evidence.py",
    "src/kalshi_predictor/crypto/calibration_cost_evidence.py",
    "src/kalshi_predictor/crypto/cost_evidence.py",
    "src/kalshi_predictor/crypto/cost_record.py",
    "src/kalshi_predictor/crypto/full_cost_evidence.py",
    "src/kalshi_predictor/overnight_paper/cf_candidate_assembly.py",
    "src/kalshi_predictor/overnight_paper/cf_source.py",
])
def test_cf_and_cost_source_change_invalidates_release_typing(evidence, target):
    release, _ = evidence
    assert target in release_typing.TYPING_TARGETS
    release.verify()
    (release.repository / target).write_text("# changed after typing verification\n")
    with pytest.raises(ValueError, match="MYPY_REVIEWED_SOURCE_SCOPE"):
        release.verify()


@pytest.mark.parametrize("filename", ["miami_driver.py", "miami_development.py"])
def test_miami_source_change_invalidates_typing_manifest(evidence, filename):
    release, _ = evidence
    required = {
        "src/kalshi_predictor/overnight_paper/miami_driver.py",
        "src/kalshi_predictor/overnight_paper/miami_development.py",
        "src/kalshi_predictor/overnight_paper/miami_storage.py",
        "src/kalshi_predictor/overnight_paper/miami_preparation.py",
        "src/kalshi_predictor/overnight_paper/miami_preparation_runner.py",
        "src/kalshi_predictor/overnight_paper/miami_binding.py",
        "src/kalshi_predictor/overnight_paper/miami_provenance.py",
        "src/kalshi_predictor/overnight_paper/miami_source.py",
        "src/kalshi_predictor/overnight_paper/miami_source_gate.py",
        "src/kalshi_predictor/weather/miami_index.py",
        "src/kalshi_predictor/weather/miami_forecast.py",
        "src/kalshi_predictor/weather/miami_half_hour_forecast.py",
        "src/kalshi_predictor/crypto/research_provenance.py",
    }
    assert required <= set(release_typing.TYPING_TARGETS)
    assert len(set(release_typing.TYPING_TARGETS)) == len(release_typing.TYPING_TARGETS)
    release.verify()
    (release.repository / "src/kalshi_predictor/overnight_paper" / filename).write_text(
        "# changed after typing evidence\n"
    )
    with pytest.raises(ValueError, match="MYPY_REVIEWED_SOURCE_SCOPE"):
        release.verify()
