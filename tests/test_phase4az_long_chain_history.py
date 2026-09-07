from __future__ import annotations

import importlib.util
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest


def _module():
    path = Path(__file__).parents[1] / "scripts/local/phase4az_long_chain_history.py"
    spec = importlib.util.spec_from_file_location("phase4az_tested", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


NOW = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)


def _source(module, tmp_path: Path, index: int, *, at: datetime | None = None):
    payload = {
        "schema": f"fixture.phase4a{chr(ord('c') + index)}.v1",
        "evaluated_at": (at or NOW + timedelta(minutes=index)).isoformat(),
        "upstream_hash": f"{(index + 1) % 10}" * 64,
        "database_mutation_performed": False,
        "execution_authorized": False,
    }
    payload["artifact_hash"] = module._hash(payload, "artifact_hash")
    path = tmp_path / f"source-{index}.json"
    path.write_text(json.dumps(payload))
    return path, payload


def test_valid_long_chain_checkpoints_reconstruct_lineage_and_bundle(tmp_path: Path):
    module = _module()
    history = tmp_path / "history"
    checkpoint_hash = module.ZERO_HASH
    sources = []
    for index in range(8):
        source, payload = _source(module, tmp_path, index)
        sources.append(payload)
        manifest, checkpoint, pruning = module.append(
            history,
            source,
            phase=f"4A{chr(ord('C') + index)}",
            timestamp_field="evaluated_at",
            retention=20,
        )
        assert checkpoint["previous_checkpoint_hash"] == checkpoint_hash
        checkpoint_hash = checkpoint["checkpoint_hash"]
        assert pruning["chain_integrity_preserved"] is True
    manifest, entries = module.validate(history)
    assert len(entries) == manifest["retained_count"] == 8
    bundle = module.reconstruct(history)
    assert bundle["validated"] is True
    assert bundle["checkpoint_hash"] == checkpoint_hash
    assert set(bundle["cross_phase_lineage_hashes"]) == {p["upstream_hash"] for p in sources}
    assert bundle["artifact_hash"] == module._hash(bundle, "artifact_hash")


def test_duplicate_and_out_of_order_sources_fail_closed(tmp_path: Path):
    module = _module()
    history = tmp_path / "history"
    source, _ = _source(module, tmp_path, 0)
    module.append(history, source, phase="4AC", timestamp_field="evaluated_at", retention=5)
    with pytest.raises(ValueError, match="DUPLICATE_SOURCE"):
        module.append(history, source, phase="4AC", timestamp_field="evaluated_at", retention=5)
    older, _ = _source(module, tmp_path, 1, at=NOW - timedelta(seconds=1))
    with pytest.raises(ValueError, match="SOURCE_OUT_OF_ORDER"):
        module.append(history, older, phase="4AD", timestamp_field="evaluated_at", retention=5)


def test_missing_link_entry_checkpoint_and_tampering_fail_closed(tmp_path: Path):
    module = _module()
    history = tmp_path / "history"
    for index in range(2):
        source, _ = _source(module, tmp_path, index)
        module.append(
            history,
            source,
            phase=f"4A{chr(ord('C') + index)}",
            timestamp_field="evaluated_at",
            retention=5,
        )
    manifest = json.loads((history / "manifest.json").read_text())
    entry_path = history / manifest["entries"][1]["filename"]
    entry = json.loads(entry_path.read_text())
    entry["previous_entry_hash"] = "0" * 64
    entry["entry_hash"] = module._hash(entry, "entry_hash")
    entry_path.write_text(json.dumps(entry))
    manifest["entries"][1]["entry_hash"] = entry["entry_hash"]
    manifest["manifest_hash"] = module._hash(manifest, "manifest_hash")
    (history / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="ENTRY_LINK_INVALID"):
        module.validate(history)
    history = tmp_path / "missing"
    source, _ = _source(module, tmp_path, 3)
    module.append(history, source, phase="4AF", timestamp_field="evaluated_at", retention=5)
    manifest = json.loads((history / "manifest.json").read_text())
    (history / manifest["entries"][0]["filename"]).unlink()
    with pytest.raises(ValueError, match="ENTRY_MISSING"):
        module.validate(history)
    history = tmp_path / "checkpoint"
    source, _ = _source(module, tmp_path, 4)
    module.append(history, source, phase="4AG", timestamp_field="evaluated_at", retention=5)
    (history / "checkpoint-00000000000000000001.json").unlink()
    with pytest.raises(ValueError, match="CHECKPOINT_OR_PRUNING_PROOF_MISSING"):
        module.validate(history)


