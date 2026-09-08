from __future__ import annotations

import copy
import hashlib
import json

from scripts.local.phase4lm_runtime_observation_ledger import build_ledger


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _classification(kind: str = "none", severity: str = "BENIGN") -> dict[str, object]:
    value: dict[str, object] = {
        "schema": "phase4ll.runtime-snapshot-drift-classification.v1",
        "severity": severity,
        "action": "QUIET",
        "candidate_snapshot_sha256": "a" * 64,
        "event": {"kind": kind, "recurrence_count": 0, "duration_seconds": 0},
    }
    value["classification_sha256"] = _digest(value)
    return value


def _observation(at: str, kind: str = "none") -> dict[str, object]:
    return {"observed_at": at, "classification": _classification(kind)}


def test_orders_deduplicates_and_hash_chains_records() -> None:
    late = _observation("2026-08-28T20:01:00Z", "wsl_unresponsive")
    early = _observation("2026-08-28T20:00:00Z", "wsl_unresponsive")
    result = build_ledger([late, early, copy.deepcopy(early)])
    assert result["verdict"] == "PASS"
    assert result["summary"]["deduplicated_count"] == 2
    assert [row["sequence"] for row in result["records"]] == [1, 2]
    assert result["records"][1]["previous_record_sha256"] == result["records"][0]["record_sha256"]


def test_tracks_consecutive_and_rolling_recurrence() -> None:
    observations = [
        _observation("2026-08-28T20:00:00Z", "wsl_unresponsive"),
        _observation("2026-08-28T20:01:00Z", "wsl_unresponsive"),
        _observation("2026-08-28T20:02:00Z", "none"),
        _observation("2026-08-28T20:03:00Z", "wsl_unresponsive"),
    ]
    result = build_ledger(observations)
    assert [row["consecutive_recurrence"] for row in result["records"]] == [1, 2, 0, 1]
    assert result["summary"]["rolling_recurrence"]["wsl_unresponsive"] == 3


def test_bad_hash_fails_closed_and_is_not_accepted() -> None:
    observation = _observation("2026-08-28T20:00:00Z")
    observation["classification"]["severity"] = "CRITICAL"
    result = build_ledger([observation])
    assert result["verdict"] == "REFUSE"
    assert result["records"] == []
    assert "OBSERVATION_0:HASH_MISMATCH" in result["errors"]


def test_bad_time_schema_and_input_shape_refuse() -> None:
    bad_time = _observation("yesterday")
    bad_schema = _observation("2026-08-28T20:00:00Z")
    bad_schema["classification"]["schema"] = "wrong"
    assert build_ledger([bad_time])["verdict"] == "REFUSE"
    assert build_ledger([bad_schema])["verdict"] == "REFUSE"
    assert build_ledger({})["verdict"] == "REFUSE"


def test_output_is_deterministic_and_incapable_of_actions() -> None:
    observations = [_observation("2026-08-28T20:00:00Z")]
    first = build_ledger(observations)
    assert first == build_ledger(observations)
    assert all(
        value is False for key, value in first["safety"].items() if key != "append_only_model"
    )
