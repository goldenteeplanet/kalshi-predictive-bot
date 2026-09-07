from __future__ import annotations

import importlib.util
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest


def _module():
    path = Path(__file__).parents[1] / "scripts/local/phase4bh_offline_observability.py"
    spec = importlib.util.spec_from_file_location("phase4bh_tested", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


NOW = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)


def _fixture(tmp_path: Path):
    module = _module()
    tmp_path.mkdir(parents=True, exist_ok=True)
    events = [
        {
            "sequence": sequence,
            "event_type": event_type,
            "occurred_at": (NOW + timedelta(seconds=sequence)).isoformat(),
            "phase": "4BH",
            "subject_hash": module.canonical_hash({"event": event_type}),
            "reason_codes": [] if "REFUSAL" not in event_type else ["SAFE_REFUSAL"],
            "attributes": {"status": "safe", "count": sequence},
        }
        for sequence, event_type in enumerate(module.EVENT_TYPES, start=1)
    ]
    payload = {"schema": module.INPUT_SCHEMA, "events": events}
    payload["artifact_hash"] = module._hash(payload)
    path = tmp_path / "events.json"
    path.write_text(json.dumps(payload))
    return module, path


def _mutate(module, path: Path, mutate, *, rehash=True):
    payload = json.loads(path.read_text())
    mutate(payload)
    if rehash:
        payload["artifact_hash"] = module._hash(payload)
    path.write_text(json.dumps(payload))


def test_all_required_offline_events_are_hash_bound_and_non_sensitive(tmp_path: Path):
    module, path = _fixture(tmp_path)
    stream, manifest = module.build(path, now=NOW)
    assert [event["event_type"] for event in stream["events"]] == list(module.EVENT_TYPES)
    assert all(len(event["subject_hash"]) == 64 for event in stream["events"])
    assert all(event["contains_secrets"] is False for event in stream["events"])
    assert manifest["production_write_controls_present"] is False
    assert stream["artifact_hash"] == module._hash(stream)


@pytest.mark.parametrize("key", sorted(_module().FORBIDDEN_KEYS))
def test_each_sensitive_key_is_refused(tmp_path: Path, key: str):
    module, path = _fixture(tmp_path)
    _mutate(module, path, lambda p: p["events"][0]["attributes"].update({key: "value"}))
    with pytest.raises(ValueError, match="SENSITIVE_ATTRIBUTE_KEY"):
        module.build(path, now=NOW)


@pytest.mark.parametrize(
    "value",
    [
        "SELECT * FROM settlements",
        "UPDATE settlements SET result='YES'",
        "-----BEGIN PRIVATE KEY-----",
        ["nested", "values"],
        None,
    ],
)
def test_sql_credentials_and_complex_attribute_values_are_refused(tmp_path: Path, value):
    module, path = _fixture(tmp_path)
    _mutate(module, path, lambda p: p["events"][0]["attributes"].update(detail=value))
    with pytest.raises(ValueError, match="SENSITIVE_ATTRIBUTE_VALUE"):
        module.build(path, now=NOW)


def test_sequence_time_type_hash_reason_and_tampering_fail_closed(tmp_path: Path):
    cases = (
        ("sequence", lambda p: p["events"][0].update(sequence=2), "EVENT_SEQUENCE_INVALID"),
        (
            "time",
            lambda p: p["events"][1].update(occurred_at="2020-01-01T00:00:00Z"),
            "EVENT_TIME_REGRESSION",
        ),
        ("type", lambda p: p["events"][0].update(event_type="UNKNOWN"), "EVENT_TYPE_INVALID"),
        ("hash", lambda p: p["events"][0].update(subject_hash="short"), "SUBJECT_HASH_INVALID"),
        ("reason", lambda p: p["events"][0].update(reason_codes=[1]), "REASON_CODES_INVALID"),
    )
    for name, mutation, reason in cases:
        module, path = _fixture(tmp_path / name)
        _mutate(module, path, mutation)
        with pytest.raises(ValueError, match=reason):
            module.build(path, now=NOW)
    module, path = _fixture(tmp_path / "tamper")
    _mutate(module, path, lambda p: p.update(extra=True), rehash=False)
    with pytest.raises(ValueError, match="INPUT_SCHEMA_OR_HASH_INVALID"):
        module.build(path, now=NOW)


def test_naive_time_and_static_offline_surface(tmp_path: Path):
    module, path = _fixture(tmp_path)
    with pytest.raises(ValueError, match="EVALUATION_TIMEZONE_MISSING"):
        module.build(path, now=datetime(2026, 8, 25, 12, 0))
    source = (
        Path(__file__).parents[1] / "scripts/local/phase4bh_offline_observability.py"
    ).read_text()
    assert "sqlite3" not in source
    assert "subprocess" not in source
    assert "--production-db" not in source
