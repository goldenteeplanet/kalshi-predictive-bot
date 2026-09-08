"""Versioned golden vectors for adversarial replay verifier conformance."""

from __future__ import annotations

import copy
import hashlib
import json
import math
from decimal import Decimal

from scripts.local.phase4mz_adversarial_backtest import run_adversarial_backtest
from scripts.local.phase4nj_reproducibility_bundle import (
    create_bundle,
    verify_bundle,
)
from scripts.local.phase4nk_portable_replay import (
    canonicalize_artifact,
    prove_portable_replay,
)

SCHEMA = "phase4nl.conformance-corpus.v1"
CORPUS_VERSION = "2026-08-28.1"
VECTOR_IDS = (
    "valid-bundle",
    "canonical-boundaries",
    "bundle-shape",
    "bundle-schema-drift",
    "bundle-missing-input",
    "bundle-corrupt-manifest",
    "bundle-model-drift",
    "bundle-scenario-order",
    "bundle-unsafe-capability",
    "bundle-output-mismatch",
    "bundle-nondeterministic",
    "canonical-version-drift",
    "canonical-ambiguous-time",
    "canonical-invalid-time",
    "canonical-nonfinite",
    "canonical-duplicate-key",
    "canonical-locale-number",
    "canonical-encoding",
    "canonical-path-leak",
    "canonical-platform-metadata",
    "canonical-nonstring-key",
    "canonical-unsupported-type",
    "portable-representation-mismatch",
)
REQUIRED_REFUSALS = {
    "BUNDLE_SHAPE_INVALID",
    "UNSUPPORTED_SCHEMA_VERSION",
    "REQUIRED_ARTIFACT_MISSING",
    "ARTIFACT_HASH_MISMATCH",
    "BUNDLE_HASH_MISMATCH",
    "MODEL_VERSION_MISMATCH",
    "SCENARIO_IDENTITY_MISMATCH",
    "SAFETY_INVARIANT_VIOLATION",
    "REPLAY_OUTPUT_MISMATCH",
    "NONDETERMINISTIC_REPLAY",
    "CANONICALIZATION_VERSION_DRIFT",
    "TIMEZONE_AMBIGUOUS",
    "TIMESTAMP_INVALID",
    "NON_FINITE_NUMBER",
    "DUPLICATE_NORMALIZED_KEY",
    "LOCALE_DEPENDENT_NUMBER",
    "UNSUPPORTED_ENCODING",
    "PATH_LEAKAGE_DETECTED",
    "PLATFORM_METADATA_PRESENT",
    "NON_STRING_KEY",
    "TYPE_UNSUPPORTED",
    "CANONICAL_BYTES_MISMATCH",
}


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _verdict(result: dict[str, object]) -> dict[str, object]:
    body = {
        "verdict": result["verdict"],
        "errors": sorted(result["errors"]),
        "result_sha256": _digest(result),
    }
    return body


def _evaluate(vector_id: str, records: list[dict[str, object]]) -> dict[str, object]:
    bundle = create_bundle(records, seed=1729)
    if vector_id == "valid-bundle":
        return verify_bundle(bundle)
    if vector_id == "canonical-boundaries":
        return canonicalize_artifact(
            {
                "caf\u00e9": Decimal("1.2300"),
                "line": "a\r\nb",
                "epoch": "1970-01-01T00:00:00+00:00",
                "offset": "2026-08-01T07:00:00-05:00",
            }
        )
    if vector_id == "bundle-shape":
        return verify_bundle(None)
    if vector_id == "bundle-schema-drift":
        bundle["schema"] = "future"
        return verify_bundle(bundle)
    if vector_id == "bundle-missing-input":
        del bundle["artifacts"]["inputs"]
        return verify_bundle(bundle)
    if vector_id == "bundle-corrupt-manifest":
        bundle["manifest"]["inputs"]["sha256"] = "0" * 64
        return verify_bundle(bundle)
    if vector_id == "bundle-model-drift":
        bundle["artifacts"]["versions"]["engine_source_sha256"] = "0" * 64
        return verify_bundle(bundle)
    if vector_id == "bundle-scenario-order":
        bundle["artifacts"]["scenario_identities"].reverse()
        return verify_bundle(bundle)
    if vector_id == "bundle-unsafe-capability":
        bundle["artifacts"]["safety"]["live_execution"] = True
        return verify_bundle(bundle)
    if vector_id == "bundle-output-mismatch":

        def mismatch(rows, *, scenario, **config):
            result = run_adversarial_backtest(rows, scenario=scenario, **config)
            result["trade_count"] += 1
            return result

        return verify_bundle(bundle, replay=mismatch)
    if vector_id == "bundle-nondeterministic":
        calls = 0

        def unstable(rows, *, scenario, **config):
            nonlocal calls
            calls += 1
            result = run_adversarial_backtest(rows, scenario=scenario, **config)
            result["nonce"] = calls
            return result

        return verify_bundle(bundle, replay=unstable)
    if vector_id == "canonical-version-drift":
        return canonicalize_artifact({}, version="future")
    if vector_id == "canonical-ambiguous-time":
        return canonicalize_artifact("2026-08-01T12:00:00")
    if vector_id == "canonical-invalid-time":
        return canonicalize_artifact("2026-99-99T12:00:00Z")
    if vector_id == "canonical-nonfinite":
        return canonicalize_artifact(math.inf)
    if vector_id == "canonical-duplicate-key":
        return canonicalize_artifact({"caf\u00e9": 1, "cafe\u0301": 2})
    if vector_id == "canonical-locale-number":
        return canonicalize_artifact("1,25")
    if vector_id == "canonical-encoding":
        return canonicalize_artifact(b"\xff")
    if vector_id == "canonical-path-leak":
        return canonicalize_artifact(r"C:\Users\operator\secret")
    if vector_id == "canonical-platform-metadata":
        return canonicalize_artifact({"hostname": "worker"})
    if vector_id == "canonical-nonstring-key":
        return canonicalize_artifact({1: "value"})
    if vector_id == "canonical-unsupported-type":
        return canonicalize_artifact({1, 2})
    if vector_id == "portable-representation-mismatch":
        return prove_portable_replay(records, records[:-1], seed=1729)
    raise ValueError("unknown vector")


