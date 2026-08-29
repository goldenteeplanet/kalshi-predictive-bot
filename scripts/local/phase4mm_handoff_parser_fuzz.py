"""Bounded preflight parsing and deterministic fuzz audit for Phase 4ML packages."""

from __future__ import annotations

import hashlib
import json
import re

from scripts.local.phase4ml_recovery_handoff_package import MAX_PACKAGE_BYTES, verify_package

SCHEMA = "phase4mm.handoff-parser-resource-certification.v1"
MAX_DEPTH = 32
MAX_TOKENS = 200_000
MAX_STRING_BYTES = 1_000_000
MAX_INTEGER_DIGITS = 20
MAX_MINIMIZATION_ATTEMPTS = 64


class DuplicateKeyError(ValueError):
    pass


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateKeyError(key)
        result[key] = value
    return result


def lexical_preflight(package_bytes: object) -> dict[str, object]:
    errors: list[str] = []
    metrics = {
        "input_bytes": len(package_bytes) if isinstance(package_bytes, bytes) else 0,
        "maximum_depth": 0,
        "token_count": 0,
        "maximum_string_bytes": 0,
        "maximum_integer_digits": 0,
        "processing_work_units": 0,
    }
    if not isinstance(package_bytes, bytes):
        errors.append("INPUT_NOT_BYTES")
        package_bytes = b""
    if len(package_bytes) > MAX_PACKAGE_BYTES:
        errors.append("INPUT_BYTE_BOUND_EXCEEDED")
    try:
        text = package_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        text = ""
        errors.append("UTF8_INVALID")
    if not errors:
        depth = 0
        in_string = False
        escaped = False
        string_start = 0
        digit_run = 0
        for index, character in enumerate(text):
            metrics["processing_work_units"] += 1
            if in_string:
                if escaped:
                    escaped = False
                elif character == "\\":
                    escaped = True
                elif character == '"':
                    in_string = False
                    metrics["maximum_string_bytes"] = max(
                        metrics["maximum_string_bytes"],
                        len(text[string_start:index].encode()),
                    )
                continue
            if character == '"':
                in_string = True
                string_start = index + 1
                digit_run = 0
            elif character in "{[":
                depth += 1
                metrics["maximum_depth"] = max(metrics["maximum_depth"], depth)
                metrics["token_count"] += 1
                digit_run = 0
            elif character in "}]":
                depth -= 1
                metrics["token_count"] += 1
                digit_run = 0
                if depth < 0:
                    errors.append("NESTING_UNBALANCED")
                    break
            elif character in ",:":
                metrics["token_count"] += 1
                digit_run = 0
            elif character.isdigit():
                digit_run += 1
                metrics["maximum_integer_digits"] = max(
                    metrics["maximum_integer_digits"], digit_run
                )
            elif not character.isspace() and character not in "-":
                digit_run = 0
        if in_string:
            errors.append("STRING_UNTERMINATED")
        if depth != 0:
            errors.append("NESTING_UNBALANCED")
        if metrics["maximum_depth"] > MAX_DEPTH:
            errors.append("NESTING_DEPTH_BOUND_EXCEEDED")
        if metrics["token_count"] > MAX_TOKENS:
            errors.append("TOKEN_BOUND_EXCEEDED")
        if metrics["maximum_string_bytes"] > MAX_STRING_BYTES:
            errors.append("STRING_BYTE_BOUND_EXCEEDED")
        if metrics["maximum_integer_digits"] > MAX_INTEGER_DIGITS:
            errors.append("INTEGER_DIGIT_BOUND_EXCEEDED")
        if re.search(r"(?<![\w\"])(?:NaN|Infinity|-Infinity)(?![\w\"])", text):
            errors.append("NONFINITE_NUMBER_FORBIDDEN")
    result: dict[str, object] = {
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": sorted(set(errors)),
        "metrics": metrics,
        "bounds": {
            "max_input_bytes": MAX_PACKAGE_BYTES,
            "max_depth": MAX_DEPTH,
            "max_tokens": MAX_TOKENS,
            "max_string_bytes": MAX_STRING_BYTES,
            "max_integer_digits": MAX_INTEGER_DIGITS,
        },
    }
    result["preflight_sha256"] = _digest(result)
    return result


