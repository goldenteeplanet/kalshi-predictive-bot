"""Bounded mutation coverage and counterexample minimization for Phase 4MH."""

from __future__ import annotations

import copy
import hashlib
import json
import re

from scripts.local.phase4me_nonce_consumption_ledger import FIELDS as RECORD_FIELDS
from scripts.local.phase4me_nonce_consumption_ledger import _digest as ledger_digest
from scripts.local.phase4me_nonce_consumption_ledger import validate_ledger
from scripts.local.phase4mf_nonce_ledger_snapshot import FIELDS as SNAPSHOT_FIELDS
from scripts.local.phase4mf_nonce_ledger_snapshot import (
    _body_hash as snapshot_body_hash,
)
from scripts.local.phase4mf_nonce_ledger_snapshot import make_snapshot, validate_snapshot
from scripts.local.phase4mg_snapshot_restoration import FIELDS as ARTIFACT_FIELDS
from scripts.local.phase4mg_snapshot_restoration import (
    _body_hash as artifact_body_hash,
)
from scripts.local.phase4mg_snapshot_restoration import (
    artifact_errors,
    migrate_snapshot,
    restore_snapshot,
)
from scripts.local.phase4mh_restoration_differential_replay import certify_all_cuts, certify_cut

SCHEMA = "phase4mi.differential-mutation-coverage.v1"
MAX_MUTATIONS = 128


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _changed(value: object) -> object:
    if value is None:
        return "mutation"
    if isinstance(value, bool):
        return not value
    if type(value) is int:
        return value + 7
    if isinstance(value, str):
        if re.fullmatch(r"[0-9a-f]{64}", value):
            return "f" * 64 if value != "f" * 64 else "e" * 64
        return f"mutated:{value}"
    if isinstance(value, list):
        return [] if value else ["mutation"]
    if isinstance(value, dict):
        return {}
    return "mutation"


def _failure_signature(result: dict[str, object]) -> str:
    if result.get("first_divergent_field"):
        return f"DIVERGENCE:{result['first_divergent_field']}"
    restoration_errors = result.get("restoration_errors")
    if isinstance(restoration_errors, list) and restoration_errors:
        normalized = re.sub(r"^SUFFIX_\d+_", "SUFFIX_#_", str(restoration_errors[0]))
        return f"RESTORATION:{normalized}"
    errors = result.get("errors")
    if isinstance(errors, list) and errors:
        return f"CERTIFIER:{errors[0]}"
    return "SILENT_EQUIVALENCE"


def minimize_suffix_counterexample(
    authoritative_records: list[dict[str, object]],
    *,
    cut_point: int,
    supplied_suffix: list[dict[str, object]],
    evaluated_at: str,
) -> dict[str, object]:
    original = copy.deepcopy(supplied_suffix)
    baseline = certify_cut(
        authoritative_records,
        cut_point=cut_point,
        supplied_suffix=original,
        evaluated_at=evaluated_at,
    )
    target = _failure_signature(baseline)
    minimized = copy.deepcopy(original)
    changed = True
    while changed and minimized:
        changed = False
        for index in range(len(minimized)):
            candidate = minimized[:index] + minimized[index + 1 :]
            result = certify_cut(
                authoritative_records,
                cut_point=cut_point,
                supplied_suffix=candidate,
                evaluated_at=evaluated_at,
            )
            if result["verdict"] == "REFUSE" and _failure_signature(result) == target:
                minimized = candidate
                changed = True
                break
    result: dict[str, object] = {
        "failure_signature": target,
        "original_count": len(original),
        "minimized_count": len(minimized),
        "original_sha256": _digest(original),
        "minimized_sha256": _digest(minimized),
        "strictly_reduced": len(minimized) < len(original),
    }
    result["minimization_sha256"] = _digest(result)
    return result


