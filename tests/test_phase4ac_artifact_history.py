from __future__ import annotations

import importlib.util
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest


def _module():
    path = Path(__file__).parents[1] / "scripts/local/phase4ac_artifact_history.py"
    spec = importlib.util.spec_from_file_location("phase4ac_artifact_history", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _source(module, at: datetime, index: int, kind: str = "PHASE4AA_GATE"):
    schema = module.SOURCE_SCHEMAS[kind]
    payload = {
        "schema": schema,
        "generated_at": at.isoformat(),
        "state": "WAITING",
        "safe_to_advance": False,
        "production_database_written": False,
        "trading_mode_changed": False,
        "index": index,
    }
    payload["artifact_hash"] = module._artifact_hash(payload)
    return payload


def _write(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_valid_chain_and_read_only_reconstruction(tmp_path: Path) -> None:
    module = _module()
    start = datetime(2026, 8, 25, 18, tzinfo=UTC)
    for index in range(3):
        source = tmp_path / f"source-{index}.json"
        _write(source, _source(module, start + timedelta(minutes=index), index))
        module.append(tmp_path / "history", source, kind="PHASE4AA_GATE", retention=10)
    manifest, entries = module.validate_history(tmp_path / "history")
    reconstructed = module.reconstruct_phase4ab_history(tmp_path / "history")
    assert [entry["sequence"] for entry in entries] == [1, 2, 3]
    assert manifest["head_entry_hash"] == entries[-1]["entry_hash"]
    assert reconstructed["validated"] is True
    assert reconstructed["phase4aa_gate_count"] == 3


def test_duplicate_and_out_of_order_snapshots_are_rejected(tmp_path: Path) -> None:
    module = _module()
    start = datetime(2026, 8, 25, 18, tzinfo=UTC)
    source = tmp_path / "source.json"
    _write(source, _source(module, start, 1))
    history = tmp_path / "history"
    module.append(history, source, kind="PHASE4AA_GATE", retention=10)
    with pytest.raises(ValueError, match="DUPLICATE_SNAPSHOT"):
        module.append(history, source, kind="PHASE4AA_GATE", retention=10)
    older = tmp_path / "older.json"
    _write(older, _source(module, start - timedelta(seconds=1), 2))
    with pytest.raises(ValueError, match="TIMESTAMP_OUT_OF_ORDER"):
        module.append(history, older, kind="PHASE4AA_GATE", retention=10)


def test_missing_link_and_entry_tampering_fail_closed(tmp_path: Path) -> None:
    module = _module()
    start = datetime(2026, 8, 25, 18, tzinfo=UTC)
    history = tmp_path / "history"
    for index in range(2):
        source = tmp_path / f"source-{index}.json"
        _write(source, _source(module, start + timedelta(minutes=index), index))
        module.append(history, source, kind="PHASE4AA_GATE", retention=10)
    manifest = json.loads((history / "manifest.json").read_text(encoding="utf-8"))
    second = history / manifest["entries"][1]["filename"]
    entry = json.loads(second.read_text(encoding="utf-8"))
    entry["previous_entry_hash"] = "f" * 64
    entry["entry_hash"] = module._entry_hash(entry)
    second.write_text(json.dumps(entry), encoding="utf-8")
    manifest["entries"][1]["entry_hash"] = entry["entry_hash"]
    manifest["head_entry_hash"] = entry["entry_hash"]
    manifest["manifest_hash"] = module._manifest_hash(manifest)
    (history / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="LINK_MISSING"):
        module.validate_history(history)
    entry["previous_entry_hash"] = manifest["entries"][0]["entry_hash"]
    entry["source_payload"]["index"] = 999
    second.write_text(json.dumps(entry), encoding="utf-8")
    with pytest.raises(ValueError, match="ENTRY_HASH_MISMATCH"):
        module.validate_history(history)


def test_missing_entry_is_detected(tmp_path: Path) -> None:
    module = _module()
    source = tmp_path / "source.json"
    _write(source, _source(module, datetime(2026, 8, 25, 18, tzinfo=UTC), 1))
    history = tmp_path / "history"
    manifest = module.append(history, source, kind="PHASE4AA_GATE", retention=10)
    (history / manifest["entries"][0]["filename"]).unlink()
    with pytest.raises(ValueError, match="ENTRY_MISSING"):
        module.validate_history(history)


def test_retention_keeps_anchor_and_chain_integrity(tmp_path: Path) -> None:
    module = _module()
    start = datetime(2026, 8, 25, 18, tzinfo=UTC)
    history = tmp_path / "history"
    for index in range(5):
        source = tmp_path / f"source-{index}.json"
        kind = "PHASE4AA_GATE" if index % 2 == 0 else "PHASE4AB_DWELL"
        _write(source, _source(module, start + timedelta(minutes=index), index, kind))
        module.append(history, source, kind=kind, retention=3)
    manifest, entries = module.validate_history(history)
    assert manifest["total_entries"] == 5
    assert [entry["sequence"] for entry in entries] == [3, 4, 5]
    assert manifest["anchor_previous_entry_hash"] == entries[0]["previous_entry_hash"]
    assert len(list(history.glob("*.json"))) == 4  # manifest plus three entries
    pruned_duplicate = tmp_path / "pruned-duplicate.json"
    _write(pruned_duplicate, _source(module, start, 0))
    with pytest.raises(ValueError, match="DUPLICATE_SNAPSHOT"):
        module.append(history, pruned_duplicate, kind="PHASE4AA_GATE", retention=3)


def test_manifest_tampering_fails_closed(tmp_path: Path) -> None:
    module = _module()
    source = tmp_path / "source.json"
    _write(source, _source(module, datetime(2026, 8, 25, 18, tzinfo=UTC), 1))
    history = tmp_path / "history"
    module.append(history, source, kind="PHASE4AA_GATE", retention=10)
    path = history / "manifest.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["total_entries"] = 500
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="MANIFEST_HASH_MISMATCH"):
        module.validate_history(history)