def bounded_verify(
    package_bytes: object,
    *,
    expected_audience: str,
    evaluated_at: str,
) -> dict[str, object]:
    preflight = lexical_preflight(package_bytes)
    errors = list(preflight["errors"])
    semantic: dict[str, object] = {"verdict": "REFUSE", "errors": ["NOT_RUN"]}
    if not errors and isinstance(package_bytes, bytes):
        try:
            json.loads(
                package_bytes,
                object_pairs_hook=_pairs,
                parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
            )
        except DuplicateKeyError:
            errors.append("DUPLICATE_JSON_KEY")
        except (ValueError, TypeError, RecursionError, MemoryError, OverflowError):
            errors.append("JSON_DECODE_REFUSED")
        if not errors:
            try:
                semantic = verify_package(
                    package_bytes,
                    expected_audience=expected_audience,
                    evaluated_at=evaluated_at,
                )
            except (ValueError, TypeError, RecursionError, MemoryError, OverflowError):
                errors.append("SEMANTIC_VERIFIER_EXCEPTION_CONTAINED")
            else:
                errors.extend(str(error) for error in semantic.get("errors", []))
    errors = sorted(set(errors))
    result: dict[str, object] = {
        "schema": SCHEMA,
        "verdict": "PASS" if not errors and semantic.get("verdict") == "PASS" else "REFUSE",
        "errors": errors,
        "preflight": preflight,
        "semantic_verification_sha256": semantic.get("verification_sha256"),
        "resource_envelope": {
            "deterministic_work_units": preflight["metrics"]["processing_work_units"],
            "maximum_work_units": MAX_PACKAGE_BYTES,
            "within_envelope": preflight["metrics"]["processing_work_units"] <= MAX_PACKAGE_BYTES,
            "wall_clock_not_security_input": True,
        },
        "safety": {
            "offline": True,
            "bounded": True,
            "read_only": True,
            "package_write": False,
            "extraction": False,
            "network_access": False,
            "runtime_write": False,
            "wsl_control": False,
            "service_control": False,
            "order_capability": False,
        },
    }
    result["verification_sha256"] = _digest(result)
    return result


