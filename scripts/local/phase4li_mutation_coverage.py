"""Mutation coverage and blind-spot audit for the bounded evidence parser."""

from __future__ import annotations

import argparse
import ast
import hashlib
import inspect
import json
from collections.abc import Callable
from decimal import Decimal

import scripts.local.phase4lg_bounded_evidence_parser as parser_module
from scripts.local.phase4lg_bounded_evidence_parser import Limits, parse_evidence
from scripts.local.phase4lh_fuzz_corpus import MUTATIONS, generate_corpus, minimize_failure

SCHEMA = "phase4li.mutation-coverage-blind-spot-audit.v1"
JUSTIFIED_UNREACHABLE = {
    "INVALID_NUMBER": "json decoder invokes parse_float only for grammar-valid numeric tokens",
    "NON_FINITE_FLOAT_POST_PARSE": (
        "parse_float returns bounded Decimal and parse_constant rejects non-finite tokens"
    ),
    "RECURSION_LIMIT": "the lower lexical nesting-depth bound refuses input before JSON recursion",
}


def _probe(document: bytes | str, limits: Limits, expected: str) -> tuple[bool, str]:
    _, result = parse_evidence(document, limits)
    errors = [str(error) for error in result["errors"]]
    matched = any(error == expected or error.startswith(f"{expected}:") for error in errors)
    return matched, errors[0] if errors else "PASS"


def _probes() -> dict[str, Callable[[], tuple[bytes | str, Limits]]]:
    return {
        "DOCUMENT_SIZE_EXCEEDED": lambda: (b"00", Limits(document_bytes=1)),
        "MALFORMED_UTF8": lambda: (b'"\xff"', Limits()),
        "NESTING_DEPTH_EXCEEDED": lambda: ("[[0]]", Limits(nesting_depth=1)),
        "MALFORMED_STRUCTURE": lambda: ("}", Limits()),
        "DUPLICATE_KEY": lambda: ('{"a":1,"a":2}', Limits()),
        "NUMERIC_TOKEN_TOO_LONG": lambda: ("100", Limits(numeric_token_characters=2)),
        "NUMERIC_RANGE_EXCEEDED": lambda: ("11", Limits(numeric_absolute_max=Decimal(10))),
        "NON_FINITE_NUMBER": lambda: ("NaN", Limits()),
        "FORBIDDEN_EXTENSION_CAPABILITY": lambda: (
            '{"extensions":{"vendor.capabilities":{"order_capability":true}}}',
            Limits(),
        ),
        "COLLECTION_SIZE_EXCEEDED": lambda: ("[0,1]", Limits(collection_items=1)),
        "EXTENSION_COUNT_EXCEEDED": lambda: (
            '{"extensions":{"a.x":1,"b.x":2}}',
            Limits(extension_count=1),
        ),
        "RECEIPT_COUNT_EXCEEDED": lambda: (
            '{"receipts":[{},{}]}',
            Limits(receipt_count=1),
        ),
        "PATH_COUNT_EXCEEDED": lambda: (
            '{"owned_paths":["a","b"]}',
            Limits(path_count=1),
        ),
        "UNSAFE_PATH": lambda: ('{"owned_paths":["../escape"]}', Limits()),
        "SCALAR_COUNT_EXCEEDED": lambda: ("[0,1]", Limits(scalar_count=1)),
        "UNSAFE_OR_OVERSIZED_STRING": lambda: ('"abcd"', Limits(string_characters=3)),
        "MALFORMED_JSON": lambda: ('{"a":1} trailing', Limits()),
    }


