from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


def _module():
    path = Path(__file__).parents[1] / "scripts/local/phase4cr_atomicity_stress.py"
    spec = importlib.util.spec_from_file_location("phase4cr_tested", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_stress_interruption_readers_replacement_and_recovery():
    module = _module()
    report = module.run_stress(generations=20, readers=8)
    assert report["total_successful_reads"] == 20 * 8 * 2
    assert report["read_errors"] == []
    assert report["boundary_violations"] == 0
    assert report["interrupted_write_refused"] is True
    assert report["final_generation"] == 20
    assert report["stale_temporaries_after_recovery"] == 0
    assert report["disposable_directory"] is True
    assert report["production_paths_touched"] == 0


def test_stress_report_is_deterministic():
    module = _module()
    assert module.run_stress(5, 4) == module.run_stress(5, 4)


def test_atomic_replacement_never_leaves_temporary(tmp_path: Path):
    module = _module()
    target = tmp_path / "snapshot.json"
    for generation in range(50):
        module.atomic_publish(target, module.snapshot(generation))
        assert module.validate(target)["generation"] == generation
        assert not list(tmp_path.glob(f".{target.name}.*"))


def test_partial_and_hash_tampered_artifacts_are_refused(tmp_path: Path):
    module = _module()
    partial = tmp_path / "partial.json"
    partial.write_text('{"schema":', encoding="utf-8")
    with pytest.raises(ValueError, match="UNREADABLE"):
        module.validate(partial)
    tampered = module.snapshot(1)
    tampered["generation"] = 2
    partial.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(ValueError, match="INVALID"):
        module.validate(partial)


def test_invalid_publication_payload_is_refused(tmp_path: Path):
    module = _module()
    payload = module.snapshot(1)
    payload["payload"]["generation_marker"] = "tampered"
    with pytest.raises(ValueError, match="PAYLOAD"):
        module.atomic_publish(tmp_path / "snapshot.json", payload)


@pytest.mark.parametrize(
    ("generations", "readers"), [(0, 1), (1001, 1), (1, 0), (1, 65), (True, 1)]
)
def test_resource_bounds_fail_closed(generations, readers):
    module = _module()
    with pytest.raises(ValueError):
        module.run_stress(generations, readers)


def test_report_publication_is_atomic(tmp_path: Path):
    module = _module()
    report = module.run_stress(2, 2)
    output = tmp_path / "stress.json"
    module.publish_report(output, report)
    assert json.loads(output.read_text()) == report
    assert not list(tmp_path.glob(".*"))


def test_source_has_no_database_network_service_or_exchange_surface():
    source = (Path(__file__).parents[1] / "scripts/local/phase4cr_atomicity_stress.py").read_text()
    for token in (
        "sqlite3",
        "requests",
        "httpx",
        "subprocess",
        "systemctl",
        "exchange_client",
        "/home/james",
    ):
        assert token not in source
