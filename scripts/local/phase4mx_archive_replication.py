"""Offline 2-of-3 archive erasure-code and corruption-repair proof."""

from __future__ import annotations

import base64
import hashlib
import json

SCHEMA = "phase4mx.archive-shard.v1"


def _digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _digest(value: object) -> str:
    return _digest_bytes(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())


def encode_archive(
    payload: bytes,
    *,
    archive_sha256: str,
    generation: int,
    custody_anchor_sha256: str,
    placement_domains: list[str],
) -> list[dict[str, object]]:
    if len(placement_domains) != 3 or len(set(placement_domains)) != 3:
        raise ValueError("three distinct placement domains required")
    padded = payload + (b"\0" if len(payload) % 2 else b"")
    half = len(padded) // 2
    data_a, data_b = padded[:half], padded[half:]
    parity = bytes(left ^ right for left, right in zip(data_a, data_b, strict=True))
    content_sha256 = _digest_bytes(payload)
    shards = []
    for index, (domain, data) in enumerate(
        zip(placement_domains, (data_a, data_b, parity), strict=True)
    ):
        body: dict[str, object] = {
            "schema": SCHEMA,
            "archive_sha256": archive_sha256,
            "generation": generation,
            "custody_anchor_sha256": custody_anchor_sha256,
            "content_sha256": content_sha256,
            "content_length": len(payload),
            "data_shards": 2,
            "total_shards": 3,
            "shard_index": index,
            "placement_domain": domain,
            "shard_data_b64": base64.b64encode(data).decode(),
            "durability": "DURABLE",
        }
        shards.append({**body, "shard_sha256": _digest(body)})
    return shards


def reconstruct_archive(shards: object) -> dict[str, object]:
    errors: list[str] = []
    valid: list[dict[str, object]] = []
    if not isinstance(shards, list):
        return _result(["SHARDS_INVALID"], None, [])
    identities: set[tuple[object, ...]] = set()
    indices: set[int] = set()
    domains: set[str] = set()
    for index, shard in enumerate(shards):
        row_errors: list[str] = []
        if not isinstance(shard, dict):
            errors.append(f"SHARD_{index}_INVALID")
            continue
        body = {key: value for key, value in shard.items() if key != "shard_sha256"}
        if shard.get("schema") != SCHEMA or shard.get("shard_sha256") != _digest(body):
            row_errors.append("HASH_OR_SCHEMA_INVALID")
        try:
            base64.b64decode(str(shard.get("shard_data_b64")), validate=True)
        except Exception:
            row_errors.append("DATA_ENCODING_INVALID")
        identity = tuple(
            shard.get(key)
            for key in (
                "archive_sha256",
                "generation",
                "custody_anchor_sha256",
                "content_sha256",
                "content_length",
                "data_shards",
                "total_shards",
            )
        )
        identities.add(identity)
        shard_index = shard.get("shard_index")
        domain = shard.get("placement_domain")
        if shard_index not in {0, 1, 2} or shard_index in indices:
            row_errors.append("DUPLICATE_OR_INVALID_INDEX")
        if not isinstance(domain, str) or domain in domains:
            row_errors.append("DUPLICATE_OR_INVALID_FAILURE_DOMAIN")
        if shard.get("durability") != "DURABLE":
            row_errors.append("SHARD_NOT_DURABLE")
        indices.add(shard_index)
        domains.add(str(domain))
        if row_errors:
            errors.extend(f"SHARD_{index}_{error}" for error in sorted(set(row_errors)))
        else:
            valid.append(shard)
    if len(identities) > 1:
        errors.append("SPLIT_BRAIN_OR_STALE_GENERATION")
    if len(valid) < 2:
        errors.append("INSUFFICIENT_INDEPENDENT_SHARDS")
    payload = None
    if not errors:
        by_index = {
            int(row["shard_index"]): base64.b64decode(str(row["shard_data_b64"])) for row in valid
        }
        if 0 in by_index and 1 in by_index:
            first, second = by_index[0], by_index[1]
        elif 0 in by_index and 2 in by_index:
            first = by_index[0]
            second = bytes(a ^ b for a, b in zip(by_index[0], by_index[2], strict=True))
        else:
            second = by_index[1]
            first = bytes(a ^ b for a, b in zip(by_index[1], by_index[2], strict=True))
        length = int(valid[0]["content_length"])
        payload = (first + second)[:length]
        if _digest_bytes(payload) != valid[0].get("content_sha256"):
            errors.append("RECONSTRUCTED_CONTENT_INVALID")
            payload = None
    return _result(sorted(set(errors)), payload, valid)


def repair_shard(
    shards: list[dict[str, object]],
    *,
    target_index: int,
    target_domain: str,
    durability: str = "DURABLE",
) -> dict[str, object]:
    sources = [row for row in shards if row.get("shard_index") != target_index]
    reconstruction = reconstruct_archive(sources)
    if reconstruction["verdict"] != "PASS":
        return {
            "verdict": "REFUSE",
            "errors": ["REPAIR_SOURCE_QUORUM_INVALID"],
            "safety": _safety(),
        }
    if durability == "PREPARED":
        return {"verdict": "REFUSE", "errors": ["PREPARED_REPAIR_DISCARDED"], "safety": _safety()}
    exemplar = sources[0]
    complete = encode_archive(
        reconstruction["payload"],
        archive_sha256=str(exemplar["archive_sha256"]),
        generation=int(exemplar["generation"]),
        custody_anchor_sha256=str(exemplar["custody_anchor_sha256"]),
        placement_domains=[
            target_domain if index == target_index else f"reconstruction-only-{index}"
            for index in range(3)
        ],
    )
    repaired = complete[target_index]
    result = {
        "verdict": "PASS",
        "errors": [],
        "repaired_shard": repaired,
        "source_shard_sha256": sorted(str(row["shard_sha256"]) for row in sources),
        "source_domain_count": len({row["placement_domain"] for row in sources}),
        "safety": _safety(),
    }
    result["repair_sha256"] = _digest(result)
    return result


def audit_repair(
    original: list[dict[str, object]], repaired: dict[str, object]
) -> dict[str, object]:
    candidate = [row for row in original if row.get("shard_index") != repaired.get("shard_index")]
    candidate.append(repaired)
    reconstruction = reconstruct_archive(candidate)
    result = {
        "verdict": reconstruction["verdict"],
        "errors": reconstruction["errors"],
        "content_sha256": _digest_bytes(reconstruction["payload"])
        if reconstruction["payload"] is not None
        else None,
        "independent_verification": reconstruction["verdict"] == "PASS",
        "safety": _safety(),
    }
    result["audit_sha256"] = _digest(result)
    return result


def _result(
    errors: list[str], payload: bytes | None, valid: list[dict[str, object]]
) -> dict[str, object]:
    result: dict[str, object] = {
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": errors,
        "payload": payload,
        "valid_shard_count": len(valid),
        "source_domains": sorted(str(row.get("placement_domain")) for row in valid),
        "safety": _safety(),
    }
    result["reconstruction_sha256"] = _digest(
        {**result, "payload": base64.b64encode(payload).decode() if payload is not None else None}
    )
    return result


def _safety() -> dict[str, bool]:
    return {
        "simulation_only": True,
        "filesystem_write": False,
        "network_access": False,
        "runtime_write": False,
        "service_control": False,
        "order_capability": False,
    }