def build_corpus(records: list[dict[str, object]]) -> dict[str, object]:
    vectors = []
    for vector_id in VECTOR_IDS:
        actual = _verdict(_evaluate(vector_id, copy.deepcopy(records)))
        body = {
            "id": vector_id,
            "provenance": {
                "phase": "4NL",
                "source_fixture_sha256": _digest(records),
                "evaluator": "phase4nl_conformance_corpus._evaluate",
            },
            "expected": actual,
        }
        vectors.append({**body, "vector_sha256": _digest(body)})
    manifest_body = {
        "corpus_version": CORPUS_VERSION,
        "vector_ids": list(VECTOR_IDS),
        "vector_hashes": [row["vector_sha256"] for row in vectors],
    }
    corpus = {
        "schema": SCHEMA,
        "corpus_version": CORPUS_VERSION,
        "vectors": vectors,
        "manifest": {**manifest_body, "manifest_sha256": _digest(manifest_body)},
        "safety": _safety(),
    }
    corpus["corpus_sha256"] = _digest(corpus)
    return corpus


def run_conformance(corpus: object, records: list[dict[str, object]]) -> dict[str, object]:
    errors: list[str] = []
    drift: list[str] = []
    if not isinstance(corpus, dict) or corpus.get("schema") != SCHEMA:
        return _result(["CORPUS_SCHEMA_INVALID"], [], [])
    if corpus.get("corpus_version") != CORPUS_VERSION:
        errors.append("CORPUS_VERSION_DRIFT")
    vectors = corpus.get("vectors")
    if not isinstance(vectors, list):
        return _result(sorted(set(errors + ["CORPUS_SHAPE_INVALID"])), [], [])
    ids = [row.get("id") for row in vectors if isinstance(row, dict)]
    if len(ids) != len(set(ids)):
        errors.append("DUPLICATE_VECTOR_ID")
    if set(ids) != set(VECTOR_IDS):
        errors.append("REQUIRED_VECTOR_MISSING")
    manifest = corpus.get("manifest", {})
    manifest_body = {
        "corpus_version": corpus.get("corpus_version"),
        "vector_ids": ids,
        "vector_hashes": [row.get("vector_sha256") for row in vectors if isinstance(row, dict)],
    }
    if not isinstance(manifest, dict) or manifest.get("manifest_sha256") != _digest(manifest_body):
        errors.append("CORPUS_MANIFEST_MISMATCH")
    observed_errors: set[str] = set()
    for vector in vectors:
        if not isinstance(vector, dict) or vector.get("id") not in VECTOR_IDS:
            continue
        body = {key: value for key, value in vector.items() if key != "vector_sha256"}
        if vector.get("vector_sha256") != _digest(body):
            errors.append("VECTOR_HASH_MISMATCH")
            continue
        actual = _verdict(_evaluate(vector["id"], copy.deepcopy(records)))
        observed_errors.update(actual["errors"])
        if actual != vector.get("expected"):
            drift.append(vector["id"])
    if drift:
        errors.append("VERIFIER_CONFORMANCE_DRIFT")
    missing_refusals = sorted(REQUIRED_REFUSALS - observed_errors)
    if missing_refusals:
        errors.append("REFUSAL_COVERAGE_INCOMPLETE")
    unsigned = {key: value for key, value in corpus.items() if key != "corpus_sha256"}
    if corpus.get("corpus_sha256") != _digest(unsigned):
        errors.append("CORPUS_HASH_MISMATCH")
    return _result(sorted(set(errors)), drift, missing_refusals)


def _result(errors, drift, missing_refusals):
    result = {
        "schema": SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": errors,
        "drift_vectors": drift,
        "missing_refusal_classes": missing_refusals,
        "safety": _safety(),
    }
    result["conformance_sha256"] = _digest(result)
    return result


def _safety():
    return {
        "offline_only": True,
        "persistence": False,
        "network_access": False,
        "runtime_write": False,
        "paper_order_creation": False,
        "demo_execution": False,
        "live_execution": False,
        "autopilot": False,
    }
