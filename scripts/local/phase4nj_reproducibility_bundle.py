"""Offline adversarial-backtest bundle creation and independent replay proof."""

from __future__ import annotations

import copy
import hashlib
import inspect
import json
from collections.abc import Callable

from scripts.local.phase4mz_adversarial_backtest import (
    SCENARIOS,
    run_adversarial_backtest,
)

SCHEMA = "phase4nj.reproducibility-bundle.v1"
ENGINE_SCHEMA = "phase4mz.adversarial-backtest.v1"
DEFAULT_CONFIG = {
    "minimum_edge": "0.05",
    "fee_per_contract": "0.01",
    "max_quote_age_seconds": 300,
}


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _safety() -> dict[str, bool]:
    return {
        "offline_only": True,
        "database_session": False,
        "persistence": False,
        "network_access": False,
        "runtime_write": False,
        "service_control": False,
        "paper_order_creation": False,
        "demo_execution": False,
        "live_execution": False,
        "autopilot": False,
    }


def _model_fingerprint() -> str:
    return hashlib.sha256(inspect.getsource(run_adversarial_backtest).encode()).hexdigest()


def create_bundle(
    records: list[dict[str, object]],
    *,
    config: dict[str, object] | None = None,
    seed: int = 0,
) -> dict[str, object]:
    """Create a canonical in-memory bundle; no filesystem or runtime writes occur."""
    effective_config = copy.deepcopy(config or DEFAULT_CONFIG)
    inputs = copy.deepcopy(records)
    reports = [
        run_adversarial_backtest(inputs, scenario=scenario, **effective_config)
        for scenario in SCENARIOS
    ]
    artifacts = {
        "inputs": inputs,
        "config": effective_config,
        "seeds": {"global": seed, "randomness_used": False},
        "scenario_identities": [
            {"ordinal": index, "scenario": scenario, "sha256": _digest(scenario)}
            for index, scenario in enumerate(SCENARIOS)
        ],
        "expected_outputs": reports,
        "versions": {
            "bundle_schema": SCHEMA,
            "engine_schema": ENGINE_SCHEMA,
            "engine_source_sha256": _model_fingerprint(),
        },
        "safety": _safety(),
    }
    manifest = {
        name: {"sha256": _digest(value), "canonical_bytes": len(_canonical(value))}
        for name, value in artifacts.items()
    }
    envelope = {"schema": SCHEMA, "artifacts": artifacts, "manifest": manifest}
    envelope["bundle_sha256"] = _digest(envelope)
    return envelope


def verify_bundle(
    bundle: object,
    *,
    replay: Callable[..., dict[str, object]] | None = None,
) -> dict[str, object]:
    """Verify and replay a bundle through scenario-by-scenario orchestration."""
    errors: list[str] = []
    required = {
        "inputs",
        "config",
        "seeds",
        "scenario_identities",
        "expected_outputs",
        "versions",
        "safety",
    }
    if not isinstance(bundle, dict):
        return _verification(["BUNDLE_SHAPE_INVALID"])
    if bundle.get("schema") != SCHEMA:
        errors.append("UNSUPPORTED_SCHEMA_VERSION")
    artifacts = bundle.get("artifacts")
    manifest = bundle.get("manifest")
    if not isinstance(artifacts, dict) or not isinstance(manifest, dict):
        return _verification(sorted(set(errors + ["BUNDLE_SHAPE_INVALID"])))
    missing = sorted(required - set(artifacts))
    if missing:
        errors.append("REQUIRED_ARTIFACT_MISSING")
    for name in sorted(required & set(artifacts)):
        entry = manifest.get(name)
        if not isinstance(entry, dict) or entry.get("sha256") != _digest(artifacts[name]):
            errors.append("ARTIFACT_HASH_MISMATCH")
    unsigned = {key: value for key, value in bundle.items() if key != "bundle_sha256"}
    if bundle.get("bundle_sha256") != _digest(unsigned):
        errors.append("BUNDLE_HASH_MISMATCH")
    versions = artifacts.get("versions", {})
    if (
        not isinstance(versions, dict)
        or versions.get("engine_schema") != ENGINE_SCHEMA
        or versions.get("engine_source_sha256") != _model_fingerprint()
    ):
        errors.append("MODEL_VERSION_MISMATCH")
    identities = artifacts.get("scenario_identities", [])
    identity_names = (
        [row.get("scenario") for row in identities if isinstance(row, dict)]
        if isinstance(identities, list)
        else []
    )
    if identity_names != list(SCENARIOS):
        errors.append("SCENARIO_IDENTITY_MISMATCH")
    if artifacts.get("safety") != _safety():
        errors.append("SAFETY_INVARIANT_VIOLATION")
    if errors:
        return _verification(sorted(set(errors)))

    engine = replay or run_adversarial_backtest
    inputs = artifacts["inputs"]
    config = artifacts["config"]
    expected = artifacts["expected_outputs"]
    first = [engine(copy.deepcopy(inputs), scenario=name, **config) for name in SCENARIOS]
    second = [engine(copy.deepcopy(inputs), scenario=name, **config) for name in SCENARIOS]
    if first != second:
        errors.append("NONDETERMINISTIC_REPLAY")
    if first != expected:
        errors.append("REPLAY_OUTPUT_MISMATCH")
    return _verification(sorted(set(errors)), replay_outputs_sha256=_digest(first))


def _verification(errors: list[str], replay_outputs_sha256: str | None = None) -> dict[str, object]:
    result = {
        "schema": SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": errors,
        "replay_outputs_sha256": replay_outputs_sha256,
        "independent_scenario_orchestration": True,
        "safety": _safety(),
    }
    result["verification_sha256"] = _digest(result)
    return result
