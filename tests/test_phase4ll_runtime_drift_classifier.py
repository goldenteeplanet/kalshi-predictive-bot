from __future__ import annotations

import copy
import hashlib
import json

import pytest

from scripts.local.phase4ll_runtime_drift_classifier import classify_drift


def _snapshot():
    value = {
        "schema": "phase4lk.runtime-configuration-snapshot.v1",
        "verdict": "PASS",
        "observed_at": "2026-08-29T00:28:00Z",
        "settings": {
            "execution_enabled": False,
            "execution_dry_run": True,
            "execution_kill_switch": True,
            "demo_execution_enabled": False,
            "autopilot_enabled": False,
            "autopilot_dry_run": True,
            "paper_order_creation_enabled": False,
            "paper_order_kill_switch": True,
            "refresh_interval_seconds": 900,
        },
        "services": {
            "refresh": {
                "active": True,
                "enabled": True,
                "restart": "always",
                "restart_sec": 15,
                "n_restarts": 0,
                "ui_read_only": None,
            },
            "ui": {
                "active": True,
                "enabled": True,
                "restart": "always",
                "restart_sec": 5,
                "n_restarts": 0,
                "ui_read_only": True,
            },
        },
        "health": {
            "status": "healthy",
            "age_seconds": 172,
            "writer_count": 1,
            "writer_exclusive": True,
        },
        "errors": [],
        "redaction": {
            "allowlisted_fields_only": True,
            "secret_like_fields_rejected": True,
            "database_url_included": False,
            "credentials_included": False,
        },
        "safety": {
            "read_only": True,
            "service_control": False,
            "database_access": False,
            "network_access": False,
            "order_capability": False,
        },
    }
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    value["snapshot_sha256"] = hashlib.sha256(canonical).hexdigest()
    return value


def _rehash(value):
    value.pop("snapshot_sha256", None)
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    value["snapshot_sha256"] = hashlib.sha256(canonical).hexdigest()
    return value


def test_no_drift_is_benign_and_quiet() -> None:
    snapshot = _snapshot()
    result = classify_drift(snapshot, copy.deepcopy(snapshot))
    assert (result["severity"], result["action"]) == ("BENIGN", "QUIET")


def test_short_first_ui_startup_delay_is_transient() -> None:
    snapshot = _snapshot()
    result = classify_drift(
        snapshot,
        snapshot,
        event_kind="ui_listener_unavailable",
        recurrence_count=1,
        duration_seconds=20,
    )
    assert (result["severity"], result["action"]) == ("EXPECTED_TRANSIENT", "MONITOR")


def test_repeated_ui_failure_escalates_with_hysteresis() -> None:
    snapshot = _snapshot()
    second = classify_drift(
        snapshot,
        snapshot,
        event_kind="ui_listener_unavailable",
        recurrence_count=2,
        duration_seconds=45,
    )
    third = classify_drift(
        snapshot,
        snapshot,
        event_kind="ui_listener_unavailable",
        recurrence_count=3,
        duration_seconds=45,
    )
    assert (second["severity"], second["action"]) == ("WARNING", "RESTART_SERVICE_ELIGIBLE")
    assert (third["severity"], third["action"]) == ("CRITICAL", "HUMAN_INTERVENTION_REQUIRED")


def test_wsl_recurrence_escalates_to_human() -> None:
    snapshot = _snapshot()
    first = classify_drift(snapshot, snapshot, event_kind="wsl_unresponsive", recurrence_count=1)
    repeated = classify_drift(snapshot, snapshot, event_kind="wsl_unresponsive", recurrence_count=2)
    assert first["action"] == "RESTART_WSL_ELIGIBLE"
    assert repeated["action"] == "HUMAN_INTERVENTION_REQUIRED"


@pytest.mark.parametrize(
    "field,value",
    [
        ("execution_enabled", True),
        ("paper_order_kill_switch", False),
        ("paper_order_creation_enabled", True),
    ],
)
def test_authority_broadening_is_critical(field: str, value: bool) -> None:
    baseline = _snapshot()
    candidate = copy.deepcopy(baseline)
    candidate["settings"][field] = value
    candidate["verdict"] = "REFUSE"
    _rehash(candidate)
    result = classify_drift(baseline, candidate)
    assert result["severity"] == "CRITICAL"
    assert result["action"] == "HUMAN_INTERVENTION_REQUIRED"


def test_stopped_service_is_restart_eligible_then_human() -> None:
    baseline = _snapshot()
    candidate = copy.deepcopy(baseline)
    candidate["services"]["ui"]["active"] = False
    candidate["verdict"] = "REFUSE"
    _rehash(candidate)
    assert (
        classify_drift(baseline, candidate, recurrence_count=1)["action"]
        == "RESTART_SERVICE_ELIGIBLE"
    )
    assert (
        classify_drift(baseline, candidate, recurrence_count=3)["action"]
        == "HUMAN_INTERVENTION_REQUIRED"
    )


def test_health_age_movement_is_transient_then_warning() -> None:
    baseline = _snapshot()
    fresh = copy.deepcopy(baseline)
    fresh["health"]["age_seconds"] = 500
    _rehash(fresh)
    assert classify_drift(baseline, fresh)["severity"] == "EXPECTED_TRANSIENT"
    aging = copy.deepcopy(baseline)
    aging["health"]["age_seconds"] = 1_200
    _rehash(aging)
    assert classify_drift(baseline, aging)["severity"] == "WARNING"


def test_restart_counter_change_warns() -> None:
    baseline = _snapshot()
    candidate = copy.deepcopy(baseline)
    candidate["services"]["ui"]["n_restarts"] = 1
    _rehash(candidate)
    assert classify_drift(baseline, candidate)["action"] == "ALERT"


def test_hash_schema_and_field_envelope_failures_are_invalid() -> None:
    baseline = _snapshot()
    candidate = copy.deepcopy(baseline)
    candidate["extra"] = "secret"
    result = classify_drift(baseline, candidate)
    assert result["severity"] == "INVALID_EVIDENCE"
    assert result["action"] == "HUMAN_INTERVENTION_REQUIRED"


def test_classifier_has_no_action_capability() -> None:
    result = classify_drift(_snapshot(), _snapshot())
    assert all(
        value is False for key, value in result["safety"].items() if key != "classification_only"
    )


def test_malformed_health_age_fails_closed_without_type_error() -> None:
    baseline = _snapshot()
    candidate = copy.deepcopy(baseline)
    candidate["health"]["age_seconds"] = None
    candidate["verdict"] = "REFUSE"
    _rehash(candidate)
    result = classify_drift(baseline, candidate)
    assert result["severity"] == "CRITICAL"
    assert "HEALTH_UNSAFE_OR_STALE" in result["unsafe_reasons"]