def _record_mutations(
    records: list[dict[str, object]], evaluated_at: str
) -> list[dict[str, object]]:
    results: list[dict[str, object]] = []
    for field in sorted(RECORD_FIELDS):
        candidate = copy.deepcopy(records)
        candidate[1][field] = _changed(candidate[1][field])
        certification = certify_all_cuts(candidate, evaluated_at=evaluated_at)
        results.append(
            {
                "mutation_id": f"record-unrehash-{field}",
                "surface": "authoritative_record",
                "detected": certification["verdict"] == "REFUSE",
                "failure_signature": (
                    f"CUT:{certification['first_failing_cut_point']}"
                    if certification["verdict"] == "REFUSE"
                    else "SILENT_EQUIVALENCE"
                ),
            }
        )
    semantic_fields = {
        "operation": "INVALID",
        "generation": 99,
        "expected_generation": 99,
        "transaction_id": "tx-substitution",
        "token_id": "f" * 64,
        "nonce_sha256": "f" * 64,
        "receipt_sha256": "f" * 64,
        "occurred_at": "2026-08-28T19:00:00Z",
        "previous_record_sha256": "f" * 64,
    }
    for field, value in semantic_fields.items():
        candidate = copy.deepcopy(records)
        candidate[1][field] = value
        candidate[1]["record_sha256"] = ledger_digest(
            {key: item for key, item in candidate[1].items() if key != "record_sha256"}
        )
        certification = certify_all_cuts(candidate, evaluated_at=evaluated_at)
        results.append(
            {
                "mutation_id": f"record-rehashed-{field}",
                "surface": "authoritative_record_semantics",
                "detected": certification["verdict"] == "REFUSE",
                "failure_signature": (
                    f"CUT:{certification['first_failing_cut_point']}"
                    if certification["verdict"] == "REFUSE"
                    else "SILENT_EQUIVALENCE"
                ),
            }
        )
    return results


