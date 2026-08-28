"""Deterministic fuzz corpus and failure-preserving minimizer for Phase 4LG."""

from __future__ import annotations

import argparse
import hashlib
import json
import random

from scripts.local.phase4lg_bounded_evidence_parser import Limits, parse_evidence

SCHEMA = "phase4lh.evidence-parser-fuzz-corpus.v1"
MINIMIZER_SCHEMA = "phase4lh.deterministic-minimizer-proof.v1"
MUTATIONS = (
    "valid",
    "duplicate_key",
    "truncation",
    "invalid_utf8",
    "numeric_abuse",
    "deep_nesting",
    "collection_fanout",
    "unicode_control",
    "path_confusion",
    "schema_mutation",
    "capability_extension",
    "trailing_data",
    "oversized_string",
)
PASSING_MUTATIONS = {"valid", "schema_mutation"}


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _document(mutation: str, rng: random.Random) -> bytes:
    nonce = rng.randrange(1_000_000)
    valid = {"schema": "phase4lb.commit-evidence-ledger.v1", "nonce": nonce}
    if mutation == "valid":
        return json.dumps(valid, sort_keys=True, separators=(",", ":")).encode()
    if mutation == "duplicate_key":
        return b'{"schema":"x.v1","schema":"y.v1"}'
    if mutation == "truncation":
        return json.dumps(valid, sort_keys=True).encode()[:-1]
    if mutation == "invalid_utf8":
        return b'{"value":"\xff"}'
    if mutation == "numeric_abuse":
        return f"1e{100 + nonce % 900}".encode()
    if mutation == "deep_nesting":
        return ("[" * 40 + "0" + "]" * 40).encode()
    if mutation == "collection_fanout":
        return ("[" + ",".join("0" for _ in range(1_001)) + "]").encode()
    if mutation == "unicode_control":
        return b'"unsafe\\u202etext"'
    if mutation == "path_confusion":
        return json.dumps({"owned_paths": ["../escape"]}).encode()
    if mutation == "schema_mutation":
        return json.dumps({**valid, "schema": f"unknown.family.v{2 + nonce % 8}"}).encode()
    if mutation == "capability_extension":
        return json.dumps(
            {"extensions": {"vendor.capabilities": {"order_capability": True}}}
        ).encode()
    if mutation == "trailing_data":
        return json.dumps(valid).encode() + b" trailing"
    if mutation == "oversized_string":
        return json.dumps("x" * 4_097).encode()
    raise ValueError(f"unknown mutation: {mutation}")


def _signature(document: bytes, limits: Limits | None = None) -> str:
    _, result = parse_evidence(document, limits)
    if result["verdict"] == "PASS":
        return "PASS"
    return str(result["errors"][0])


def generate_corpus(seed: int, count: int = 39) -> dict[str, object]:
    if count < len(MUTATIONS) or count > 256:
        raise ValueError("count must cover every mutation and remain bounded")
    rng = random.Random(seed)
    cases: list[dict[str, object]] = []
    for index in range(count):
        mutation = MUTATIONS[index % len(MUTATIONS)]
        document = _document(mutation, rng)
        expected = "PASS" if mutation in PASSING_MUTATIONS else "REFUSE"
        signature = _signature(document)
        actual = "PASS" if signature == "PASS" else "REFUSE"
        identity = hashlib.sha256(
            f"{seed}:{index}:{mutation}:{_sha(document)}".encode()
        ).hexdigest()[:20]
        cases.append(
            {
                "case_id": f"fuzz-{identity}",
                "index": index,
                "seed": seed,
                "mutation": mutation,
                "document_bytes": len(document),
                "document_sha256": _sha(document),
                "expected_verdict": expected,
                "actual_verdict": actual,
                "failure_signature": signature,
            }
        )
    case_ids = [str(case["case_id"]) for case in cases]
    errors: list[str] = []
    if len(case_ids) != len(set(case_ids)):
        errors.append("DUPLICATE_CASE_ID")
    for case in cases:
        if case["expected_verdict"] != case["actual_verdict"]:
            errors.append(f"UNEXPECTED_VERDICT:{case['case_id']}")
    payload: dict[str, object] = {
        "schema": SCHEMA,
        "seed": seed,
        "case_count": len(cases),
        "mutation_kinds": list(MUTATIONS),
        "cases": cases,
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": errors,
        "safety": {
            "bounded_generation": True,
            "read_only": True,
            "database_access": False,
            "service_control": False,
            "order_capability": False,
        },
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    payload["corpus_sha256"] = _sha(canonical)
    return payload


def minimize_failure(
    document: bytes, expected_signature: str | None = None, max_evaluations: int = 256
) -> tuple[bytes, dict[str, object]]:
    signature = expected_signature or _signature(document)
    if signature == "PASS" or _signature(document) != signature:
        raise ValueError("input must reproduce the requested failure signature")
    candidate = document
    evaluations = 1
    chunk = max(1, len(candidate) // 2)
    while chunk >= 1 and evaluations < max_evaluations:
        changed = False
        offset = 0
        while offset < len(candidate) and evaluations < max_evaluations:
            trial = candidate[:offset] + candidate[offset + chunk :]
            evaluations += 1
            if trial and _signature(trial) == signature:
                candidate = trial
                changed = True
            else:
                offset += chunk
        if not changed:
            chunk //= 2
    proof: dict[str, object] = {
        "schema": MINIMIZER_SCHEMA,
        "verdict": "PASS" if _signature(candidate) == signature else "REFUSE",
        "failure_signature": signature,
        "original_bytes": len(document),
        "minimized_bytes": len(candidate),
        "original_sha256": _sha(document),
        "minimized_sha256": _sha(candidate),
        "evaluations": evaluations,
        "evaluation_limit": max_evaluations,
        "provenance_valid": True,
        "errors": [],
    }
    canonical = json.dumps(proof, sort_keys=True, separators=(",", ":")).encode()
    proof["proof_sha256"] = _sha(canonical)
    return candidate, proof


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=int, default=4_108)
    parser.add_argument("--count", type=int, default=39)
    args = parser.parse_args()
    result = generate_corpus(args.seed, args.count)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["verdict"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
