from __future__ import annotations

import copy

import pytest

from scripts.local.phase4mx_archive_replication import (
    audit_repair,
    encode_archive,
    reconstruct_archive,
    repair_shard,
)

PAYLOAD = b"deterministic transcript archive payload"


def _shards(payload=PAYLOAD, generation=4):
    return encode_archive(
        payload,
        archive_sha256="a" * 64,
        generation=generation,
        custody_anchor_sha256="b" * 64,
        placement_domains=["zone-a", "zone-b", "zone-c"],
    )


@pytest.mark.parametrize("indices", [(0, 1), (0, 2), (1, 2)])
def test_every_two_of_three_subset_reconstructs_exactly(indices) -> None:
    shards = _shards()
    result = reconstruct_archive([shards[index] for index in indices])
    assert result["verdict"] == "PASS"
    assert result["payload"] == PAYLOAD


def test_encoding_and_reconstruction_are_deterministic() -> None:
    assert _shards() == _shards()
    assert reconstruct_archive(_shards()[:2]) == reconstruct_archive(_shards()[:2])


def test_insufficient_shards_and_duplicate_domains_refuse() -> None:
    shards = _shards()
    assert "INSUFFICIENT_INDEPENDENT_SHARDS" in reconstruct_archive(shards[:1])["errors"]
    duplicate = copy.deepcopy(shards[:2])
    duplicate[1]["placement_domain"] = duplicate[0]["placement_domain"]
    assert "DUPLICATE_OR_INVALID_FAILURE_DOMAIN" in " ".join(
        reconstruct_archive(duplicate)["errors"]
    )


def test_bit_corruption_and_swapped_metadata_refuse() -> None:
    corrupted = copy.deepcopy(_shards()[:2])
    corrupted[0]["shard_data_b64"] = "AAAA"
    assert "HASH_OR_SCHEMA_INVALID" in " ".join(reconstruct_archive(corrupted)["errors"])
    swapped = copy.deepcopy(_shards()[:2])
    swapped[0]["shard_index"], swapped[1]["shard_index"] = (
        swapped[1]["shard_index"],
        swapped[0]["shard_index"],
    )
    assert "HASH_OR_SCHEMA_INVALID" in " ".join(reconstruct_archive(swapped)["errors"])


def test_stale_generation_and_split_brain_refuse() -> None:
    current, stale = _shards(), _shards(generation=3)
    result = reconstruct_archive([current[0], stale[1]])
    assert "SPLIT_BRAIN_OR_STALE_GENERATION" in result["errors"]
    other = _shards(payload=b"other payload")
    assert (
        "SPLIT_BRAIN_OR_STALE_GENERATION" in reconstruct_archive([current[0], other[1]])["errors"]
    )


def test_repair_is_idempotent_and_independently_verified() -> None:
    shards = _shards()
    first = repair_shard(shards[:2], target_index=2, target_domain="zone-c")
    second = repair_shard(shards[:2], target_index=2, target_domain="zone-c")
    assert first == second
    assert first["verdict"] == "PASS"
    audit = audit_repair(shards, first["repaired_shard"])
    assert audit["verdict"] == "PASS"
    assert audit["independent_verification"] is True


def test_partial_repair_crash_discards_prepared_output() -> None:
    result = repair_shard(
        _shards()[:2], target_index=2, target_domain="zone-c", durability="PREPARED"
    )
    assert result["verdict"] == "REFUSE"
    assert "PREPARED_REPAIR_DISCARDED" in result["errors"]


def test_repair_requires_two_valid_independent_sources() -> None:
    result = repair_shard(_shards()[:1], target_index=2, target_domain="zone-c")
    assert result["verdict"] == "REFUSE"
    assert "REPAIR_SOURCE_QUORUM_INVALID" in result["errors"]


def test_repaired_shard_remains_bound_to_archive_generation_and_custody() -> None:
    shards = _shards()
    repaired = repair_shard(shards[:2], target_index=2, target_domain="zone-c")["repaired_shard"]
    assert repaired["archive_sha256"] == "a" * 64
    assert repaired["generation"] == 4
    assert repaired["custody_anchor_sha256"] == "b" * 64
    assert repaired["placement_domain"] == "zone-c"


def test_model_has_no_filesystem_network_or_operational_capability() -> None:
    safety = reconstruct_archive(_shards()[:2])["safety"]
    assert safety["simulation_only"] is True
    assert all(value is False for key, value in safety.items() if key != "simulation_only")
