"""Offline audit-anchor rotation, rollover, and split-view proof."""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import UTC, datetime

from scripts.local.phase4mr_review_history_compaction import restore_history

SCHEMA = "phase4ms.anchor-trust-store.v1"
RECORD_SCHEMA = "phase4ms.anchor-rotation-record.v1"


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else None


def create_trust_store(anchor_sha256: str, *, activated_at: str) -> dict[str, object]:
    return _append_record([], anchor_sha256, activated_at=activated_at, overlap_until=None)


def rotate_anchor(
    store: object,
    new_anchor_sha256: str,
    *,
    activated_at: str,
    overlap_until: str | None,
) -> dict[str, object]:
    if not isinstance(store, dict) or not isinstance(store.get("records"), list):
        return _refusal("STORE_INVALID")
    return _append_record(
        copy.deepcopy(store["records"]),
        new_anchor_sha256,
        activated_at=activated_at,
        overlap_until=overlap_until,
    )


def _append_record(
    records: list[dict[str, object]],
    anchor_sha256: str,
    *,
    activated_at: str,
    overlap_until: str | None,
) -> dict[str, object]:
    if len(anchor_sha256) != 64 or _time(activated_at) is None:
        return _refusal("ROTATION_INPUT_INVALID")
    if records:
        prior_time = _time(records[-1].get("activated_at"))
        if prior_time is None or _time(activated_at) <= prior_time:
            return _refusal("ROTATION_TIME_NOT_MONOTONIC")
        if overlap_until is not None:
            overlap = _time(overlap_until)
            if overlap is None or overlap < _time(activated_at):
                return _refusal("OVERLAP_WINDOW_INVALID")
    elif overlap_until is not None:
        return _refusal("GENESIS_OVERLAP_FORBIDDEN")
    predecessor = records[-1] if records else None
    body: dict[str, object] = {
        "schema": RECORD_SCHEMA,
        "generation": len(records) + 1,
        "anchor_sha256": anchor_sha256,
        "previous_anchor_sha256": predecessor.get("anchor_sha256") if predecessor else None,
        "previous_record_sha256": predecessor.get("record_sha256") if predecessor else "0" * 64,
        "activated_at": activated_at,
        "predecessor_overlap_until": overlap_until,
        "revoked": False,
    }
    records.append({**body, "record_sha256": _digest(body)})
    result: dict[str, object] = {
        "schema": SCHEMA,
        "verdict": "PASS",
        "errors": [],
        "records": records,
        "head_generation": len(records),
        "head_record_sha256": records[-1]["record_sha256"],
        "safety": _safety(),
    }
    result["store_sha256"] = _digest(result)
    return result


def revoke_anchor(store: object, anchor_sha256: str) -> dict[str, object]:
    if not isinstance(store, dict) or not isinstance(store.get("records"), list):
        return _refusal("STORE_INVALID")
    records = copy.deepcopy(store["records"])
    found = False
    for record in records:
        if record.get("anchor_sha256") == anchor_sha256:
            record["revoked"] = True
            body = {key: value for key, value in record.items() if key != "record_sha256"}
            record["record_sha256"] = _digest(body)
            found = True
    if not found:
        return _refusal("UNKNOWN_ANCHOR")
    # Re-link descendants because revocation changes the authenticated chain.
    for index in range(1, len(records)):
        records[index]["previous_record_sha256"] = records[index - 1]["record_sha256"]
        body = {key: value for key, value in records[index].items() if key != "record_sha256"}
        records[index]["record_sha256"] = _digest(body)
    result = {
        "schema": SCHEMA,
        "verdict": "PASS",
        "errors": [],
        "records": records,
        "head_generation": len(records),
        "head_record_sha256": records[-1]["record_sha256"],
        "safety": _safety(),
    }
    result["store_sha256"] = _digest(result)
    return result


