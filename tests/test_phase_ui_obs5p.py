from __future__ import annotations

import csv
import json
from pathlib import Path

from kalshi_predictor.phase_ui_obs5p import certify_timeline_export, verify_timeline_bundle


def test_exports_are_deterministic_and_cross_format_consistent(tmp_path: Path) -> None:
    history = _write_history(tmp_path / "history.json")
    first = certify_timeline_export(history, tmp_path / "first")
    second = certify_timeline_export(history, tmp_path / "second")
    assert first == second
    assert first["status"] == "PASSED"
    assert first["failures"] == []
    assert len(first["bundle_sha256"]) == 64
    timeline = json.loads((tmp_path / "first/certification_timeline.json").read_text())
    with (tmp_path / "first/certification_timeline.csv").open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == timeline["event_count"] == first["transition_count"]
    assert verify_timeline_bundle(tmp_path / "first")["status"] == "PASSED"


def test_tampered_export_fails_hash_gate(tmp_path: Path) -> None:
    history = _write_history(tmp_path / "history.json")
    output = tmp_path / "bundle"
    certify_timeline_export(history, output)
    (output / "certification_timeline.csv").write_text("tampered\n", encoding="utf-8")
    verification = verify_timeline_bundle(output)
    assert verification["status"] == "FAILED"
    assert "CSV_EXPORT_HASH_MISMATCH" in verification["failures"]
    assert "BUNDLE_HASH_MISMATCH" in verification["failures"]


def test_retention_overflow_and_duplicate_digest_fail_closed(tmp_path: Path) -> None:
    history = _write_history(tmp_path / "history.json")
    payload = json.loads(history.read_text())
    payload["retention_limit"] = 3
    payload["entries"].append(dict(payload["entries"][-1]))
    history.write_text(json.dumps(payload), encoding="utf-8")
    manifest = certify_timeline_export(history, tmp_path / "failed")
    assert manifest["status"] == "FAILED"
    assert "RETENTION_LIMIT_EXCEEDED" in manifest["failures"]
    assert "SNAPSHOT_DIGEST_DUPLICATE" in manifest["failures"]
    assert verify_timeline_bundle(tmp_path / "failed")["status"] == "FAILED"


def test_missing_manifest_fails_closed(tmp_path: Path) -> None:
    assert verify_timeline_bundle(tmp_path)["failures"] == ["MANIFEST_MISSING_OR_INVALID"]


def _write_history(path: Path) -> Path:
    entries = [
        _entry("2026-07-19T20:00:00+00:00", "a", ("WAITING",) * 4, ["ALERT-A"]),
        _entry(
            "2026-07-19T20:01:00+00:00",
            "b",
            ("PASSED", "RUNNING", "WAITING", "WAITING"),
            ["ALERT-A"],
        ),
        _entry("2026-07-19T20:02:00+00:00", "c", ("PASSED",) * 4, []),
    ]
    path.write_text(
        json.dumps({"schema_version": 1, "retention_limit": 3, "entries": entries}),
        encoding="utf-8",
    )
    return path


def _entry(timestamp: str, digest: str, states: tuple[str, ...], alerts: list[str]) -> dict:
    return {
        "generated_at": timestamp,
        "snapshot_digest": digest * 64,
        "prov14b": {
            "state": "PASSED" if all(state == "PASSED" for state in states) else "RUNNING",
            "current_stage": "certification",
            "gates": {
                gate: {
                    "state": state,
                    "evidence_stale": False,
                    "evidence_age_seconds": 0,
                }
                for gate, state in zip(("R2A", "R2B", "R2C", "R2D"), states, strict=True)
            },
            "alerts": alerts,
        },
    }
