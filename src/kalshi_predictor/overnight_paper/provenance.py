"""Deterministic provenance and reviewed local-boundary checks, not attestations.

Provenance completeness does not establish calibrated skill or settlement authority.
Callers must separately close those semantic gates before considering activation.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from kalshi_predictor.advanced_risk.engine import AdvancedRiskDecision
from kalshi_predictor.overnight_paper.source_health import aware
from kalshi_predictor.position_sizing.sizer import PositionSizingDecision


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()


@dataclass(frozen=True)
class Artifact:
    sha256: str
    payload: bytes

    def decode(self) -> dict[str, Any]:
        if hashlib.sha256(self.payload).hexdigest() != self.sha256:
            raise ValueError("ARTIFACT_HASH_MISMATCH")
        decoded = json.loads(self.payload)
        if not isinstance(decoded, dict):
            raise ValueError("ARTIFACT_OBJECT_REQUIRED")
        return decoded


@dataclass(frozen=True)
class Verification:
    passed: bool
    blockers: tuple[str, ...]
    verified_hashes: tuple[str, ...] = ()
    scope: str = "PROVENANCE_COMPLETENESS_ONLY"
    model_calibration_verified: bool = False
    settlement_rules_verified: bool = False


def validate_source_visibility(
    source: dict[str, Any],
    *,
    decision_at: datetime,
    now: datetime,
) -> None:
    """Verify original clocks without inventing provider clocks for REST metadata.

    Receipt-only evidence is restricted to named public catalog/book/geography
    endpoints. It cannot stand in for forecast or analytical price observations.
    Provider-clock freshness remains a separate unchanged analytical check.
    """
    at, reference = aware(decision_at), aware(now)
    if not source.get("url") or not source.get("body"):
        raise ValueError("ORIGINAL_SOURCE_PAYLOAD_REQUIRED")
    basis = source.get("clock_basis", "provider")
    if basis == "coinbase-btc-trade-closed-candles-v1":
        from .crypto_source import verify_coinbase_source

        verify_coinbase_source(source, decision_at=at, now=reference)
        return
    if basis == "public_rest_receipt":
        url = source["url"]
        kalshi = isinstance(url, str) and re.fullmatch(
            r"https://external-api\.kalshi\.com/trade-api/v2/"
            r"(?:markets/[A-Z0-9][A-Z0-9._-]*(?:/orderbook)?|"
            r"events/[A-Z0-9][A-Z0-9._-]*|series/[A-Z0-9][A-Z0-9._-]*)",
            url,
        )
        station = isinstance(url, str) and re.fullmatch(
            r"https://api\.weather\.gov/stations/[A-Z0-9]{4}",
            url,
        )
        point = isinstance(url, str) and re.fullmatch(
            r"https://api\.weather\.gov/points/(-?\d{1,3}(?:\.\d{1,6})?),"
            r"(-?\d{1,3}(?:\.\d{1,6})?)",
            url,
        )
        valid_point = bool(point and abs(float(point[1])) <= 90 and abs(float(point[2])) <= 180)
        if not (kalshi or station or valid_point):
            raise ValueError("RECEIPT_BASIS_METADATA_ENDPOINT_REQUIRED")
        if source["provider_updated_at"] is not None or source["provider_generated_at"] is not None:
            raise ValueError("RECEIPT_BASIS_PROVIDER_CLOCKS_MUST_BE_NULL")
        receipt, available = aware(source["received_at"]), aware(source["available_at"])
        if not available == receipt <= at <= reference or not (
            0 <= (reference - receipt).total_seconds() <= 60
        ):
            raise ValueError("RECEIPT_BASIS_VISIBILITY_INVALID")
    elif basis == "provider":
        clocks = [
            aware(source[key])
            for key in (
                "provider_updated_at",
                "provider_generated_at",
                "available_at",
                "received_at",
            )
        ]
        if clocks != sorted(clocks) or any(value > at for value in clocks):
            raise ValueError("FUTURE_OR_INCONSISTENT_SOURCE_VISIBILITY")
    else:
        raise ValueError("UNSUPPORTED_SOURCE_CLOCK_BASIS")


def validate_fixed_heuristic_manifest(
    *,
    model: dict[str, Any],
    decision: dict[str, Any],
    forecast: dict[str, Any],
    config: dict[str, Any],
    at: datetime,
    has_training_artifacts: bool,
) -> datetime:
    """Bind an honestly untrained frozen model; this never evaluates model skill.

    Freeze the complete configuration artifact as parameters, including defaults,
    rather than an asserted selected subset. Exact original code is independently
    checked by the calling provenance verifier.
    """
    if any(row["model_kind"] != "fixed_heuristic" for row in (model, decision, forecast)):
        raise ValueError("FIXED_HEURISTIC_KIND_BINDING_MISMATCH")
    if any(row["training_cutoff"] is not None for row in (model, decision, forecast)):
        raise ValueError("FIXED_HEURISTIC_HAS_NO_TRAINING_CUTOFF")
    if model["training_dataset_hashes"] != [] or has_training_artifacts:
        raise ValueError("FIXED_HEURISTIC_CANNOT_CLAIM_TRAINING_ARTIFACTS")
    if any(
        not isinstance(model[key], str) or not model[key].strip() for key in ("name", "version")
    ):
        raise ValueError("FIXED_HEURISTIC_MODEL_IDENTITY_REQUIRED")
    frozen = aware(model["frozen_at"])
    if not aware(model["created_at"]) <= frozen <= aware(model["available_at"]) <= aware(at):
        raise ValueError("FIXED_HEURISTIC_FREEZE_VISIBILITY_INVALID")
    parameters = model["parameters"]
    if not isinstance(parameters, dict) or not parameters or parameters != config:
        raise ValueError("FIXED_HEURISTIC_COMPLETE_PARAMETERS_REQUIRED")
    parameters_hash = canonical_hash(parameters)
    if model["parameters_sha256"] != parameters_hash:
        raise ValueError("FIXED_HEURISTIC_PARAMETER_HASH_MISMATCH")
    for row in (decision, forecast):
        if (
            row["model_parameters_sha256"] != parameters_hash
            or row["model_code_sha256"] != model["code_sha256"]
            or row["model_name"] != model["name"]
            or row["model_version"] != model["version"]
            or aware(row["model_frozen_at"]) != frozen
        ):
            raise ValueError("FIXED_HEURISTIC_MANIFEST_BINDING_MISMATCH")
    return frozen


def verify_full_provenance(
    *,
    decision: dict[str, Any],
    decision_id: str,
    artifacts: dict[str, Artifact],
    source_artifacts: tuple[Artifact, ...],
    now: datetime,
    training_artifacts: tuple[Artifact, ...] = (),
    model_code: bytes = b"",
    phase3m: PositionSizingDecision | None = None,
    phase3n: AdvancedRiskDecision | None = None,
) -> Verification:
    """Verify every bound original record and original decision-time visibility.

    Artifact roles are forecast, snapshot, model, config, phase3m and phase3n.
    Sources carry original body, provider_generated_at, provider_updated_at,
    available_at, received_at and url. The forecast names their exact hashes.
    A model manifest includes actual training dataset hashes and cutoff; this
    verifies lineage, never whether that model is calibrated or economically valid.
    """
    try:
        if canonical_hash(decision) != decision_id:
            raise ValueError("DECISION_HASH_MISMATCH")
        required = {"forecast", "snapshot", "model", "config", "phase3m", "phase3n"}
        if set(artifacts) != required or not source_artifacts:
            raise ValueError("ALL_ORIGINAL_ARTIFACTS_REQUIRED")
        rows = {role: artifact.decode() for role, artifact in artifacts.items()}
        if not isinstance(phase3m, PositionSizingDecision) or not isinstance(
            phase3n, AdvancedRiskDecision
        ):
            raise ValueError("ACTUAL_ENGINE_OUTPUTS_REQUIRED")
        if rows["phase3m"] != phase3m.as_dict() or rows["phase3n"] != phase3n.as_dict():
            raise ValueError("ORIGINAL_ENGINE_OUTPUT_MISMATCH")
        sources = [artifact.decode() for artifact in source_artifacts]
        source_hashes = tuple(source.sha256 for source in source_artifacts)
        if len(set(source_hashes)) != len(source_hashes):
            raise ValueError("DUPLICATE_SOURCE_ARTIFACT")
        if decision.get("source_hashes") != list(source_hashes):
            raise ValueError("DECISION_SOURCE_HASH_MISMATCH")
        for role, artifact in artifacts.items():
            if decision.get(role + "_artifact_sha256") != artifact.sha256:
                raise ValueError("DECISION_ARTIFACT_BINDING:" + role)
        at, reference, close = (
            aware(decision["decision_at"]),
            aware(now),
            aware(decision["close_time"]),
        )
        if not at <= reference < close:
            raise ValueError("DECISION_OR_CLOSE_CLOCK_INVALID")
        if (reference - at).total_seconds() > 60:
            raise ValueError("DECISION_REQUALIFICATION_REQUIRED")
        ticker, event, series = (decision[key] for key in ("ticker", "event_id", "series"))
        if not all(isinstance(value, str) and value for value in (ticker, event, series)):
            raise ValueError("EXACT_IDENTITY_REQUIRED")
        forecast, snapshot, model = (rows[key] for key in ("forecast", "snapshot", "model"))
        model_kind = model.get("model_kind", "trained")
        if model_kind not in {"trained", "fixed_heuristic"}:
            raise ValueError("UNSUPPORTED_MODEL_KIND")
        if any(row.get("model_kind", "trained") != model_kind for row in (decision, forecast)):
            raise ValueError("MODEL_KIND_BINDING_MISMATCH")
        if model_kind == "fixed_heuristic":
            validate_fixed_heuristic_manifest(
                model=model,
                decision=decision,
                forecast=forecast,
                config=rows["config"],
                at=at,
                has_training_artifacts=bool(training_artifacts),
            )
        for role in ("forecast", "snapshot"):
            if any(
                rows[role].get(key) != expected
                for key, expected in (
                    ("ticker", ticker),
                    ("event_id", event),
                    ("series", series),
                )
            ):
                raise ValueError("ARTIFACT_IDENTITY_MISMATCH:" + role)
        if forecast["id"] != decision["forecast_id"] or snapshot["id"] != decision["snapshot_id"]:
            raise ValueError("FORECAST_OR_SNAPSHOT_ID_MISMATCH")
        if forecast["source_hashes"] != list(source_hashes):
            raise ValueError("FORECAST_SOURCE_HASH_MISMATCH")
        if forecast["model_artifact_sha256"] != artifacts["model"].sha256:
            raise ValueError("FORECAST_MODEL_HASH_MISMATCH")
        if (
            forecast["model_version"] != model["version"]
            or model["version"] != decision["model_version"]
        ):
            raise ValueError("MODEL_VERSION_MISMATCH")
        if forecast["probability"] != decision["forecast_probability"]:
            raise ValueError("FORECAST_PROBABILITY_MISMATCH")
        probability = float(forecast["probability"])
        if not 0 <= probability <= 1:
            raise ValueError("FORECAST_PROBABILITY_INVALID")
        if canonical_hash(rows["config"]) != decision["config_hash"]:
            raise ValueError("CONFIG_HASH_MISMATCH")
        if canonical_hash(snapshot["book"]) != decision["snapshot_book_hash"]:
            raise ValueError("SNAPSHOT_BOOK_HASH_MISMATCH")
        for role, version in (("phase3m", "3M"), ("phase3n", "3N")):
            engine = rows[role]
            if engine["version"] != version or canonical_hash(engine) != decision[role + "_hash"]:
                raise ValueError("ENGINE_OUTPUT_BINDING:" + role)
            if aware(engine["decision_timestamp"]) != at:
                raise ValueError("ENGINE_DECISION_CLOCK_MISMATCH")
        # These are the original clock fields, not fresh attestation timestamps.
        for role, keys in (
            ("forecast", ("generated_at", "available_at")),
            ("snapshot", ("captured_at", "available_at")),
            (
                "model",
                ("created_at", "frozen_at", "available_at")
                if model_kind == "fixed_heuristic"
                else ("training_cutoff", "created_at", "available_at"),
            ),
        ):
            times = [aware(rows[role][key]) for key in keys]
            if any(value > at for value in times) or times != sorted(times):
                raise ValueError("FUTURE_OR_INCONSISTENT_ARTIFACT_VISIBILITY:" + role)
        for source in sources:
            validate_source_visibility(source, decision_at=at, now=reference)
            if source.get("clock_basis") == "coinbase-btc-trade-closed-candles-v1":
                from .crypto_source import verify_coinbase_binding

                verify_coinbase_binding(source, decision=decision, now=reference, forecast=forecast)
        if not model.get("code_sha256") or (
            model_kind == "trained" and not model.get("training_dataset_hashes")
        ):
            raise ValueError("MODEL_TRAINING_LINEAGE_REQUIRED")
        if decision.get("model_code_sha256") != model["code_sha256"]:
            raise ValueError("MODEL_CODE_HASH_MISMATCH")
        if not model_code or hashlib.sha256(model_code).hexdigest() != model["code_sha256"]:
            raise ValueError("ORIGINAL_MODEL_CODE_REQUIRED")
        if model_kind == "fixed_heuristic":
            return Verification(
                True, (), tuple(a.sha256 for a in artifacts.values()) + source_hashes
            )
        if not training_artifacts or model["training_dataset_hashes"] != [
            artifact.sha256 for artifact in training_artifacts
        ]:
            raise ValueError("ORIGINAL_TRAINING_ARTIFACTS_REQUIRED")
        cutoff = aware(model["training_cutoff"])
        for artifact in training_artifacts:
            dataset = artifact.decode()
            records = dataset.get("records")
            if not isinstance(records, list) or not records:
                raise ValueError("TRAINING_RECORDS_REQUIRED")
            for row in records:
                if aware(row["available_at"]) > cutoff or aware(row["label_available_at"]) > cutoff:
                    raise ValueError("TRAINING_DATA_NOT_VISIBLE_AT_CUTOFF")
                if not row.get("source_sha256") or not row.get("label_source_sha256"):
                    raise ValueError("TRAINING_SOURCE_LINEAGE_REQUIRED")
                if canonical_hash(row["source_payload"]) != row["source_sha256"] or (
                    canonical_hash(row["label_payload"]) != row["label_source_sha256"]
                ):
                    raise ValueError("TRAINING_SOURCE_PAYLOAD_HASH_MISMATCH")
        return Verification(True, (), tuple(a.sha256 for a in artifacts.values()) + source_hashes)
    except (ValueError, TypeError, KeyError, AttributeError, ArithmeticError) as exc:
        return Verification(False, (str(exc),))


# Pinned source reviewed for the local ledger/simulator boundary. Changing any
# entry requires another source review, not a caller-provided PASS/hash manifest.
AUDITED_BOUNDARY_SHA256: dict[str, str] = {
    "kalshi_predictor.advanced_risk.engine": (
        "dd81729c2c276fdb0a706858b9d4d813994153fa7037637ee23b26e8d39ceffd"
    ),
    "kalshi_predictor.advanced_risk.repository": (
        "7a6736e9a419cb65acd04f0aa499bec17ec620d2d2e50f33fbe675a1a9f5d163"
    ),
    "kalshi_predictor.advanced_risk.service": (
        "2c92a375a6db6c2bbea6a966c7d5aec52d46850a95c3e27916b1d99ec1e65b49"
    ),
    "kalshi_predictor.config": ("42d3c364142f296fa8447c365e77707b382b7b13ae4223ba6d5fdf7bbf4e0770"),
    "kalshi_predictor.data.repositories": (
        "af405031cec92008cbaab0d381096d4ac456bc60cc46fb33692838ef6abdf2db"
    ),
    "kalshi_predictor.data.schema": (
        "c614333a03dd021f7d7685c7efe0e25c9931bcc922b8438603896dc1900e0553"
    ),
    "kalshi_predictor.memory.repository": (
        "cd39dc72aeac3bdbb7e9df05f6a244496f7384b02b5f6c1f1882872c655de989"
    ),
    "kalshi_predictor.overnight_paper.activation": (
        "1729a0b041458ba267704cfe06c94169a98a0b27a5a9227ffb179d041d74bc2f"
    ),
    "kalshi_predictor.overnight_paper.boundary": (
        "7e8647e9cf73874dbcc9df4c7b324b86fbaaca21a4f9981c3c71b9a85227e6e8"
    ),
    "kalshi_predictor.overnight_paper.boundary_gate": (
        "20a91e63a471ac7b9e52535391e843c7043a874fdef2fd83cf885f4e05cf33db"
    ),
    "kalshi_predictor.overnight_paper.crypto_source": (
        "79868e4f373cbc4a57753dd5d9704f4f6f3ab256dfea0a70de6ba6fa947fc9d2"
    ),
    "kalshi_predictor.overnight_paper.coordinator": (
        "4cccb4c099d27605a5b3a2b9b7990b766c369daa36ea628427daa05d2cc57e88"
    ),
    "kalshi_predictor.overnight_paper.dataset_store": (
        "37237c7e0f4f19c21d1b221e1a86be4b188fc90ab6bf732bbef101c1ce19e9e5"
    ),
    "kalshi_predictor.overnight_paper.evaluation_dataset": (
        "e9fd430f19f4b54224071b85c9602716cf3c23b7b738e16571e7b361799b4bb0"
    ),
    "kalshi_predictor.overnight_paper.gate_context": (
        "efe309c97702acb84537196f29a38bfa78ba4c19b7a650ac4b35af9e881283b8"
    ),
    "kalshi_predictor.overnight_paper.model_release": (
        "fc01bea9fbfa561344525a91f16afa80fc121bde51512175486c584330e164ab"
    ),
    "kalshi_predictor.overnight_paper.monitoring": (
        "b03fc26ef422cdf29e845a08c9dfd4a7a1c4cd5a1a7b750ff3b04812ddd1254a"
    ),
    "kalshi_predictor.overnight_paper.provenance_gate": (
        "cb5a7fcfb8ffe943a29415d4474bcca6e883876c2a2d0eb679790a303cdf57b5"
    ),
    "kalshi_predictor.overnight_paper.qualification": (
        "6a23eb6a174b4d824557845d925792176ca006ac4fdab1a563136ccdf0785640"
    ),
    "kalshi_predictor.overnight_paper.release_typing": (
        "e0c737e8a2f3e79ab4f64f78d2b2305be7ffbc638c7433022ed2a143c6db08cb"
    ),
    "kalshi_predictor.overnight_paper.rule_verifier": (
        "66c87dcc2c014601f12d3c67e5b7b696a820e56ef6649da97873edbec69449aa"
    ),
    "kalshi_predictor.overnight_paper.runtime_liveness": (
        "8260a4d987c5f9cc6b0dbdb1e27754c52294418d6c96d5547e8314cbb2bc0dbb"
    ),
    "kalshi_predictor.overnight_paper.runtime_owner": (
        "8920989600f7e2d481493336fb5a0d5b8151024c027373e4421bf34f10e6fdf1"
    ),
    "kalshi_predictor.overnight_paper.settlement": (
        "d7c75859f9cc33c6ba9c56abb9b3bd6fd353d4eeba2967a5bdadb4af9a5e1ff8"
    ),
    "kalshi_predictor.overnight_paper.source_health": (
        "2cfcb5a66f158b35d645753748a17fb219e8aa84511fa6cb140319ad5b0872cc"
    ),
    "kalshi_predictor.overnight_paper.store": (
        "a7f91448dec8e3d11a04410daab27d2101135ce443ee8bc3c0b38f31623423d6"
    ),
    "kalshi_predictor.overnight_paper.timing": (
        "4f08a822106e0faf6f7fd6cec05baebe56eea27a5bdbd31928269a3b261d4522"
    ),
    "kalshi_predictor.overnight_paper.watcher": (
        "2db9a1acecaf976efd4188abb2a38ae679f766ffae3df4c40f65bebbf6171a2d"
    ),
    "kalshi_predictor.paper.fees": (
        "80ea757283fe2e7b54bdfb282155d13f8e08a95cdab5e5e9b6cfee7bad4aa2fa"
    ),
    "kalshi_predictor.paper.ledger": (
        "4bf8db1f301c1f3ace3bf7baa535c5f3dd0a08c17474da3761f91837eeee1ff9"
    ),
    "kalshi_predictor.paper.models": (
        "eed1d4e355d1508aa34688079feb2dc8f1b8ee5a98840d53987409ff14f4d305"
    ),
    "kalshi_predictor.paper.simulator": (
        "873217a66565092504c82d19226448f9c43e4ad8ee3b84f4660c82acb199d65d"
    ),
    "kalshi_predictor.position_sizing.repository": (
        "14c72d6a4267b268869abfaec8c25a8212c2dba7d4e0695bf25c8772d25a38dc"
    ),
    "kalshi_predictor.position_sizing.service": (
        "05bc17f1f71090cf0dfa3aae2192f516a47b16c033337852e3476f78ac8ced57"
    ),
    "kalshi_predictor.position_sizing.sizer": (
        "9fd801f7402d35b0fa0704f5c8d2158d3b3a489455e31bb8ae3d1181c94c5914"
    ),
    "kalshi_predictor.signals.attribution": (
        "3212997928610e7c440471a05d367214dda27063287d0c3fb2876fd72f4e5797"
    ),
}


def _repository() -> Path:
    return Path(__file__).resolve().parents[3]


def verify_local_boundary(
    *,
    repository: Path,
    code_sha: str,
    settings: dict[str, Any],
) -> Verification:
    """Verify the reviewed local code is what this process imported.

    This is a code/config boundary check in a trusted Python process, not a sandbox
    against arbitrary in-process monkeypatching. Exact hosted release assurance
    remains a separate activation prerequisite; no submitted attestation is read.
    """
    scope = "REVIEWED_LOCAL_LEDGER_RUNTIME_BOUNDARY"
    try:
        if repository.resolve() != _repository():
            raise ValueError("RUNTIME_REPOSITORY_MISMATCH")
        required_flags = {
            "execution_enabled": False,
            "execution_dry_run": True,
            "execution_kill_switch": True,
            "execution_gateway_mode": "disabled",
            "autopilot_enabled": False,
            "autopilot_dry_run": True,
            "learning_mode": False,
        }
        for name, expected in required_flags.items():
            if settings.get(name) != expected or type(settings.get(name)) is not type(expected):
                raise ValueError("UNSAFE_OR_MISSING_RUNTIME_FLAG:" + name)
        if not AUDITED_BOUNDARY_SHA256:
            raise ValueError("AUDITED_BOUNDARY_MANIFEST_MISSING")

        def git(*args: str) -> bytes:
            return subprocess.check_output(["git", "-C", str(repository), *args], timeout=10)

        if git("rev-parse", "HEAD").decode().strip() != code_sha:
            raise ValueError("RUNTIME_CODE_SHA_MISMATCH")
        if git("status", "--porcelain").strip():
            raise ValueError("CLEAN_RUNTIME_RELEASE_REQUIRED")
        hashes = []
        for name, expected_hash in AUDITED_BOUNDARY_SHA256.items():
            relative = "src/" + name.replace(".", "/") + ".py"
            path = repository / relative
            module = sys.modules.get(name)
            origin = getattr(module, "__file__", None)
            if origin is None or Path(origin).resolve() != path.resolve():
                raise ValueError("RUNTIME_MODULE_ORIGIN_MISMATCH:" + name)
            raw = path.read_bytes().replace(b"\r\n", b"\n")
            if hashlib.sha256(raw).hexdigest() != expected_hash:
                raise ValueError("AUDITED_BOUNDARY_SOURCE_CHANGED:" + name)
            if raw != git("show", code_sha + ":" + relative).replace(b"\r\n", b"\n"):
                raise ValueError("RUNTIME_SOURCE_DIFFERS_FROM_COMMIT:" + name)
            hashes.append(expected_hash)
        for module_name, symbol in (
            ("kalshi_predictor.overnight_paper.activation", "activate_local_paper"),
            ("kalshi_predictor.paper.ledger", "create_paper_order"),
            ("kalshi_predictor.paper.simulator", "simulate_immediate_fill"),
            ("kalshi_predictor.position_sizing.service", "size_paper_decision"),
        ):
            module = sys.modules.get(module_name)
            function = getattr(module, symbol, None)
            path = repository / ("src/" + module_name.replace(".", "/") + ".py")
            origin = getattr(getattr(function, "__code__", None), "co_filename", None)
            if origin is None or Path(origin).resolve() != path.resolve():
                raise ValueError("RUNTIME_FUNCTION_BINDING_MISMATCH:" + symbol)
        activation = sys.modules.get("kalshi_predictor.overnight_paper.activation")
        for target_module, symbol in (
            ("kalshi_predictor.paper.ledger", "create_paper_order"),
            ("kalshi_predictor.paper.simulator", "simulate_immediate_fill"),
            ("kalshi_predictor.position_sizing.service", "size_paper_decision"),
        ):
            if getattr(activation, symbol, None) is not getattr(
                sys.modules.get(target_module), symbol, None
            ):
                raise ValueError("LOCAL_ENTRYPOINT_ALIAS_CHANGED:" + symbol)
        return Verification(True, (), tuple(hashes), scope)
    except (ValueError, TypeError, OSError, subprocess.SubprocessError) as exc:
        return Verification(False, (str(exc),), scope=scope)