def validate_trust_store(
    store: object,
    *,
    expected_store_sha256: str,
    evaluated_at: str,
) -> dict[str, object]:
    errors: list[str] = []
    now = _time(evaluated_at)
    if now is None:
        errors.append("EVALUATION_TIME_INVALID")
    if not isinstance(store, dict) or not isinstance(store.get("records"), list):
        return _validation(["STORE_INVALID"], [])
    if store.get("store_sha256") != expected_store_sha256:
        errors.append("STORE_TRUST_MISMATCH")
    body = {key: value for key, value in store.items() if key != "store_sha256"}
    if store.get("store_sha256") != _digest(body):
        errors.append("STORE_SELF_HASH_INVALID")
    records = store["records"]
    trusted: list[str] = []
    previous_record = "0" * 64
    previous_anchor = None
    previous_time = None
    seen: set[str] = set()
    for index, record in enumerate(records):
        prefix = f"RECORD_{index}_"
        if not isinstance(record, dict):
            errors.append(prefix + "INVALID")
            continue
        record_body = {key: value for key, value in record.items() if key != "record_sha256"}
        if record.get("record_sha256") != _digest(record_body):
            errors.append(prefix + "HASH_INVALID")
        if record.get("generation") != index + 1:
            errors.append(prefix + "GENERATION_INVALID")
        if record.get("previous_record_sha256") != previous_record:
            errors.append(prefix + "PREDECESSOR_RECORD_INVALID")
        if record.get("previous_anchor_sha256") != previous_anchor:
            errors.append(prefix + "PREDECESSOR_ANCHOR_INVALID")
        activated = _time(record.get("activated_at"))
        if activated is None or (previous_time is not None and activated <= previous_time):
            errors.append(prefix + "TIME_INVALID")
        anchor = record.get("anchor_sha256")
        if not isinstance(anchor, str) or len(anchor) != 64:
            errors.append(prefix + "ANCHOR_INVALID")
        elif anchor in seen:
            errors.append(prefix + "ANCHOR_REPLAY")
        else:
            seen.add(anchor)
        previous_record = record.get("record_sha256")
        previous_anchor = anchor
        previous_time = activated
    if records and store.get("head_record_sha256") != previous_record:
        errors.append("HEAD_RECORD_MISMATCH")
    if store.get("head_generation") != len(records):
        errors.append("HEAD_GENERATION_MISMATCH")
    if now is not None and records:
        active_indices = [
            i for i, row in enumerate(records) if _time(row.get("activated_at")) <= now
        ]
        if active_indices:
            current = active_indices[-1]
            row = records[current]
            if not row.get("revoked"):
                trusted.append(row["anchor_sha256"])
            if current > 0:
                overlap = _time(row.get("predecessor_overlap_until"))
                predecessor = records[current - 1]
                if overlap is not None and now <= overlap and not predecessor.get("revoked"):
                    trusted.append(predecessor["anchor_sha256"])
    return _validation(sorted(set(errors)), trusted)


def detect_split_views(stores: object) -> dict[str, object]:
    errors: list[str] = []
    observations: dict[tuple[int, str], set[tuple[str, str]]] = {}
    if not isinstance(stores, list) or not stores:
        errors.append("VIEWS_INVALID")
        stores = []
    for view_index, store in enumerate(stores):
        if not isinstance(store, dict) or not isinstance(store.get("records"), list):
            errors.append(f"VIEW_{view_index}_INVALID")
            continue
        for record in store["records"]:
            if not isinstance(record, dict):
                errors.append(f"VIEW_{view_index}_RECORD_INVALID")
                continue
            key = (record.get("generation"), record.get("previous_record_sha256"))
            observations.setdefault(key, set()).add(
                (record.get("anchor_sha256"), record.get("record_sha256"))
            )
    conflicts = [
        {"generation": key[0], "previous_record_sha256": key[1], "variants": sorted(values)}
        for key, values in observations.items()
        if len(values) > 1
    ]
    if conflicts:
        errors.append("SPLIT_VIEW_OR_EQUIVOCATION")
    heads = {
        (s.get("head_generation"), s.get("head_record_sha256"))
        for s in stores
        if isinstance(s, dict)
    }
    if len(heads) > 1 and not conflicts:
        # Prefix-related views are normal propagation; same-generation divergent heads are not.
        by_generation: dict[object, set[object]] = {}
        for generation, head in heads:
            by_generation.setdefault(generation, set()).add(head)
        if any(len(values) > 1 for values in by_generation.values()):
            errors.append("DIVERGENT_HEADS")
    result: dict[str, object] = {
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": sorted(set(errors)),
        "conflicts": conflicts,
        "view_count": len(stores),
        "reconciliation_authorized": False,
        "safety": _safety(),
    }
    result["split_view_audit_sha256"] = _digest(result)
    return result


def restore_with_trust(
    artifact: dict[str, object],
    archived_prefix: list[object],
    store: dict[str, object],
    *,
    expected_store_sha256: str,
    expected_packet_sha256: str,
    expected_implementation_identity_sha256: str,
    evaluated_at: str,
) -> dict[str, object]:
    trust = validate_trust_store(
        store, expected_store_sha256=expected_store_sha256, evaluated_at=evaluated_at
    )
    anchor = artifact.get("anchor", {}).get("anchor_sha256")
    if trust["verdict"] != "PASS" or anchor not in trust["trusted_anchor_sha256"]:
        return {"verdict": "REFUSE", "errors": ["ANCHOR_NOT_TRUSTED"], "safety": _safety()}
    return restore_history(
        artifact,
        archived_prefix,
        expected_anchor_sha256=anchor,
        expected_packet_sha256=expected_packet_sha256,
        expected_implementation_identity_sha256=expected_implementation_identity_sha256,
        evaluated_at=evaluated_at,
    )


def _validation(errors: list[str], trusted: list[str]) -> dict[str, object]:
    result: dict[str, object] = {
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": errors,
        "trusted_anchor_sha256": trusted if not errors else [],
        "safety": _safety(),
    }
    result["trust_validation_sha256"] = _digest(result)
    return result


def _refusal(error: str) -> dict[str, object]:
    result = {"schema": SCHEMA, "verdict": "REFUSE", "errors": [error], "safety": _safety()}
    result["store_sha256"] = _digest(result)
    return result


def _safety() -> dict[str, bool]:
    return {
        "simulation_only": True,
        "persistence": False,
        "network_access": False,
        "runtime_write": False,
        "service_control": False,
        "order_capability": False,
    }