def test_safe_retention_preserves_anchor_and_never_deletes_untracked_files(tmp_path: Path):
    module = _module()
    history = tmp_path / "history"
    history.mkdir()
    user_file = history / "user-notes.txt"
    user_file.write_text("never delete")
    last_pruning = None
    for index in range(6):
        source, _ = _source(module, tmp_path, index)
        _, _, last_pruning = module.append(
            history,
            source,
            phase=f"4A{chr(ord('C') + index)}",
            timestamp_field="evaluated_at",
            retention=3,
        )
    manifest, entries = module.validate(history)
    assert manifest["retained_count"] == len(entries) == 3
    assert manifest["first_sequence"] == 4
    assert manifest["anchor_previous_entry_hash"] != module.ZERO_HASH
    assert last_pruning and len(last_pruning["pruned_entry_hashes"]) == 1
    assert user_file.read_text() == "never delete"
    assert len(list(history.glob("entry-*.json"))) == 3


def test_manifest_entry_source_pruning_and_checkpoint_tampering_detected(tmp_path: Path):
    module = _module()
    cases = ("manifest", "entry", "source", "pruning", "checkpoint")
    for case in cases:
        history = tmp_path / case
        source, _ = _source(module, tmp_path, len(list(tmp_path.glob("source-*.json"))) + 10)
        module.append(history, source, phase="4AX", timestamp_field="evaluated_at", retention=2)
        manifest_path = history / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        if case == "manifest":
            manifest["head_entry_hash"] = "0" * 64
            manifest_path.write_text(json.dumps(manifest))
        elif case in {"entry", "source"}:
            entry_path = history / manifest["entries"][0]["filename"]
            entry = json.loads(entry_path.read_text())
            if case == "entry":
                entry["phase"] = "4AW"
            else:
                entry["source_payload"]["schema"] = "tampered"
            entry_path.write_text(json.dumps(entry))
        else:
            path = history / f"{case}-00000000000000000001.json"
            payload = json.loads(path.read_text())
            payload["tampered"] = True
            path.write_text(json.dumps(payload))
        with pytest.raises(ValueError):
            module.validate(history)


@pytest.mark.parametrize("retention", [0, -1])
def test_invalid_retention_fails_before_publication(tmp_path: Path, retention: int):
    module = _module()
    source, _ = _source(module, tmp_path, 0)
    with pytest.raises(ValueError, match="RETENTION_INVALID"):
        module.append(
            tmp_path / "history",
            source,
            phase="4AC",
            timestamp_field="evaluated_at",
            retention=retention,
        )
    assert not (tmp_path / "history").exists()


def test_invalid_marker_and_static_surface_fail_closed(tmp_path: Path):
    module = _module()
    history = tmp_path / "history"
    source, _ = _source(module, tmp_path, 0)
    module.append(history, source, phase="4AC", timestamp_field="evaluated_at", retention=2)
    marker = history / ".phase4az-disposable-history.json"
    marker.write_text("{}")
    with pytest.raises(ValueError, match="HISTORY_MARKER_INVALID"):
        module.validate(history)
    source_text = (
        Path(__file__).parents[1] / "scripts/local/phase4az_long_chain_history.py"
    ).read_text()
    assert "sqlite3" not in source_text
    assert "--production-db" not in source_text
    assert "systemctl" not in source_text
