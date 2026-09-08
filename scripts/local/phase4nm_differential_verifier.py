"""Structurally independent verifier and differential cross-check proof."""

from __future__ import annotations

import copy
import hashlib
import inspect
import json
import math
import re
import unicodedata
from datetime import UTC, datetime
from decimal import Decimal

from scripts.local.phase4mz_adversarial_backtest import SCENARIOS, run_adversarial_backtest
from scripts.local.phase4nj_reproducibility_bundle import (
    ENGINE_SCHEMA,
    create_bundle,
)
from scripts.local.phase4nj_reproducibility_bundle import (
    SCHEMA as BUNDLE_SCHEMA,
)
from scripts.local.phase4nl_conformance_corpus import (
    CORPUS_VERSION,
    VECTOR_IDS,
)
from scripts.local.phase4nl_conformance_corpus import (
    SCHEMA as CORPUS_SCHEMA,
)

SCHEMA = "phase4nm.differential-verifier.v1"
CANONICAL_VERSION = "phase4nk-canonical-json-v1"
_TIME = re.compile(r"^\d{4}-\d{2}-\d{2}T")
_NUMBER = re.compile(r"^-?(?:0|[1-9]\d*)(?:\.\d+)?$")
_LOCALE = re.compile(r"^-?\d{1,3}(?:[.,]\d{3})*[,.]\d+$")
_DRIVE = re.compile(r"^[A-Za-z]:[\\/]")
_HOST_KEYS = {"platform", "os", "hostname", "absolute_path", "cwd"}


class SecondaryRefusal(Exception):
    pass


def _json_hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _engine_hash() -> str:
    return hashlib.sha256(inspect.getsource(run_adversarial_backtest).encode()).hexdigest()


def _decimal(value: Decimal) -> str:
    if not value.is_finite():
        raise SecondaryRefusal("NON_FINITE_NUMBER")
    rendered = format(value.normalize(), "f")
    return "0" if Decimal(rendered) == 0 else rendered


def _secondary_normalize(value: object) -> object:
    if value is None or type(value) in {bool, int}:
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise SecondaryRefusal("NON_FINITE_NUMBER")
        return _decimal(Decimal(str(value)))
    if isinstance(value, Decimal):
        return _decimal(value)
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SecondaryRefusal("UNSUPPORTED_ENCODING") from exc
    if isinstance(value, str):
        text = unicodedata.normalize("NFC", value).replace("\r\n", "\n").replace("\r", "\n")
        if _DRIVE.match(text) or text.startswith(("/home/", "/Users/", "/mnt/")):
            raise SecondaryRefusal("PATH_LEAKAGE_DETECTED")
        if _TIME.match(text):
            if not (text.endswith("Z") or re.search(r"[+-]\d{2}:\d{2}$", text)):
                raise SecondaryRefusal("TIMEZONE_AMBIGUOUS")
            try:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError as exc:
                raise SecondaryRefusal("TIMESTAMP_INVALID") from exc
            if parsed.tzinfo is None:
                raise SecondaryRefusal("TIMEZONE_AMBIGUOUS")
            rendered = parsed.astimezone(UTC).isoformat(timespec="microseconds")
            return rendered.replace("+00:00", "Z").replace(".000000Z", "Z")
        if "," in text and _LOCALE.match(text):
            raise SecondaryRefusal("LOCALE_DEPENDENT_NUMBER")
        return _decimal(Decimal(text)) if _NUMBER.match(text) else text
    if isinstance(value, list):
        return [_secondary_normalize(item) for item in value]
    if isinstance(value, dict):
        normalized = {}
        for raw_key, item in value.items():
            if not isinstance(raw_key, str):
                raise SecondaryRefusal("NON_STRING_KEY")
            key = unicodedata.normalize("NFC", raw_key)
            if key in normalized:
                raise SecondaryRefusal("DUPLICATE_NORMALIZED_KEY")
            if key.casefold() in _HOST_KEYS:
                raise SecondaryRefusal("PLATFORM_METADATA_PRESENT")
            normalized[key] = _secondary_normalize(item)
        return normalized
    raise SecondaryRefusal("TYPE_UNSUPPORTED")


