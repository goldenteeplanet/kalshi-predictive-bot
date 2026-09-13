import hashlib
import importlib.util
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "cadence", Path(__file__).parents[1] / "scripts/nyc_w8_shadow_cadence.py"
)
assert spec and spec.loader
cadence = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cadence)


def state():
    return {
        "started_at": "2026-08-23T02:00:00+00:00",
        "consumed_source_reports": ["/reports/one.json", "/reports/two.json"],
        "mode": "READ_ONLY_DISABLED_FEATURE_FLAG",
    }


def test_atomic_roundtrip_preserves_start_and_consumed(tmp_path):
    path = tmp_path / "state.json"
    cadence._save_state(path, state())
    assert cadence._load_or_start(path) == state()
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("raw", [b"", b"{", b"null", b"[]"])
def test_corrupt_original_never_reset(tmp_path, raw):
    path = tmp_path / "state.json"
    path.write_bytes(raw)
    with pytest.raises(ValueError, match="PRESERVE_ORIGINAL"):
        cadence._load_or_start(path)
    assert path.read_bytes() == raw


def test_failed_replace_keeps_old_identities(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    original = json.dumps(state()).encode()
    path.write_bytes(original)
    updated = state()
    updated["consumed_source_reports"].append("/reports/three.json")

    def fail(*args):
        raise OSError("injected replace failure")

    monkeypatch.setattr(cadence.os, "replace", fail)
    with pytest.raises(OSError):
        cadence._save_state(path, updated)
    assert path.read_bytes() == original
    assert list(tmp_path.iterdir()) == [path]


def test_failed_fsync_keeps_old_identities(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    original = json.dumps(state()).encode()
    path.write_bytes(original)

    def fail(*args):
        raise OSError("injected fsync failure")

    monkeypatch.setattr(cadence.os, "fsync", fail)
    with pytest.raises(OSError):
        cadence._save_state(path, state())
    assert path.read_bytes() == original


@pytest.mark.parametrize("field,value", [
    ("started_at", "2026-08-23T02:00:00"),
    ("consumed_source_reports", "not a list"),
    ("consumed_source_reports", [False]),
])
def test_malformed_state_refused_without_mutation(tmp_path, field, value):
    path = tmp_path / "state.json"
    invalid = state()
    invalid[field] = value
    raw = json.dumps(invalid).encode()
    path.write_bytes(raw)
    with pytest.raises(ValueError):
        cadence._load_or_start(path)
    assert path.read_bytes() == raw


def test_explicit_recovery_archives_exact_bytes_and_new_namespace(tmp_path):
    path = tmp_path / "state.json"
    raw = b"broken original\x00"
    path.write_bytes(raw)
    before = datetime.now(UTC)
    recovered = cadence._recover_corrupt_state(path)
    assert datetime.fromisoformat(recovered["started_at"]) >= before
    assert recovered["consumed_source_reports"] == []
    assert recovered["historical_consumption_status"] == "UNKNOWN_NOT_RECONSTRUCTED"
    namespace = cadence._census_directory(tmp_path, tmp_path / "old", recovered)
    assert namespace / "corrupt-state.original" != path
    assert (namespace / "corrupt-state.original").read_bytes() == raw
    receipt = json.loads((namespace / "archive.receipt.json").read_bytes())
    assert receipt["sha256"] == hashlib.sha256(raw).hexdigest()
    assert datetime.fromisoformat(receipt["original_recorded_at"]) <= datetime.fromisoformat(
        recovered["started_at"]
    )
    assert cadence._load_or_start(path) == recovered
    with pytest.raises(ValueError, match="MUST_NOT_RESET"):
        cadence._recover_corrupt_state(path)


def test_recovery_archive_failure_preserves_corrupt_state(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    path.write_bytes(b"")

    def fail(*args):
        raise OSError("archive unavailable")

    monkeypatch.setattr(cadence, "_write_exclusive", fail)
    with pytest.raises(OSError):
        cadence._recover_corrupt_state(path)
    assert path.read_bytes() == b""


def test_recovery_census_excludes_historical_and_future_sources(tmp_path, monkeypatch):
    output = tmp_path / "out"
    output.mkdir()
    path = output / "cadence_state.json"
    recovered = state()
    epoch = datetime.now(UTC) - timedelta(minutes=2)
    recovered.update(started_at=epoch.isoformat(), recovery_epoch="a" * 32,
                     historical_consumption_status="UNKNOWN_NOT_RECONSTRUCTED")
    cadence._save_state(path, recovered)
    for label, generated, target in [
        ("old", epoch - timedelta(seconds=1), epoch + timedelta(hours=1)),
        ("oldtarget", epoch + timedelta(seconds=1), epoch),
        ("futureclock", epoch + timedelta(days=1), epoch + timedelta(days=2)),
        ("new", epoch + timedelta(seconds=1), epoch + timedelta(hours=1)),
    ]:
        folder = tmp_path / f"phase_nyc_w4_{label}"
        folder.mkdir()
        (folder / "nyc_w4_observation_feature_integration_preview.json").write_text(json.dumps({
            "generated_at": generated.isoformat(),
            "rows": [{"preview_passed": True, "target_utc_time": target.isoformat()}],
        }))
    seen = []
    censuses = []
    monkeypatch.setattr(cadence, "write_shadow_runtime_report", lambda **kw: seen.append(kw))
    monkeypatch.setattr(cadence, "write_nyc_w8_report", lambda **kw: censuses.append(kw))
    monkeypatch.setattr(sys, "argv", ["cadence", "--reports-dir", str(tmp_path),
                                    "--output-dir", str(output)])
    cadence.main()
    assert len(seen) == 1
    assert "phase_nyc_w4_new" in str(seen[0]["source_paths"][0])
    assert seen[0]["output_dir"].parent == output / "recovery_epochs" / ("a" * 32)
    assert censuses[0]["reports_dir"] == seen[0]["output_dir"].parent
    assert censuses[0]["output_dir"] == censuses[0]["reports_dir"]
    cadence.main()
    assert len(seen) == 1  # Consumed identity retained on the next invocation.
