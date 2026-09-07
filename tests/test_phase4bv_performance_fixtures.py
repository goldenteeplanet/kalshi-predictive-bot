from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

import pytest


def _module():
    path = Path(__file__).parents[1] / "scripts/local/phase4bv_performance_fixtures.py"
    spec = importlib.util.spec_from_file_location("phase4bv_tested", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _rehash(module, payload):
    payload["artifact_hash"] = module._hash(payload)


def test_pack_is_deterministic_complete_and_non_authorizing():
    module = _module()
    first, second = module.build_pack(), module.build_pack()
    assert first == second
    assert [row["name"] for row in first["fixtures"]] == list(module.FIXTURE_NAMES)
    assert first["manifest"]["fixture_count"] == 5
    assert all(row["production_records_created"] == 0 for row in first["fixtures"])
    assert all(row["execution_authorized"] is False for row in first["fixtures"])


@pytest.mark.parametrize("name", ("small", "medium", "large", "sparse", "burst"))
def test_fixture_counts_hashes_and_envelopes_are_stable(name: str):
    module = _module()
    pack = module.build_pack()
    fixture = next(row for row in pack["fixtures"] if row["name"] == name)
    assert len(fixture["events"]) == module.SPECIFICATIONS[name]["event_count"]
    assert fixture["expected_envelope"] == module.ENVELOPES[name]
    assert len(fixture["artifact_hash"]) == 64
    assert fixture["artifact_hash"] == module._hash(fixture)


def test_sparse_and_burst_shapes_are_representative():
    module = _module()
    fixtures = {row["name"]: row for row in module.build_pack()["fixtures"]}
    sparse_active = sum(row["active"] for row in fixtures["sparse"]["events"])
    burst = fixtures["burst"]["events"]
    assert sparse_active == 16
    assert all(row["active"] for row in burst[:64])
    assert not any(row["active"] for row in burst[64:128])


@pytest.mark.parametrize(
    "mutation",
    (
        lambda p: p["fixtures"][0]["events"].pop(),
        lambda p: p["fixtures"][0]["events"][0].update(sequence=2),
        lambda p: p["fixtures"][0].update(expected_envelope={"max_elapsed_ms": 1}),
        lambda p: p["fixtures"][0].update(execution_authorized=True),
    ),
)
def test_tampered_fixture_fails_closed_after_outer_rehash(mutation):
    module = _module()
    pack = module.build_pack()
    mutation(pack)
    _rehash(module, pack["fixtures"][0])
    pack["manifest"]["fixtures"][0]["fixture_hash"] = pack["fixtures"][0]["artifact_hash"]
    _rehash(module, pack["manifest"])
    pack["manifest_hash"] = pack["manifest"]["artifact_hash"]
    _rehash(module, pack)
    with pytest.raises(ValueError):
        module.validate_pack(pack)


def test_manifest_and_pack_tampering_fail_closed():
    module = _module()
    for mutation in (
        lambda p: p["manifest"].update(fixture_count=4),
        lambda p: p.update(manifest_hash="0" * 64),
        lambda p: p["manifest"]["fixtures"].reverse(),
        lambda p: p["fixtures"].reverse(),
    ):
        pack = module.build_pack()
        mutation(pack)
        with pytest.raises(ValueError):
            module.validate_pack(pack)


def test_atomic_publication_and_round_trip(tmp_path: Path):
    module = _module()
    pack = module.build_pack()
    module.publish_pack(tmp_path, pack)
    assert sorted(path.name for path in tmp_path.iterdir()) == sorted(
        [f"phase4bv-{name}.json" for name in module.FIXTURE_NAMES]
        + ["phase4bv-manifest.json", "phase4bv-pack.json"]
    )
    loaded = json.loads((tmp_path / "phase4bv-pack.json").read_text())
    module.validate_pack(loaded)
    assert not list(tmp_path.glob(".*"))


def test_duplicate_or_unknown_fixture_fails_closed():
    module = _module()
    for replacement in ("small", "unknown"):
        pack = module.build_pack()
        pack["fixtures"][1]["name"] = replacement
        _rehash(module, pack["fixtures"][1])
        _rehash(module, pack)
        with pytest.raises(ValueError):
            module.validate_pack(pack)


def test_input_copy_does_not_alias_pack():
    module = _module()
    first = module.build_pack()
    second = copy.deepcopy(first)
    second["fixtures"][0]["events"][0]["active"] = False
    assert first != second


def test_source_has_no_database_service_network_or_exchange_surface():
    source = (
        Path(__file__).parents[1] / "scripts/local/phase4bv_performance_fixtures.py"
    ).read_text()
    for token in (
        "sqlite3",
        "subprocess",
        "requests",
        "systemctl",
        "exchange_client",
        "/home/james",
    ):
        assert token not in source
