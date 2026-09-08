"""Cross-platform canonicalization and portable adversarial replay proof."""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from scripts.local.phase4nj_reproducibility_bundle import create_bundle, verify_bundle

SCHEMA = "phase4nk.portable-replay.v1"
CANONICALIZATION_VERSION = "phase4nk-canonical-json-v1"
_TIMESTAMP_PREFIX = re.compile(r"^\d{4}-\d{2}-\d{2}T")
_DECIMAL = re.compile(r"^-?(?:0|[1-9]\d*)(?:\.\d+)?$")
_LOCALE_NUMBER = re.compile(r"^-?\d{1,3}(?:[.,]\d{3})*[,.]\d+$")
_WINDOWS_PATH = re.compile(r"^[A-Za-z]:[\\/]")
_PLATFORM_KEYS = {"platform", "os", "hostname", "absolute_path", "cwd"}


class CanonicalizationError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _decimal(value: Decimal) -> str:
    if not value.is_finite():
        raise CanonicalizationError("NON_FINITE_NUMBER")
    text = format(value.normalize(), "f")
    return "0" if Decimal(text) == 0 else text


def _string(value: str) -> str:
    value = unicodedata.normalize("NFC", value.replace("\r\n", "\n").replace("\r", "\n"))
    if _WINDOWS_PATH.match(value) or value.startswith(("/home/", "/Users/", "/mnt/")):
        raise CanonicalizationError("PATH_LEAKAGE_DETECTED")
    if _TIMESTAMP_PREFIX.match(value):
        if not (value.endswith("Z") or re.search(r"[+-]\d{2}:\d{2}$", value)):
            raise CanonicalizationError("TIMEZONE_AMBIGUOUS")
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise CanonicalizationError("TIMESTAMP_INVALID") from exc
        if parsed.tzinfo is None:
            raise CanonicalizationError("TIMEZONE_AMBIGUOUS")
        utc = parsed.astimezone(UTC)
        rendered = utc.isoformat(timespec="microseconds").replace("+00:00", "Z")
        return rendered.replace(".000000Z", "Z")
    if "," in value and _LOCALE_NUMBER.match(value):
        raise CanonicalizationError("LOCALE_DEPENDENT_NUMBER")
    if _DECIMAL.match(value):
        try:
            return _decimal(Decimal(value))
        except InvalidOperation as exc:
            raise CanonicalizationError("NUMBER_INVALID") from exc
    return value


def _normalize(value: object) -> object:
    if value is None or isinstance(value, bool) or isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CanonicalizationError("NON_FINITE_NUMBER")
        return _decimal(Decimal(str(value)))
    if isinstance(value, Decimal):
        return _decimal(value)
    if isinstance(value, bytes):
        try:
            return _string(value.decode("utf-8", errors="strict"))
        except UnicodeDecodeError as exc:
            raise CanonicalizationError("UNSUPPORTED_ENCODING") from exc
    if isinstance(value, str):
        return _string(value)
    if isinstance(value, list):
        return [_normalize(item) for item in value]
    if isinstance(value, dict):
        output: dict[str, object] = {}
        for raw_key, item in value.items():
            if not isinstance(raw_key, str):
                raise CanonicalizationError("NON_STRING_KEY")
            key = unicodedata.normalize("NFC", raw_key)
            if key in output:
                raise CanonicalizationError("DUPLICATE_NORMALIZED_KEY")
            if key.casefold() in _PLATFORM_KEYS:
                raise CanonicalizationError("PLATFORM_METADATA_PRESENT")
            output[key] = _normalize(item)
        return output
    raise CanonicalizationError("TYPE_UNSUPPORTED")


def canonicalize_artifact(
    value: object, *, version: str = CANONICALIZATION_VERSION
) -> dict[str, object]:
    errors: list[str] = []
    normalized = None
    if version != CANONICALIZATION_VERSION:
        errors.append("CANONICALIZATION_VERSION_DRIFT")
    else:
        try:
            normalized = _normalize(value)
        except CanonicalizationError as exc:
            errors.append(exc.code)
    canonical_bytes = (
        json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
            "utf-8"
        )
        if not errors
        else None
    )
    result = {
        "schema": SCHEMA,
        "version": version,
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": errors,
        "normalized": normalized,
        "canonical_hex": canonical_bytes.hex() if canonical_bytes is not None else None,
        "canonical_sha256": (
            hashlib.sha256(canonical_bytes).hexdigest() if canonical_bytes is not None else None
        ),
        "safety": _safety(),
    }
    return result


def prove_portable_replay(left: object, right: object, *, seed: int = 0) -> dict[str, object]:
    left_result = canonicalize_artifact(left)
    right_result = canonicalize_artifact(right)
    errors = sorted(set(left_result["errors"] + right_result["errors"]))
    if not errors and left_result["canonical_hex"] != right_result["canonical_hex"]:
        errors.append("CANONICAL_BYTES_MISMATCH")
    left_bundle = right_bundle = left_verify = right_verify = None
    if not errors:
        left_bundle = create_bundle(left_result["normalized"], seed=seed)
        right_bundle = create_bundle(right_result["normalized"], seed=seed)
        left_verify, right_verify = verify_bundle(left_bundle), verify_bundle(right_bundle)
        if left_bundle["bundle_sha256"] != right_bundle["bundle_sha256"]:
            errors.append("BUNDLE_HASH_MISMATCH")
        if left_verify["replay_outputs_sha256"] != right_verify["replay_outputs_sha256"]:
            errors.append("REPLAY_OUTPUT_MISMATCH")
        if left_verify["verdict"] != right_verify["verdict"]:
            errors.append("REPLAY_VERDICT_MISMATCH")
    result = {
        "schema": SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": errors,
        "canonical_sha256": left_result["canonical_sha256"] if not errors else None,
        "bundle_sha256": left_bundle["bundle_sha256"] if left_bundle and not errors else None,
        "replay_outputs_sha256": (
            left_verify["replay_outputs_sha256"] if left_verify and not errors else None
        ),
        "safety": _safety(),
    }
    result["proof_sha256"] = hashlib.sha256(
        json.dumps(result, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return result


def _safety() -> dict[str, bool]:
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