def minimize_failure(
    payload: bytes,
    *,
    expected_audience: str,
    evaluated_at: str,
) -> dict[str, object]:
    original = payload
    baseline = bounded_verify(
        original,
        expected_audience=expected_audience,
        evaluated_at=evaluated_at,
    )
    target = baseline["errors"][0] if baseline["errors"] else "NO_FAILURE"
    minimized = original
    attempts = 0
    granularity = 2
    while len(minimized) > 1 and attempts < MAX_MINIMIZATION_ATTEMPTS:
        chunk = max(1, len(minimized) // granularity)
        reduced = False
        for start in range(0, len(minimized), chunk):
            if attempts >= MAX_MINIMIZATION_ATTEMPTS:
                break
            candidate = minimized[:start] + minimized[start + chunk :]
            attempts += 1
            result = bounded_verify(
                candidate,
                expected_audience=expected_audience,
                evaluated_at=evaluated_at,
            )
            if result["errors"] and result["errors"][0] == target:
                minimized = candidate
                reduced = True
                granularity = max(2, granularity - 1)
                break
        if not reduced:
            if granularity >= len(minimized):
                break
            granularity = min(len(minimized), granularity * 2)
    result = {
        "failure_class": target,
        "original_bytes": len(original),
        "minimized_bytes": len(minimized),
        "attempt_count": attempts,
        "original_sha256": hashlib.sha256(original).hexdigest(),
        "minimized_sha256": hashlib.sha256(minimized).hexdigest(),
    }
    result["minimization_sha256"] = _digest(result)
    return result


def _recanonicalize(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def fuzz_corpus(valid_package: bytes) -> list[dict[str, object]]:
    cases: list[tuple[str, bytes, str]] = [
        ("invalid-utf8", b"\xff\xfe", "utf8"),
        ("malformed-json", b'{"x":', "json"),
        ("deep-nesting", ("[" * 40 + "]" * 40).encode(), "depth"),
        ("oversized-input", b"x" * (MAX_PACKAGE_BYTES + 1), "input_bytes"),
        ("oversized-string", (b'{"x":"' + b"a" * (MAX_STRING_BYTES + 1) + b'"}'), "string"),
        ("huge-integer", b'{"x":123456789012345678901}', "integer"),
        ("duplicate-key", b'{"x":1,"x":2}', "duplicate_key"),
        ("nan", b'{"x":NaN}', "numeric_edge"),
        ("infinity", b'{"x":Infinity}', "numeric_edge"),
    ]
    for boundary in (1, 2, 8, len(valid_package) // 2, len(valid_package) - 1):
        cases.append((f"truncated-{boundary}", valid_package[:boundary], "truncation"))
    mutations: list[tuple[str, callable, str]] = [
        (
            "descriptor-count-skew",
            lambda value: value["manifest"].update(component_count=99),
            "count_skew",
        ),
        (
            "expansion-claim",
            lambda value: value["manifest"].update(total_component_bytes=MAX_PACKAGE_BYTES * 100),
            "expansion_claim",
        ),
        (
            "dot-path",
            lambda value: value["components"][0].update(path="./handoff.json"),
            "path_normalization",
        ),
        (
            "backslash-path",
            lambda value: value["components"][0].update(path="recovery\\handoff.json"),
            "path_normalization",
        ),
        (
            "unicode-confusable-path",
            lambda value: value["components"][0].update(path="recovery-handoff/..\uff0fescape"),
            "unicode_confusable",
        ),
        (
            "hash-substitution",
            lambda value: value["manifest"]["component_descriptors"][0].update(sha256="f" * 64),
            "hash_substitution",
        ),
        (
            "field-type-confusion",
            lambda value: value["manifest"].update(component_count="7"),
            "type_confusion",
        ),
        (
            "component-array-confusion",
            lambda value: value.update(components={}),
            "type_confusion",
        ),
    ]
    for name, mutate, surface in mutations:
        candidate = json.loads(valid_package)
        mutate(candidate)
        cases.append((name, _recanonicalize(candidate), surface))
    return [
        {"case_id": name, "payload": payload, "surface": surface}
        for name, payload, surface in cases
    ]


def run_fuzz_audit(
    valid_package: bytes,
    *,
    expected_audience: str,
    evaluated_at: str,
) -> dict[str, object]:
    baseline = bounded_verify(
        valid_package,
        expected_audience=expected_audience,
        evaluated_at=evaluated_at,
    )
    cases = fuzz_corpus(valid_package)
    records: list[dict[str, object]] = []
    for case in cases:
        result = bounded_verify(
            case["payload"],
            expected_audience=expected_audience,
            evaluated_at=evaluated_at,
        )
        records.append(
            {
                "case_id": case["case_id"],
                "surface": case["surface"],
                "refused": result["verdict"] == "REFUSE",
                "errors": result["errors"],
                "payload_sha256": hashlib.sha256(case["payload"]).hexdigest(),
                "work_units": result["resource_envelope"]["deterministic_work_units"],
            }
        )
    representatives = [
        cases[0],
        cases[1],
        next(row for row in cases if row["case_id"] == "duplicate-key"),
    ]
    minimizations = [
        minimize_failure(
            row["payload"],
            expected_audience=expected_audience,
            evaluated_at=evaluated_at,
        )
        for row in representatives
    ]
    errors: list[str] = []
    if baseline["verdict"] != "PASS":
        errors.append("VALID_BASELINE_REFUSED")
    if not all(row["refused"] and row["errors"] for row in records):
        errors.append("FUZZ_CASE_NOT_SAFELY_REFUSED")
    coverage = {
        surface: sum(row["surface"] == surface for row in records)
        for surface in sorted({str(row["surface"]) for row in records})
    }
    result: dict[str, object] = {
        "schema": SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": errors,
        "case_count": len(records),
        "refused_count": sum(row["refused"] for row in records),
        "surface_coverage": coverage,
        "corpus_sha256": _digest(records),
        "minimization_sha256": _digest(minimizations),
        "resource_envelope_sha256": _digest(baseline["resource_envelope"]),
        "records": records,
        "minimizations": minimizations,
        "safety": baseline["safety"],
    }
    result["audit_sha256"] = _digest(result)
    return result
