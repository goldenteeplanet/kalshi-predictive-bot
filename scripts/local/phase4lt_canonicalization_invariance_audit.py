"""Audit platform, text, numeric, and timestamp canonicalization invariants."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import unicodedata
from datetime import UTC, datetime

SCHEMA = "phase4lt.canonicalization-invariance-audit.v1"
RFC3339 = re.compile(
    r"\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.(\d{1,6}))?(?:Z|[+-]\d{2}:\d{2})\Z"
)
WINDOWS_PATH = re.compile(r"(?:\A[A-Za-z]:\\|\\\\[^\\]+\\)")
POSIX_RUNTIME_PATH = re.compile(r"(?:\A|\s)/(?:mnt|home|tmp|var|etc)/")
LOCALE_NUMBER = re.compile(r"\A[+-]?\d{1,3}(?:[.,]\d{3})*[,.]\d+\Z")
MIN_INT64 = -(2**63)
MAX_INT64 = 2**63 - 1


class CanonicalizationError(ValueError):
    def __init__(self, signature: str) -> None:
        super().__init__(signature)
        self.signature = signature


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _timestamp(value: str) -> str:
    match = RFC3339.fullmatch(value)
    if match is None:
        if "[" in value or "]" in value:
            raise CanonicalizationError("TIMESTAMP_ZONE_AMBIGUOUS_OR_NONEXISTENT")
        raise CanonicalizationError("TIMESTAMP_NONCANONICAL")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CanonicalizationError("TIMESTAMP_INVALID") from exc
    utc = parsed.astimezone(UTC)
    if utc.microsecond:
        return utc.isoformat(timespec="microseconds").replace("+00:00", "Z")
    return utc.isoformat(timespec="seconds").replace("+00:00", "Z")


def canonicalize(value: object, key: str = "") -> object:
    if value is None or type(value) is bool:
        if key.endswith("_enabled") and type(value) is not bool:
            raise CanonicalizationError("BOOLEAN_TYPE_INVALID")
        return value
    if type(value) is int:
        if not MIN_INT64 <= value <= MAX_INT64:
            raise CanonicalizationError("INTEGER_RANGE_INVALID")
        return value
    if isinstance(value, float):
        raise CanonicalizationError("FLOAT_PRECISION_REJECTED")
    if isinstance(value, str):
        normalized = unicodedata.normalize("NFC", value.replace("\r\n", "\n").replace("\r", "\n"))
        if WINDOWS_PATH.search(normalized) or POSIX_RUNTIME_PATH.search(normalized):
            raise CanonicalizationError("PLATFORM_PATH_REJECTED")
        if key.endswith("_at"):
            return _timestamp(normalized)
        if key.endswith("_count") and LOCALE_NUMBER.fullmatch(normalized):
            raise CanonicalizationError("LOCALE_NUMERIC_STRING_REJECTED")
        if key.endswith("_enabled"):
            raise CanonicalizationError("BOOLEAN_TYPE_INVALID")
        return normalized
    if isinstance(value, list):
        items = [canonicalize(item, key) for item in value]
        if all(isinstance(item, dict) and isinstance(item.get("name"), str) for item in items):
            names = [item["name"] for item in items]
            if len(names) != len(set(names)):
                raise CanonicalizationError("NAMED_LIST_DUPLICATE")
            return sorted(items, key=lambda item: item["name"])
        return items
    if isinstance(value, dict):
        result: dict[str, object] = {}
        for raw_key, item in value.items():
            if not isinstance(raw_key, str):
                raise CanonicalizationError("KEY_TYPE_INVALID")
            normalized_key = unicodedata.normalize("NFC", raw_key)
            if normalized_key in result:
                raise CanonicalizationError("NORMALIZED_KEY_COLLISION")
            result[normalized_key] = canonicalize(item, normalized_key)
        return {item_key: result[item_key] for item_key in sorted(result)}
    raise CanonicalizationError("TYPE_UNSUPPORTED")


def canonical_hash(value: object) -> str:
    return _digest(canonicalize(value))


def _outcome(
    name: str, value: object, expected: str, baseline_hash: str | None = None
) -> dict[str, object]:
    try:
        digest = canonical_hash(value)
        observed = "PASS"
        signature = "PASS"
        equivalent = baseline_hash is None or digest == baseline_hash
    except CanonicalizationError as exc:
        digest = None
        observed = "REFUSE"
        signature = exc.signature
        equivalent = False
    return {
        "name": name,
        "expected": expected,
        "observed": observed,
        "signature": signature,
        "canonical_sha256": digest,
        "equivalent_to_baseline": equivalent,
    }


def run_audit() -> dict[str, object]:
    baseline = {
        "schema": "phase4lr.alert-evidence-export-manifest.v1",
        "observed_at": "2026-08-28T20:00:00Z",
        "artifact_count": 2,
        "paper_enabled": False,
        "note": "café\nline two",
        "artifacts": [{"name": "alpha", "value": 1}, {"name": "beta", "value": 2}],
    }
    baseline_hash = canonical_hash(baseline)
    invariants = [
        _outcome("baseline", baseline, "PASS", baseline_hash),
        _outcome(
            "dictionary_and_artifact_reordering",
            {
                "artifacts": list(reversed(baseline["artifacts"])),
                "note": baseline["note"],
                "paper_enabled": False,
                "artifact_count": 2,
                "observed_at": baseline["observed_at"],
                "schema": baseline["schema"],
            },
            "PASS",
            baseline_hash,
        ),
        _outcome(
            "crlf_equivalence", {**baseline, "note": "café\r\nline two"}, "PASS", baseline_hash
        ),
        _outcome(
            "unicode_nfc_equivalence",
            {**baseline, "note": "cafe\u0301\nline two"},
            "PASS",
            baseline_hash,
        ),
        _outcome(
            "timezone_offset_equivalence",
            {**baseline, "observed_at": "2026-08-28T15:00:00-05:00"},
            "PASS",
            baseline_hash,
        ),
    ]
    refusals = [
        _outcome("windows_path", {**baseline, "note": "C:\\Users\\user\\file"}, "REFUSE"),
        _outcome("wsl_path", {**baseline, "note": "/mnt/c/Users/user/file"}, "REFUSE"),
        _outcome("naive_timestamp", {**baseline, "observed_at": "2026-08-28T20:00:00"}, "REFUSE"),
        _outcome(
            "ambiguous_dst_timestamp",
            {**baseline, "observed_at": "2026-11-01T01:30:00[America/Chicago]"},
            "REFUSE",
        ),
        _outcome(
            "nonexistent_dst_timestamp",
            {**baseline, "observed_at": "2026-03-08T02:30:00[America/Chicago]"},
            "REFUSE",
        ),
        _outcome("precision_losing_float", {**baseline, "artifact_count": 2.0}, "REFUSE"),
        _outcome("integer_overflow", {**baseline, "artifact_count": 2**63}, "REFUSE"),
        _outcome("locale_numeric", {**baseline, "artifact_count": "2,0"}, "REFUSE"),
        _outcome("string_boolean", {**baseline, "paper_enabled": "false"}, "REFUSE"),
        _outcome(
            "excess_timestamp_precision",
            {**baseline, "observed_at": "2026-08-28T20:00:00.1234567Z"},
            "REFUSE",
        ),
    ]
    errors: list[str] = []
    for row in invariants:
        if row["observed"] != "PASS" or row["equivalent_to_baseline"] is not True:
            errors.append(f"INVARIANCE_FAILED:{row['name']}")
    for row in refusals:
        if row["observed"] != "REFUSE":
            errors.append(f"UNSAFE_CASE_ACCEPTED:{row['name']}")
    path_signatures = {
        row["signature"] for row in refusals if row["name"] in {"windows_path", "wsl_path"}
    }
    if path_signatures != {"PLATFORM_PATH_REJECTED"}:
        errors.append("PATH_REJECTION_SIGNATURE_DIVERGED")
    result: dict[str, object] = {
        "schema": SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": errors,
        "baseline_canonical_sha256": baseline_hash,
        "invariants": invariants,
        "refusals": refusals,
        "coverage": {
            "invariant_case_count": len(invariants),
            "refusal_case_count": len(refusals),
            "platform_path_signature_equal": len(path_signatures) == 1,
            "locale_independent_numbers": True,
            "timezone_normalization": "UTC_RFC3339",
            "unicode_normalization": "NFC",
            "line_endings": "LF",
        },
        "safety": {
            "offline_only": True,
            "file_mutation": False,
            "network_access": False,
            "database_access": False,
            "service_control": False,
            "wsl_control": False,
            "notification_delivery": False,
            "order_capability": False,
        },
    }
    result["audit_sha256"] = _digest(result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.parse_args()
    result = run_audit()
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["verdict"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
