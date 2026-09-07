from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


def _module(filename: str, name: str):
    path = Path(__file__).parents[1] / "scripts/local" / filename
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _modules():
    fixtures = _module("phase4bv_performance_fixtures.py", "phase4bv_for_bw")
    profiler = _module("phase4bw_read_only_profiler.py", "phase4bw_tested")
    return fixtures, profiler


def _measure(events):
    return len(events) * 1_000, len(events) * 64, "a" * 64


def _rehash(module, payload):
    payload["artifact_hash"] = module._hash(payload)


def test_profile_is_deterministic_complete_and_non_authorizing():
    fixtures, profiler = _modules()
    pack = fixtures.build_pack()
    first = profiler.build_profile(pack, measure=_measure)
    assert first == profiler.build_profile(pack, measure=_measure)
    assert [row["name"] for row in first["profiles"]] == list(profiler.EXPECTED_NAMES)
    assert first["all_envelopes_satisfied"] is True
    assert first["production_records_created"] == 0
    assert first["execution_authorized"] is False


def test_workload_output_is_ordered_and_stable():
    fixtures, profiler = _modules()
    events = fixtures.build_pack()["fixtures"][0]["events"]
    assert profiler._workload(events) == profiler._workload(list(reversed(events)))


def test_envelope_boundary_is_inclusive_and_excess_is_reported():
    fixtures, profiler = _modules()
    pack = fixtures.build_pack()
    maximum = pack["fixtures"][0]["expected_envelope"]["max_elapsed_ms"]

    def measure_at_boundary(events):
        elapsed = maximum * 1_000_000 if len(events) == 16 else 0
        return elapsed, 0, "b" * 64

    report = profiler.build_profile(pack, measure=measure_at_boundary)
    assert report["profiles"][0]["elapsed_within_envelope"] is True

    def measure_over(events):
        elapsed = (maximum * 1_000_000 + 1) if len(events) == 16 else 0
        return elapsed, 0, "b" * 64

    report = profiler.build_profile(pack, measure=measure_over)
    assert report["profiles"][0]["elapsed_within_envelope"] is False
    assert report["all_envelopes_satisfied"] is False


@pytest.mark.parametrize(
    "measurement",
    ((-1, 0, "a" * 64), (0, -1, "a" * 64), (True, 0, "a" * 64), (0, 0, "bad")),
)
def test_invalid_measurement_fails_closed(measurement):
    fixtures, profiler = _modules()
    with pytest.raises(ValueError):
        profiler.build_profile(fixtures.build_pack(), measure=lambda events: measurement)


def test_pack_manifest_fixture_and_authority_tampering_fail_closed():
    fixtures, profiler = _modules()
    mutations = (
        lambda p: p.update(manifest_hash="0" * 64),
        lambda p: p["manifest"].update(artifact_hash="0" * 64),
        lambda p: p["fixtures"][0].update(artifact_hash="0" * 64),
        lambda p: p["fixtures"][0].update(execution_authorized=True),
    )
    for mutation in mutations:
        pack = fixtures.build_pack()
        mutation(pack)
        _rehash(profiler, pack)
        with pytest.raises(ValueError):
            profiler.build_profile(pack, measure=_measure)


def test_atomic_publication_round_trip(tmp_path: Path):
    fixtures, profiler = _modules()
    report = profiler.build_profile(fixtures.build_pack(), measure=_measure)
    output = tmp_path / "profile.json"
    profiler.publish(output, report)
    assert json.loads(output.read_text()) == report
    assert not list(tmp_path.glob(".*"))


def test_real_offline_profiler_smoke():
    fixtures, profiler = _modules()
    report = profiler.build_profile(fixtures.build_pack())
    assert all(row["elapsed_ns"] >= 0 and row["peak_bytes"] >= 0 for row in report["profiles"])
    assert all(len(row["output_hash"]) == 64 for row in report["profiles"])


def test_source_has_no_database_service_network_or_exchange_surface():
    source = (
        Path(__file__).parents[1] / "scripts/local/phase4bw_read_only_profiler.py"
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