def _first_literal(call: ast.Call) -> str | None:
    if not call.args:
        return None
    value = call.args[0]
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        return value.value.split(":", 1)[0]
    if isinstance(value, ast.JoinedStr) and value.values:
        first = value.values[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            return first.value.split(":", 1)[0]
    return None


def discover_refusal_literals() -> set[str]:
    tree = ast.parse(inspect.getsource(parser_module))
    discovered: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = ""
        if isinstance(node.func, ast.Name):
            name = node.func.id
        elif isinstance(node.func, ast.Attribute):
            name = node.func.attr
        if name in {"EvidenceParseError", "append"}:
            literal = _first_literal(node)
            if literal and literal.isupper():
                discovered.add(literal)
    discovered.add("MALFORMED_JSON")
    discovered.add("NON_FINITE_FLOAT_POST_PARSE")
    return discovered


def _boundary_results() -> list[dict[str, object]]:
    pairs = [
        ("document_bytes", b"0", b"00", Limits(document_bytes=1)),
        ("nesting_depth", "[0]", "[[0]]", Limits(nesting_depth=1)),
        ("collection_items", "[0]", "[0,1]", Limits(collection_items=1)),
        ("string_characters", '"abc"', '"abcd"', Limits(string_characters=3)),
        ("numeric_range", "10", "11", Limits(numeric_absolute_max=Decimal(10))),
        ("scalar_count", "[0]", "[0,1]", Limits(scalar_count=1)),
        ("receipt_count", '{"receipts":[{}]}', '{"receipts":[{},{}]}', Limits(receipt_count=1)),
        ("path_count", '{"owned_paths":["a"]}', '{"owned_paths":["a","b"]}', Limits(path_count=1)),
        (
            "extension_count",
            '{"extensions":{"a.x":1}}',
            '{"extensions":{"a.x":1,"b.x":2}}',
            Limits(extension_count=1),
        ),
    ]
    results = []
    for name, at_limit, over_limit, limits in pairs:
        at_verdict = parse_evidence(at_limit, limits)[1]["verdict"]
        over_verdict = parse_evidence(over_limit, limits)[1]["verdict"]
        results.append({"limit": name, "at_limit": at_verdict, "over_limit": over_verdict})
    return results


def run_coverage_audit(seed: int = 4108) -> dict[str, object]:
    errors: list[str] = []
    first_corpus = generate_corpus(seed)
    second_corpus = generate_corpus(seed)
    if first_corpus != second_corpus:
        errors.append("CORPUS_NONDETERMINISTIC")
    present_mutations = {str(case["mutation"]) for case in first_corpus["cases"]}
    for mutation in sorted(set(MUTATIONS) - present_mutations):
        errors.append(f"ORPHANED_MUTATION:{mutation}")

    probe_results: list[dict[str, object]] = []
    for branch, factory in sorted(_probes().items()):
        document, limits = factory()
        matched, actual = _probe(document, limits, branch)
        probe_results.append({"branch": branch, "matched": matched, "actual": actual})
        if not matched:
            errors.append(f"BRANCH_NOT_REACHED:{branch}")

    discovered = discover_refusal_literals()
    covered = set(_probes())
    classified = covered | set(JUSTIFIED_UNREACHABLE)
    for branch in sorted(discovered - classified):
        errors.append(f"UNCLASSIFIED_PARSER_BRANCH:{branch}")
    for branch in sorted(covered - discovered):
        if branch != "MALFORMED_JSON":
            errors.append(f"STALE_COVERAGE_TARGET:{branch}")

    boundaries = _boundary_results()
    for boundary in boundaries:
        if boundary["at_limit"] != "PASS" or boundary["over_limit"] != "REFUSE":
            errors.append(f"BOUNDARY_NOT_PROVED:{boundary['limit']}")

    minimizer_results = []
    for document in (b'{"a":1,"a":2}', b'{"schema":"x.v1"', b'{"owned_paths":["../escape"]}'):
        minimized, proof = minimize_failure(document)
        minimizer_results.append(
            {
                "signature": proof["failure_signature"],
                "verdict": proof["verdict"],
                "minimized_sha256": hashlib.sha256(minimized).hexdigest(),
            }
        )
        if proof["verdict"] != "PASS":
            errors.append("MINIMIZER_COVERAGE_FAILED")

    errors = sorted(set(errors))
    result: dict[str, object] = {
        "schema": SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "seed": seed,
        "corpus_sha256": first_corpus["corpus_sha256"],
        "mutation_coverage": {"covered": len(present_mutations), "required": len(MUTATIONS)},
        "branch_coverage": {"covered": len(covered), "discovered": len(discovered)},
        "probe_results": probe_results,
        "boundary_results": boundaries,
        "justified_unreachable": JUSTIFIED_UNREACHABLE,
        "minimizer_results": minimizer_results,
        "errors": errors,
        "safety": {"read_only": True, "bounded": True, "order_capability": False},
    }
    canonical = json.dumps(result, sort_keys=True, separators=(",", ":")).encode()
    result["audit_sha256"] = hashlib.sha256(canonical).hexdigest()
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=int, default=4108)
    args = parser.parse_args()
    result = run_coverage_audit(args.seed)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["verdict"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
