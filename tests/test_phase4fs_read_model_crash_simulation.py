from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from kalshi_predictor.phase4cd.read_model_crash_simulation import (
    CrashSimulationError,
    build_simulated_artifact,
    initialize_workspace,
    inspect_workspace,
    simulate_publication,
)

MAX_BYTES = 4096


def test_successful_atomic_replace_advances_current(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    state = simulate_publication(
        root,
        build_simulated_artifact(sequence=1, data={"value": "new"}),
        crash_point="AFTER_REPLACE",
        max_artifact_bytes=MAX_BYTES,
    )
    assert state.status == "CURRENT_ARTIFACT_VALID"
    assert state.current_sequence == 1
    assert state.temporary_state == "ABSENT"


@pytest.mark.parametrize(
    ("crash_point", "temporary_state"),
    [
        ("BEFORE_TEMP_WRITE", "ABSENT"),
        ("AFTER_PARTIAL_TEMP_WRITE", "INVALID"),
        ("AFTER_TEMP_FSYNC", "VALID"),
    ],
)
def test_pre_replace_crashes_preserve_old_current(
    tmp_path: Path, crash_point: str, temporary_state: str
) -> None:
    root = _workspace(tmp_path)
    _publish(root, sequence=1)
    state = simulate_publication(
        root,
        build_simulated_artifact(sequence=2, data={"value": "next"}),
        crash_point=crash_point,
        max_artifact_bytes=MAX_BYTES,
    )
    assert state.current_sequence == 1
    assert state.temporary_state == temporary_state


def test_empty_workspace_and_missing_marker_fail_closed(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    state = inspect_workspace(root, max_artifact_bytes=MAX_BYTES)
    assert state.status == "NO_CURRENT_ARTIFACT"
    unmarked = tmp_path / "phase4fs-unmarked"
    unmarked.mkdir()
    with pytest.raises(CrashSimulationError, match="SIMULATION_MARKER_MISSING"):
        inspect_workspace(unmarked, max_artifact_bytes=MAX_BYTES)


def test_exact_size_boundary_and_overflow(tmp_path: Path) -> None:
    artifact = build_simulated_artifact(sequence=1, data={"value": "x"})
    encoded_size = len(json.dumps(artifact, sort_keys=True, separators=(",", ":"))) + 1
    root = _workspace(tmp_path)
    state = simulate_publication(
        root,
        artifact,
        crash_point="AFTER_REPLACE",
        max_artifact_bytes=encoded_size,
    )
    assert state.current_sequence == 1
    root = _workspace(tmp_path, name="phase4fs-overflow")
    with pytest.raises(CrashSimulationError, match="ARTIFACT_SIZE_EXCEEDED"):
        simulate_publication(
            root,
            artifact,
            crash_point="AFTER_REPLACE",
            max_artifact_bytes=encoded_size - 1,
        )


def test_tampering_and_partial_current_fail_closed(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    artifact = build_simulated_artifact(sequence=1, data={"value": "x"})
    tampered = copy.deepcopy(artifact)
    tampered["sequence"] = 2
    with pytest.raises(CrashSimulationError, match="ARTIFACT_HASH_MISMATCH"):
        simulate_publication(
            root,
            tampered,
            crash_point="AFTER_REPLACE",
            max_artifact_bytes=MAX_BYTES,
        )
    (root / "current.json").write_text("{", encoding="utf-8")
    with pytest.raises(CrashSimulationError, match="CURRENT_ARTIFACT_INVALID"):
        inspect_workspace(root, max_artifact_bytes=MAX_BYTES)


def test_non_simulation_directory_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "production-reports"
    with pytest.raises(CrashSimulationError, match="SIMULATION_DIRECTORY_NAME_INVALID"):
        initialize_workspace(root)
    assert not root.exists()


def test_simulation_never_touches_sibling_files(tmp_path: Path) -> None:
    sibling = tmp_path / "production.db"
    sibling.write_bytes(b"unchanged")
    root = _workspace(tmp_path)
    _publish(root, sequence=1)
    assert sibling.read_bytes() == b"unchanged"


def _publish(root: Path, *, sequence: int) -> None:
    simulate_publication(
        root,
        build_simulated_artifact(sequence=sequence, data={"value": sequence}),
        crash_point="AFTER_REPLACE",
        max_artifact_bytes=MAX_BYTES,
    )


def _workspace(tmp_path: Path, *, name: str = "phase4fs-case") -> Path:
    root = tmp_path / name
    initialize_workspace(root)
    return root
