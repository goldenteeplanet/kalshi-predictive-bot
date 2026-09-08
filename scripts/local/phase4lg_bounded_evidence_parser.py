"""Resource-bounded, ambiguity-refusing JSON parser for phase evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import PurePosixPath
from typing import Any

SCHEMA = "phase4lg.bounded-evidence-parser-audit.v1"
FORBIDDEN_EXTENSION_CAPABILITIES = {
    "artifact_publication",
    "database_write",
    "exchange_access",
    "network_access",
    "order_capability",
    "service_control",
    "trading_capability",
    "writer_lock",
}


@dataclass(frozen=True)
class Limits:
    document_bytes: int = 65_536
    nesting_depth: int = 32
    collection_items: int = 1_000
    string_characters: int = 4_096
    scalar_count: int = 5_000
    receipt_count: int = 256
    path_count: int = 512
    extension_count: int = 64
    numeric_token_characters: int = 40
    numeric_absolute_max: Decimal = Decimal("1e18")


class EvidenceParseError(ValueError):
    pass


def _scan_depth(text: str, maximum: int) -> None:
    depth = 0
    in_string = False
    escaped = False
    for character in text:
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character in "[{":
            depth += 1
            if depth > maximum:
                raise EvidenceParseError("NESTING_DEPTH_EXCEEDED")
        elif character in "]}":
            depth -= 1
            if depth < 0:
                raise EvidenceParseError("MALFORMED_STRUCTURE")


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise EvidenceParseError(f"DUPLICATE_KEY:{key}")
        result[key] = value
    return result


def _integer(token: str, limits: Limits) -> int:
    if len(token) > limits.numeric_token_characters:
        raise EvidenceParseError("NUMERIC_TOKEN_TOO_LONG")
    value = int(token)
    if abs(value) > limits.numeric_absolute_max:
        raise EvidenceParseError("NUMERIC_RANGE_EXCEEDED")
    return value


def _floating(token: str, limits: Limits) -> Decimal:
    if len(token) > limits.numeric_token_characters:
        raise EvidenceParseError("NUMERIC_TOKEN_TOO_LONG")
    try:
        value = Decimal(token)
    except InvalidOperation as exc:
        raise EvidenceParseError("INVALID_NUMBER") from exc
    if not value.is_finite() or abs(value) > limits.numeric_absolute_max:
        raise EvidenceParseError("NUMERIC_RANGE_EXCEEDED")
    return value


def _reject_constant(token: str) -> None:
    raise EvidenceParseError(f"NON_FINITE_NUMBER:{token}")


def _safe_string(value: str) -> bool:
    return all(not unicodedata.category(character).startswith("C") for character in value)


def _safe_path(value: str) -> bool:
    if not _safe_string(value) or value != unicodedata.normalize("NFC", value) or "\\" in value:
        return False
    path = PurePosixPath(value)
    raw_parts = value.split("/")
    return bool(value) and not path.is_absolute() and "." not in raw_parts and ".." not in raw_parts


def _audit_extension_capabilities(value: object, inside_extensions: bool = False) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            nested_extensions = inside_extensions or key == "extensions"
            normalized = str(key).lower().replace("-", "_")
            disabled = child is False or child is None or child in ("disabled", "none")
            if (
                nested_extensions
                and normalized in FORBIDDEN_EXTENSION_CAPABILITIES
                and not disabled
            ):
                raise EvidenceParseError("FORBIDDEN_EXTENSION_CAPABILITY")
            _audit_extension_capabilities(child, nested_extensions)
    elif isinstance(value, list):
        for child in value:
            _audit_extension_capabilities(child, inside_extensions)


def _audit(value: object, limits: Limits) -> dict[str, int]:
    stats = {"scalars": 0, "collections": 0, "maximum_depth": 0}

    def visit(node: object, depth: int, field: str | None = None) -> None:
        stats["maximum_depth"] = max(stats["maximum_depth"], depth)
        if isinstance(node, dict):
            stats["collections"] += 1
            if len(node) > limits.collection_items:
                raise EvidenceParseError("COLLECTION_SIZE_EXCEEDED")
            if field == "extensions" and len(node) > limits.extension_count:
                raise EvidenceParseError("EXTENSION_COUNT_EXCEEDED")
            for key, child in node.items():
                if len(key) > limits.string_characters or not _safe_string(key):
                    raise EvidenceParseError("UNSAFE_OR_OVERSIZED_STRING")
                visit(child, depth + 1, key)
        elif isinstance(node, list):
            stats["collections"] += 1
            if len(node) > limits.collection_items:
                raise EvidenceParseError("COLLECTION_SIZE_EXCEEDED")
            if field in {"receipts", "entries"} and len(node) > limits.receipt_count:
                raise EvidenceParseError("RECEIPT_COUNT_EXCEEDED")
            if field in {"owned_paths", "committed_paths", "staged_paths"}:
                if len(node) > limits.path_count:
                    raise EvidenceParseError("PATH_COUNT_EXCEEDED")
                if any(not isinstance(path, str) or not _safe_path(path) for path in node):
                    raise EvidenceParseError("UNSAFE_PATH")
            for child in node:
                visit(child, depth + 1, field)
        else:
            stats["scalars"] += 1
            if stats["scalars"] > limits.scalar_count:
                raise EvidenceParseError("SCALAR_COUNT_EXCEEDED")
            if isinstance(node, str):
                if len(node) > limits.string_characters or not _safe_string(node):
                    raise EvidenceParseError("UNSAFE_OR_OVERSIZED_STRING")
            elif isinstance(node, float) and not math.isfinite(node):
                raise EvidenceParseError("NON_FINITE_NUMBER")

    visit(value, 1)
    return stats


def parse_evidence(
    document: bytes | str, limits: Limits | None = None
) -> tuple[object | None, dict[str, object]]:
    limits = limits or Limits()
    errors: list[str] = []
    raw = document.encode("utf-8") if isinstance(document, str) else document
    if len(raw) > limits.document_bytes:
        errors.append("DOCUMENT_SIZE_EXCEEDED")
        text = ""
    else:
        try:
            text = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            text = ""
            errors.append("MALFORMED_UTF8")
    value: object | None = None
    stats = {"scalars": 0, "collections": 0, "maximum_depth": 0}
    if not errors:
        try:
            _scan_depth(text, limits.nesting_depth)
            value = json.loads(
                text,
                object_pairs_hook=_pairs,
                parse_int=lambda token: _integer(token, limits),
                parse_float=lambda token: _floating(token, limits),
                parse_constant=_reject_constant,
            )
            _audit_extension_capabilities(value)
            stats = _audit(value, limits)
        except EvidenceParseError as exc:
            errors.append(str(exc).splitlines()[0])
            value = None
        except json.JSONDecodeError:
            errors.append("MALFORMED_JSON")
            value = None
        except RecursionError:
            errors.append("RECURSION_LIMIT")
            value = None
    errors = sorted(set(errors))
    result: dict[str, object] = {
        "schema": SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "document_bytes": len(raw),
        "stats": stats,
        "errors": errors,
        "safety": {
            "bounded": True,
            "read_only": True,
            "database_access": False,
            "service_control": False,
            "order_capability": False,
        },
    }
    canonical = json.dumps(result, sort_keys=True, separators=(",", ":")).encode()
    result["audit_sha256"] = hashlib.sha256(canonical).hexdigest()
    return value, result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("document")
    args = parser.parse_args()
    with open(args.document, "rb") as stream:
        document = stream.read(Limits().document_bytes + 1)
    _, result = parse_evidence(document)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["verdict"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