def secondary_canonicalize(value: object, *, version: str = CANONICAL_VERSION) -> dict[str, object]:
    errors = []
    normalized = None
    if version != CANONICAL_VERSION:
        errors.append("CANONICALIZATION_VERSION_DRIFT")
    else:
        try:
            normalized = _secondary_normalize(value)
        except SecondaryRefusal as exc:
            errors.append(str(exc))
    canonical = (
        json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        if not errors
        else None
    )
    return {
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": errors,
        "normalized": normalized,
        "canonical_sha256": hashlib.sha256(canonical).hexdigest() if canonical else None,
    }


def secondary_verify_bundle(bundle: object, *, replay=None) -> dict[str, object]:
    errors = []
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
        return {"verdict": "REFUSE", "errors": ["BUNDLE_SHAPE_INVALID"]}
    if bundle.get("schema") != BUNDLE_SCHEMA:
        errors.append("UNSUPPORTED_SCHEMA_VERSION")
    artifacts, manifest = bundle.get("artifacts"), bundle.get("manifest")
    if not isinstance(artifacts, dict) or not isinstance(manifest, dict):
        return {"verdict": "REFUSE", "errors": sorted(set(errors + ["BUNDLE_SHAPE_INVALID"]))}
    if required - set(artifacts):
        errors.append("REQUIRED_ARTIFACT_MISSING")
    for name in sorted(required & set(artifacts)):
        entry = manifest.get(name)
        digest = _json_hash(artifacts[name])
        if not isinstance(entry, dict) or entry.get("sha256") != digest:
            errors.append("ARTIFACT_HASH_MISMATCH")
    unsigned = {key: value for key, value in bundle.items() if key != "bundle_sha256"}
    if bundle.get("bundle_sha256") != _json_hash(unsigned):
        errors.append("BUNDLE_HASH_MISMATCH")
    versions = artifacts.get("versions", {})
    if (
        not isinstance(versions, dict)
        or versions.get("engine_schema") != ENGINE_SCHEMA
        or versions.get("engine_source_sha256") != _engine_hash()
    ):
        errors.append("MODEL_VERSION_MISMATCH")
    identities = artifacts.get("scenario_identities", [])
    names = (
        [row.get("scenario") for row in identities if isinstance(row, dict)]
        if isinstance(identities, list)
        else []
    )
    if names != list(SCENARIOS):
        errors.append("SCENARIO_IDENTITY_MISMATCH")
    if artifacts.get("safety") != _bundle_safety():
        errors.append("SAFETY_INVARIANT_VIOLATION")
    if errors:
        return {"verdict": "REFUSE", "errors": sorted(set(errors))}
    runner = replay or run_adversarial_backtest
    first = [
        runner(copy.deepcopy(artifacts["inputs"]), scenario=name, **artifacts["config"])
        for name in SCENARIOS
    ]
    second = [
        runner(copy.deepcopy(artifacts["inputs"]), scenario=name, **artifacts["config"])
        for name in SCENARIOS
    ]
    if first != second:
        errors.append("NONDETERMINISTIC_REPLAY")
    if first != artifacts["expected_outputs"]:
        errors.append("REPLAY_OUTPUT_MISMATCH")
    return {"verdict": "PASS" if not errors else "REFUSE", "errors": sorted(set(errors))}


def _bundle_safety():
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


