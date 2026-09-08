"""Deterministic provenance and reviewed local-boundary checks, not attestations.

Provenance completeness does not establish calibrated skill or settlement authority.
Callers must separately close those semantic gates before considering activation.
"""

from __future__ import annotations

import hashlib
import json
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
            ("model", ("training_cutoff", "created_at", "available_at")),
        ):
            times = [aware(rows[role][key]) for key in keys]
            if any(value > at for value in times) or times != sorted(times):
                raise ValueError("FUTURE_OR_INCONSISTENT_ARTIFACT_VISIBILITY:" + role)
        for source in sources:
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
            if not source.get("url") or not source.get("body"):
                raise ValueError("ORIGINAL_SOURCE_PAYLOAD_REQUIRED")
        if not model.get("code_sha256") or not model.get("training_dataset_hashes"):
            raise ValueError("MODEL_TRAINING_LINEAGE_REQUIRED")
        if decision.get("model_code_sha256") != model["code_sha256"]:
            raise ValueError("MODEL_CODE_HASH_MISMATCH")
        if not model_code or hashlib.sha256(model_code).hexdigest() != model["code_sha256"]:
            raise ValueError("ORIGINAL_MODEL_CODE_REQUIRED")
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
    "kalshi_predictor.overnight_paper.activation": (
        "6342b329675246661a31beeba0a8d167ca9d6197594513bf4834397053f050d5"
    ),
    "kalshi_predictor.overnight_paper.boundary": (
        "7e8647e9cf73874dbcc9df4c7b324b86fbaaca21a4f9981c3c71b9a85227e6e8"
    ),
    "kalshi_predictor.overnight_paper.qualification": (
        "e1a5e802baf49723fe71ad15f8f5dd23f23714e0e75627ffcbbc76c689de6ee4"
    ),
    "kalshi_predictor.overnight_paper.store": (
        "a7f91448dec8e3d11a04410daab27d2101135ce443ee8bc3c0b38f31623423d6"
    ),
    "kalshi_predictor.paper.ledger": (
        "3364db4a75ab625b6f6101c103e00ab099784eea1985e381edec058074243509"
    ),
    "kalshi_predictor.paper.simulator": (
        "6696993adcf16db25ea3a2352d736d1e311f05ad63135568ed357f2609cb4282"
    ),
    "kalshi_predictor.paper.models": (
        "eed1d4e355d1508aa34688079feb2dc8f1b8ee5a98840d53987409ff14f4d305"
    ),
    "kalshi_predictor.position_sizing.service": (
        "05bc17f1f71090cf0dfa3aae2192f516a47b16c033337852e3476f78ac8ced57"
    ),
    "kalshi_predictor.position_sizing.sizer": (
        "9fd801f7402d35b0fa0704f5c8d2158d3b3a489455e31bb8ae3d1181c94c5914"
    ),
    "kalshi_predictor.position_sizing.repository": (
        "14c72d6a4267b268869abfaec8c25a8212c2dba7d4e0695bf25c8772d25a38dc"
    ),
    "kalshi_predictor.advanced_risk.service": (
        "109cd3737149e21197a60bf1d47adfad0c4fa432d20b5e21d1611fd17ed1ea53"
    ),
    "kalshi_predictor.advanced_risk.engine": (
        "a250e788b008f3c6ccd967b418d4c834be9fbf26932190d219349f996f96a2ce"
    ),
    "kalshi_predictor.advanced_risk.repository": (
        "7a6736e9a419cb65acd04f0aa499bec17ec620d2d2e50f33fbe675a1a9f5d163"
    ),
    "kalshi_predictor.data.repositories": (
        "af405031cec92008cbaab0d381096d4ac456bc60cc46fb33692838ef6abdf2db"
    ),
    "kalshi_predictor.data.schema": (
        "c614333a03dd021f7d7685c7efe0e25c9931bcc922b8438603896dc1900e0553"
    ),
    "kalshi_predictor.signals.attribution": (
        "3212997928610e7c440471a05d367214dda27063287d0c3fb2876fd72f4e5797"
    ),
    "kalshi_predictor.config": ("dbcb32e93e44ed565bbe747a9cd753f25269cd0d16d05ed86537c3631698e2a7"),
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
