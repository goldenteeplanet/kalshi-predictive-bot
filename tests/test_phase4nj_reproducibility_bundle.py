from __future__ import annotations

import copy

from scripts.local.phase4mz_adversarial_backtest import run_adversarial_backtest
from scripts.local.phase4nj_reproducibility_bundle import create_bundle, verify_bundle
from tests.test_phase4mz_adversarial_backtest import _records


def _bundle():
    return create_bundle(_records(), seed=1729)


def test_bundle_is_canonical_complete_and_deterministic() -> None:
    first = _bundle()
    assert first == _bundle()
    assert set(first["artifacts"]) == {
        "inputs",
        "config",
        "seeds",
        "scenario_identities",
        "expected_outputs",
        "versions",
        "safety",
    }
    assert len(first["artifacts"]["scenario_identities"]) == 17
    assert first["artifacts"]["seeds"] == {"global": 1729, "randomness_used": False}


def test_independent_scenario_replay_matches_expected_outputs() -> None:
    result = verify_bundle(_bundle())
    assert result["verdict"] == "PASS"
    assert result["independent_scenario_orchestration"] is True
    assert result["replay_outputs_sha256"]


def test_missing_or_altered_artifacts_fail_closed() -> None:
    missing = _bundle()
    del missing["artifacts"]["inputs"]
    assert "REQUIRED_ARTIFACT_MISSING" in verify_bundle(missing)["errors"]

    altered = _bundle()
    altered["artifacts"]["inputs"][0]["yes_ask"] = "0.01"
    errors = verify_bundle(altered)["errors"]
    assert "ARTIFACT_HASH_MISMATCH" in errors
    assert "BUNDLE_HASH_MISMATCH" in errors


def test_schema_model_scenario_and_safety_changes_refuse() -> None:
    cases = (
        (
            "schema",
            lambda value: value.__setitem__("schema", "future"),
            "UNSUPPORTED_SCHEMA_VERSION",
        ),
        (
            "model",
            lambda value: value["artifacts"]["versions"].__setitem__(
                "engine_source_sha256", "0" * 64
            ),
            "MODEL_VERSION_MISMATCH",
        ),
        (
            "scenario",
            lambda value: value["artifacts"]["scenario_identities"].reverse(),
            "SCENARIO_IDENTITY_MISMATCH",
        ),
        (
            "safety",
            lambda value: value["artifacts"]["safety"].__setitem__("paper_order_creation", True),
            "SAFETY_INVARIANT_VIOLATION",
        ),
    )
    for _, mutation, expected in cases:
        bundle = _bundle()
        mutation(bundle)
        assert expected in verify_bundle(bundle)["errors"]


def test_output_mismatch_and_nondeterminism_are_distinguished() -> None:
    def mismatching(records, *, scenario, **config):
        result = run_adversarial_backtest(records, scenario=scenario, **config)
        result["trade_count"] += 1
        return result

    assert "REPLAY_OUTPUT_MISMATCH" in verify_bundle(_bundle(), replay=mismatching)["errors"]

    calls = 0

    def unstable(records, *, scenario, **config):
        nonlocal calls
        calls += 1
        result = copy.deepcopy(run_adversarial_backtest(records, scenario=scenario, **config))
        result["unstable_nonce"] = calls
        return result

    errors = verify_bundle(_bundle(), replay=unstable)["errors"]
    assert "NONDETERMINISTIC_REPLAY" in errors
    assert "REPLAY_OUTPUT_MISMATCH" in errors


def test_bundle_creation_does_not_mutate_inputs() -> None:
    records = _records()
    original = copy.deepcopy(records)
    create_bundle(records)
    assert records == original


def test_bundle_has_no_order_or_execution_capability() -> None:
    safety = verify_bundle(_bundle())["safety"]
    assert safety["offline_only"] is True
    assert all(value is False for key, value in safety.items() if key != "offline_only")
