from __future__ import annotations

import importlib.util
from datetime import UTC, datetime
from pathlib import Path

import pytest


def _module():
    path = Path(__file__).parents[1] / "scripts/local/phase4bg_resource_bounds_audit.py"
    spec = importlib.util.spec_from_file_location("phase4bg_tested", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


NOW = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)


def test_synthetic_measurements_cover_validation_ordering_publication_memory_and_rollback():
    module = _module()
    audit, proof = module.build(rows=1000, history=1000, artifact_bytes=50_000, now=NOW)
    assert audit["synthetic_data_only"] is True
    assert audit["deterministic_ordering_verified"] is True
    assert audit["rollback_verified"] is True
    assert all(value >= 0 for value in audit["measurements"].values())
    assert proof["limits_checked_before_allocation"] is True
    assert proof["bounded_temporary_publication"] is True
    assert audit["artifact_hash"] == module._hash(audit)


def test_ordering_history_and_artifact_hashes_are_deterministic_across_runs():
    module = _module()
    first, _ = module.build(rows=100, history=200, artifact_bytes=4096, now=NOW)
    second, _ = module.build(rows=100, history=200, artifact_bytes=4096, now=NOW)
    for field in ("ordering_hash", "history_head_hash", "artifact_hash_measured"):
        assert first[field] == second[field]


@pytest.mark.parametrize(
    ("field", "maximum"),
    [
        ("rows", _module().MAX_ROWS),
        ("history", _module().MAX_HISTORY),
        ("artifact_bytes", _module().MAX_ARTIFACT_BYTES),
    ],
)
def test_supported_boundary_values_complete(field: str, maximum: int):
    module = _module()
    arguments = {"rows": 10, "history": 10, "artifact_bytes": 1000, "now": NOW}
    arguments[field] = maximum
    audit, _ = module.build(**arguments)
    assert audit["requested"][field] == maximum


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("rows", _module().MAX_ROWS + 1, "ROWS_LIMIT_EXCEEDED"),
        ("history", _module().MAX_HISTORY + 1, "HISTORY_LIMIT_EXCEEDED"),
        ("artifact_bytes", _module().MAX_ARTIFACT_BYTES + 1, "ARTIFACT_BYTES_LIMIT_EXCEEDED"),
        ("rows", -1, "ROWS_LIMIT_EXCEEDED"),
        ("rows", True, "ROWS_LIMIT_EXCEEDED"),
    ],
)
def test_over_limit_negative_and_boolean_values_refuse_before_work(field: str, value, reason: str):
    module = _module()
    arguments = {"rows": 1, "history": 1, "artifact_bytes": 1, "now": NOW}
    arguments[field] = value
    with pytest.raises(ValueError, match=reason):
        module.build(**arguments)


def test_naive_time_and_static_synthetic_only_surface_fail_closed():
    module = _module()
    with pytest.raises(ValueError, match="EVALUATION_TIMEZONE_MISSING"):
        module.build(rows=1, history=1, artifact_bytes=1, now=datetime(2026, 8, 25, 12, 0))
    source = (
        Path(__file__).parents[1] / "scripts/local/phase4bg_resource_bounds_audit.py"
    ).read_text()
    assert ":memory:" in source
    assert "--production-db" not in source
    assert "systemctl" not in source
    assert "urllib" not in source and "requests" not in source