def _secondary_vector(vector_id: str, records: list[dict[str, object]]) -> dict[str, object]:
    bundle = create_bundle(records, seed=1729)
    if vector_id == "valid-bundle":
        return secondary_verify_bundle(bundle)
    if vector_id == "canonical-boundaries":
        return secondary_canonicalize(
            {
                "caf\u00e9": Decimal("1.2300"),
                "line": "a\r\nb",
                "epoch": "1970-01-01T00:00:00+00:00",
                "offset": "2026-08-01T07:00:00-05:00",
            }
        )
    if vector_id == "bundle-shape":
        return secondary_verify_bundle(None)
    if vector_id == "bundle-schema-drift":
        bundle["schema"] = "future"
    elif vector_id == "bundle-missing-input":
        del bundle["artifacts"]["inputs"]
    elif vector_id == "bundle-corrupt-manifest":
        bundle["manifest"]["inputs"]["sha256"] = "0" * 64
    elif vector_id == "bundle-model-drift":
        bundle["artifacts"]["versions"]["engine_source_sha256"] = "0" * 64
    elif vector_id == "bundle-scenario-order":
        bundle["artifacts"]["scenario_identities"].reverse()
    elif vector_id == "bundle-unsafe-capability":
        bundle["artifacts"]["safety"]["live_execution"] = True
    elif vector_id == "bundle-output-mismatch":

        def mismatch(rows, *, scenario, **config):
            result = run_adversarial_backtest(rows, scenario=scenario, **config)
            result["trade_count"] += 1
            return result

        return secondary_verify_bundle(bundle, replay=mismatch)
    elif vector_id == "bundle-nondeterministic":
        state = {"calls": 0}

        def unstable(rows, *, scenario, **config):
            state["calls"] += 1
            result = run_adversarial_backtest(rows, scenario=scenario, **config)
            result["nonce"] = state["calls"]
            return result

        return secondary_verify_bundle(bundle, replay=unstable)
    elif vector_id == "canonical-version-drift":
        return secondary_canonicalize({}, version="future")
    elif vector_id == "canonical-ambiguous-time":
        return secondary_canonicalize("2026-08-01T12:00:00")
    elif vector_id == "canonical-invalid-time":
        return secondary_canonicalize("2026-99-99T12:00:00Z")
    elif vector_id == "canonical-nonfinite":
        return secondary_canonicalize(math.inf)
    elif vector_id == "canonical-duplicate-key":
        return secondary_canonicalize({"caf\u00e9": 1, "cafe\u0301": 2})
    elif vector_id == "canonical-locale-number":
        return secondary_canonicalize("1,25")
    elif vector_id == "canonical-encoding":
        return secondary_canonicalize(b"\xff")
    elif vector_id == "canonical-path-leak":
        return secondary_canonicalize(r"C:\Users\operator\secret")
    elif vector_id == "canonical-platform-metadata":
        return secondary_canonicalize({"hostname": "worker"})
    elif vector_id == "canonical-nonstring-key":
        return secondary_canonicalize({1: "value"})
    elif vector_id == "canonical-unsupported-type":
        return secondary_canonicalize({1, 2})
    elif vector_id == "portable-representation-mismatch":
        left, right = secondary_canonicalize(records), secondary_canonicalize(records[:-1])
        errors = (
            []
            if left["canonical_sha256"] == right["canonical_sha256"]
            else ["CANONICAL_BYTES_MISMATCH"]
        )
        return {"verdict": "PASS" if not errors else "REFUSE", "errors": errors}
    return secondary_verify_bundle(bundle)


def differential_cross_check(corpus: object, records: list[dict[str, object]], *, overrides=None):
    errors, disagreements = [], []
    if (
        not isinstance(corpus, dict)
        or corpus.get("schema") != CORPUS_SCHEMA
        or corpus.get("corpus_version") != CORPUS_VERSION
    ):
        return _result(["CORPUS_INCOMPATIBLE"], [], [])
    vectors = corpus.get("vectors", [])
    ids = [row.get("id") for row in vectors if isinstance(row, dict)]
    missing = sorted(set(VECTOR_IDS) - set(ids))
    if missing:
        errors.append("MISSING_CORPUS_CASE")
    for row in vectors:
        if not isinstance(row, dict) or row.get("id") not in VECTOR_IDS:
            continue
        actual = _secondary_vector(row["id"], copy.deepcopy(records))
        if overrides and row["id"] in overrides:
            actual = overrides[row["id"]]
        expected = row.get("expected", {})
        if actual.get("verdict") != expected.get("verdict") or sorted(
            actual.get("errors", [])
        ) != sorted(expected.get("errors", [])):
            disagreements.append(row["id"])
    if disagreements:
        errors.append("DIFFERENTIAL_DISAGREEMENT")
    result = _result(sorted(set(errors)), disagreements, missing)
    result["implementation_coupling"] = False
    result["secondary_source_sha256"] = hashlib.sha256(
        inspect.getsource(secondary_verify_bundle).encode()
    ).hexdigest()
    result["proof_sha256"] = _json_hash(result)
    return result


def _result(errors, disagreements, missing):
    return {
        "schema": SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": errors,
        "disagreement_vectors": disagreements,
        "missing_vectors": missing,
        "safety": _safety(),
    }


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
