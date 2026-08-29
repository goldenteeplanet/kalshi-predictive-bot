"""Differentially certify direct ledger validation against snapshot restoration."""

from __future__ import annotations

import argparse
import hashlib
import json

from scripts.local.phase4me_nonce_consumption_ledger import validate_ledger
from scripts.local.phase4mf_nonce_ledger_snapshot import make_snapshot
from scripts.local.phase4mg_snapshot_restoration import migrate_snapshot, restore_snapshot

SCHEMA = "phase4mh.restoration-differential-certification.v1"
FIELDS = (
    "generation",
    "head_sha256",
    "reserved_or_consumed_nonce_sha256",
    "transactions",
)


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _canonical_records(records: list[object]) -> list[object]:
    identities: dict[object, str] = {}
    result: list[object] = []
    for record in records:
        if not isinstance(record, dict):
            result.append(record)
            continue
        record_id = record.get("record_id")
        fingerprint = _digest(record)
        if record_id in identities and identities[record_id] == fingerprint:
            continue
        identities[record_id] = fingerprint
        result.append(record)
    return result


def _recovery_classification(transactions: object) -> dict[str, str]:
    if not isinstance(transactions, dict):
        return {}
    classification: dict[str, str] = {}
    for transaction_id, transaction in sorted(transactions.items()):
        state = transaction.get("state") if isinstance(transaction, dict) else None
        classification[transaction_id] = {
            "PREPARED": "RECOVER_ABORT",
            "CONSUMED": "RECOVER_COMMIT",
            "COMMITTED": "NO_RECOVERY_TERMINAL",
            "ABORTED": "NO_RECOVERY_TERMINAL",
        }.get(state, "INVALID_STATE")
    return classification


def certify_cut(
    authoritative_records: object,
    *,
    cut_point: int,
    evaluated_at: str,
    supplied_suffix: object | None = None,
) -> dict[str, object]:
    source_sha256 = _digest(authoritative_records)
    errors: list[str] = []
    if not isinstance(authoritative_records, list):
        authoritative_records = []
        errors.append("AUTHORITATIVE_HISTORY_NOT_LIST")
    canonical = _canonical_records(authoritative_records)
    if type(cut_point) is not int or not 0 <= cut_point <= len(canonical):
        errors.append("CUT_POINT_INVALID")
        cut_point = 0
    direct = validate_ledger(authoritative_records, evaluated_at=evaluated_at)
    prefix = validate_ledger(canonical[:cut_point], evaluated_at=evaluated_at)
    restoration: dict[str, object] = {"verdict": "REFUSE", "errors": ["NOT_RUN"]}
    snapshot_hash = None
    artifact_hash = None
    suffix = canonical[cut_point:] if supplied_suffix is None else supplied_suffix
    if direct["verdict"] != "PASS":
        errors.append("AUTHORITATIVE_HISTORY_INVALID")
    if prefix["verdict"] != "PASS":
        errors.append("PREFIX_INVALID")
    if not errors:
        snapshot = make_snapshot(prefix)
        artifact = migrate_snapshot(snapshot, from_version=1, to_version=2)
        snapshot_hash = snapshot["snapshot_sha256"]
        artifact_hash = artifact["artifact_sha256"]
        restoration = restore_snapshot(
            artifact,
            suffix,
            evaluated_at=evaluated_at,
            expected_snapshot_sha256=snapshot["snapshot_sha256"],
            expected_source_generation=snapshot["source_generation"],
            expected_source_head_sha256=snapshot["source_head_sha256"],
        )
        if restoration["verdict"] != "PASS":
            errors.append("RESTORATION_REFUSED")
    divergences: list[dict[str, object]] = []
    if not errors:
        for field in FIELDS:
            if restoration.get(field) != direct.get(field):
                divergences.append(
                    {
                        "field": field,
                        "direct_sha256": _digest(direct.get(field)),
                        "restored_sha256": _digest(restoration.get(field)),
                    }
                )
        direct_recovery = _recovery_classification(direct.get("transactions"))
        restored_recovery = _recovery_classification(restoration.get("transactions"))
        if direct_recovery != restored_recovery:
            divergences.append(
                {
                    "field": "recovery_classification",
                    "direct_sha256": _digest(direct_recovery),
                    "restored_sha256": _digest(restored_recovery),
                }
            )
        direct_replay = {
            nonce: "REFUSE_NONCE_RESERVED_OR_CONSUMED"
            for nonce in direct.get("reserved_or_consumed_nonce_sha256", [])
        }
        restored_replay = {
            nonce: "REFUSE_NONCE_RESERVED_OR_CONSUMED"
            for nonce in restoration.get("reserved_or_consumed_nonce_sha256", [])
        }
        if direct_replay != restored_replay:
            divergences.append(
                {
                    "field": "replay_refusal_decisions",
                    "direct_sha256": _digest(direct_replay),
                    "restored_sha256": _digest(restored_replay),
                }
            )
    result: dict[str, object] = {
        "schema": SCHEMA,
        "verdict": "PASS" if not errors and not divergences else "REFUSE",
        "errors": sorted(set(errors)),
        "cut_point": cut_point,
        "canonical_record_count": len(canonical),
        "snapshot_sha256": snapshot_hash,
        "artifact_sha256": artifact_hash,
        "suffix_sha256": _digest(suffix),
        "direct_validation_sha256": direct["validation_sha256"],
        "restoration_sha256": restoration.get("restoration_sha256"),
        "first_divergent_field": divergences[0]["field"] if divergences else None,
        "divergences": divergences,
        "restoration_errors": restoration.get("errors", []),
        "source_unchanged": _digest(authoritative_records) == source_sha256,
        "safety": {
            "read_only": True,
            "snapshot_persistence": False,
            "production_compaction": False,
            "repair_execution": False,
            "runtime_write": False,
            "wsl_control": False,
            "service_control": False,
            "network_access": False,
            "order_capability": False,
        },
    }
    result["certification_sha256"] = _digest(result)
    return result


def certify_all_cuts(records: object, *, evaluated_at: str) -> dict[str, object]:
    source_sha256 = _digest(records)
    canonical = _canonical_records(records) if isinstance(records, list) else []
    cuts = [
        certify_cut(records, cut_point=cut, evaluated_at=evaluated_at)
        for cut in range(len(canonical) + 1)
    ]
    first_failure = next((row for row in cuts if row["verdict"] != "PASS"), None)
    result: dict[str, object] = {
        "schema": SCHEMA,
        "verdict": "PASS" if cuts and first_failure is None else "REFUSE",
        "source_sha256": source_sha256,
        "source_unchanged": _digest(records) == source_sha256,
        "source_record_count": len(records) if isinstance(records, list) else 0,
        "canonical_record_count": len(canonical),
        "cut_count": len(cuts),
        "passing_cut_count": sum(row["verdict"] == "PASS" for row in cuts),
        "first_failing_cut_point": first_failure["cut_point"] if first_failure else None,
        "first_divergent_field": first_failure["first_divergent_field"] if first_failure else None,
        "cuts": cuts,
        "safety": cuts[0]["safety"] if cuts else {},
    }
    result["certification_sha256"] = _digest(result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("ledger")
    parser.add_argument("--evaluated-at", required=True)
    args = parser.parse_args()
    with open(args.ledger, encoding="utf-8") as stream:
        records = json.load(stream)
    result = certify_all_cuts(records, evaluated_at=args.evaluated_at)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["verdict"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