def _suffix_mutations(
    records: list[dict[str, object]], evaluated_at: str
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    results: list[dict[str, object]] = []
    minimizations: list[dict[str, object]] = []
    for cut in range(len(records)):
        suffix = copy.deepcopy(records[cut:])
        cases: list[tuple[str, list[dict[str, object]]]] = [
            ("omit-last", suffix[:-1]),
        ]
        if len(suffix) > 1:
            cases.append(("reverse", list(reversed(suffix))))
        corrupted = copy.deepcopy(suffix)
        corrupted[0]["record_sha256"] = "0" * 64
        cases.append(("hash-corrupt", corrupted))
        substituted = copy.deepcopy(suffix)
        substituted[0]["token_id"] = "f" * 64
        substituted[0]["record_sha256"] = ledger_digest(
            {key: value for key, value in substituted[0].items() if key != "record_sha256"}
        )
        cases.append(("token-substitute", substituted))
        for name, candidate in cases:
            certification = certify_cut(
                records,
                cut_point=cut,
                supplied_suffix=candidate,
                evaluated_at=evaluated_at,
            )
            detected = certification["verdict"] == "REFUSE"
            results.append(
                {
                    "mutation_id": f"suffix-cut-{cut}-{name}",
                    "surface": "retained_suffix",
                    "detected": detected,
                    "failure_signature": _failure_signature(certification),
                }
            )
            if detected and len(candidate) > 1:
                minimizations.append(
                    minimize_suffix_counterexample(
                        records,
                        cut_point=cut,
                        supplied_suffix=candidate,
                        evaluated_at=evaluated_at,
                    )
                )
    return results, minimizations


def _snapshot_mutations(
    records: list[dict[str, object]], evaluated_at: str
) -> list[dict[str, object]]:
    source = validate_ledger(records[:1], evaluated_at=evaluated_at)
    snapshot = make_snapshot(source)
    results: list[dict[str, object]] = []
    for field in sorted(SNAPSHOT_FIELDS):
        candidate = copy.deepcopy(snapshot)
        candidate[field] = _changed(candidate[field])
        if field != "snapshot_sha256":
            candidate["snapshot_sha256"] = snapshot_body_hash(candidate)
        validation = validate_snapshot(
            candidate,
            source,
            expected_prior_snapshot_sha256="0" * 64,
        )
        results.append(
            {
                "mutation_id": f"snapshot-{field}",
                "surface": "snapshot_binding",
                "detected": validation["verdict"] == "REFUSE",
                "failure_signature": (
                    validation["errors"][0] if validation["errors"] else "SILENT_EQUIVALENCE"
                ),
            }
        )
    return results


def _artifact_mutations(
    records: list[dict[str, object]], evaluated_at: str
) -> list[dict[str, object]]:
    source = validate_ledger(records[:1], evaluated_at=evaluated_at)
    snapshot = make_snapshot(source)
    artifact = migrate_snapshot(snapshot, from_version=1, to_version=2)
    results: list[dict[str, object]] = []
    for field in sorted(ARTIFACT_FIELDS):
        candidate = copy.deepcopy(artifact)
        candidate[field] = _changed(candidate[field])
        if field != "artifact_sha256":
            candidate["artifact_sha256"] = artifact_body_hash(candidate)
        errors = artifact_errors(candidate)
        if not errors:
            restored = restore_snapshot(
                candidate,
                records[1:],
                evaluated_at=evaluated_at,
                expected_snapshot_sha256=snapshot["snapshot_sha256"],
                expected_source_generation=snapshot["source_generation"],
                expected_source_head_sha256=snapshot["source_head_sha256"],
                expected_prior_snapshot_sha256=snapshot["prior_snapshot_sha256"],
            )
            detected = restored["verdict"] == "REFUSE"
            signature = restored["errors"][0] if restored["errors"] else "SILENT_EQUIVALENCE"
        else:
            detected = True
            signature = errors[0]
        results.append(
            {
                "mutation_id": f"artifact-{field}",
                "surface": "migration_artifact_binding",
                "detected": detected,
                "failure_signature": signature,
            }
        )
    return results


def run_mutation_audit(
    records: object,
    *,
    evaluated_at: str,
    max_mutations: int = MAX_MUTATIONS,
) -> dict[str, object]:
    source_sha256 = _digest(records)
    errors: list[str] = []
    if not isinstance(records, list) or len(records) < 2:
        errors.append("CORPUS_HISTORY_TOO_SHORT")
        records = []
    elif validate_ledger(records, evaluated_at=evaluated_at)["verdict"] != "PASS":
        errors.append("CORPUS_HISTORY_INVALID")
    if type(max_mutations) is not int or not 1 <= max_mutations <= MAX_MUTATIONS:
        errors.append("MUTATION_BOUND_INVALID")
    mutations: list[dict[str, object]] = []
    minimizations: list[dict[str, object]] = []
    if not errors:
        mutations.extend(_record_mutations(records, evaluated_at))
        suffix, minimizations = _suffix_mutations(records, evaluated_at)
        mutations.extend(suffix)
        mutations.extend(_snapshot_mutations(records, evaluated_at))
        mutations.extend(_artifact_mutations(records, evaluated_at))
    if len(mutations) > max_mutations:
        errors.append("MUTATION_BOUND_EXCEEDED")
    silent = [row["mutation_id"] for row in mutations if not row["detected"]]
    if silent:
        errors.append("SEMANTIC_MUTATION_SILENT")
    coverage = {
        surface: sum(row["surface"] == surface for row in mutations)
        for surface in sorted({str(row["surface"]) for row in mutations})
    }
    result: dict[str, object] = {
        "schema": SCHEMA,
        "verdict": "PASS" if not errors and mutations else "REFUSE",
        "errors": sorted(set(errors)),
        "source_sha256": source_sha256,
        "source_unchanged": _digest(records) == source_sha256,
        "mutation_bound": max_mutations,
        "mutation_count": len(mutations),
        "detected_count": sum(bool(row["detected"]) for row in mutations),
        "silent_mutation_ids": silent,
        "surface_coverage": coverage,
        "corpus_sha256": _digest(mutations),
        "minimization_count": len(minimizations),
        "minimization_sha256": _digest(minimizations),
        "mutations": mutations,
        "minimizations": minimizations,
        "safety": {
            "bounded": True,
            "read_only": True,
            "artifact_persistence": False,
            "production_compaction": False,
            "repair_execution": False,
            "runtime_write": False,
            "wsl_control": False,
            "service_control": False,
            "network_access": False,
            "order_capability": False,
        },
    }
    result["audit_sha256"] = _digest(result)
    return result
