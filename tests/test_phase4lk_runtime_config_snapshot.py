from __future__ import annotations

import copy

import pytest

from scripts.local.phase4lk_runtime_config_snapshot import build_snapshot


def _sources():
    services = {
        "refresh": {
            "active": True,
            "enabled": True,
            "restart": "always",
            "restart_sec": 15,
            "n_restarts": 0,
            "environment": [],
        },
        "ui": {
            "active": True,
            "enabled": True,
            "restart": "always",
            "restart_sec": 5,
            "n_restarts": 0,
            "environment": [["UI_READ_ONLY", "true"], ["EXECUTION_ENABLED", "false"]],
        },
    }
    launcher = [
        ["EXECUTION_ENABLED", "false"],
        ["EXECUTION_DRY_RUN", "true"],
        ["EXECUTION_KILL_SWITCH", "true"],
        ["DEMO_EXECUTION_ENABLED", "false"],
        ["AUTOPILOT_ENABLED", "false"],
        ["AUTOPILOT_DRY_RUN", "true"],
        ["PAPER_ORDER_CREATION_ENABLED", "false"],
        ["PAPER_ORDER_KILL_SWITCH", "true"],
        ["KALSHI_REFRESH_INTERVAL_SECONDS", "900"],
    ]
    health = {
        "status": "healthy",
        "generated_at": "2026-08-28T23:00:00Z",
        "writer_count": 1,
        "writer_exclusive": True,
    }
    return services, launcher, health


def _snapshot(mutator=None):
    sources = list(_sources())
    if mutator:
        mutator(sources)
    return build_snapshot(*sources, observed_at="2026-08-28T23:10:00Z")


def test_safe_snapshot_passes_deterministically() -> None:
    first = _snapshot()
    assert first == _snapshot()
    assert first["verdict"] == "PASS"
    assert first["redaction"]["credentials_included"] is False


@pytest.mark.parametrize(
    "field,value",
    [
        ("EXECUTION_ENABLED", "true"),
        ("AUTOPILOT_ENABLED", "true"),
        ("PAPER_ORDER_CREATION_ENABLED", "true"),
        ("PAPER_ORDER_KILL_SWITCH", "false"),
    ],
)
def test_authority_broadening_refuses(field: str, value: str) -> None:
    def mutate(sources):
        for pair in sources[1]:
            if pair[0] == field:
                pair[1] = value

    assert _snapshot(mutate)["verdict"] == "REFUSE"


def test_duplicate_unknown_and_secret_fields_refuse() -> None:
    def mutate(sources):
        sources[1].extend(
            [["EXECUTION_ENABLED", "false"], ["EXEC_ENABLED", "false"], ["API_TOKEN", "secret"]]
        )

    errors = _snapshot(mutate)["errors"]
    assert any("DUPLICATE_FIELD" in error for error in errors)
    assert any("UNKNOWN_FIELD" in error for error in errors)
    assert any("SECRET_LIKE_FIELD" in error for error in errors)


def test_service_restart_and_ui_read_only_refuse() -> None:
    def mutate(sources):
        sources[0]["refresh"]["restart"] = "no"
        sources[0]["ui"]["environment"][0][1] = "false"

    errors = _snapshot(mutate)["errors"]
    assert "SERVICE_REFRESH:RESTART_PROTECTION_DISABLED" in errors
    assert "SERVICE_UI:NOT_READ_ONLY" in errors


def test_stale_or_future_health_refuses() -> None:
    def stale(sources):
        sources[2]["generated_at"] = "2026-08-28T22:00:00Z"

    assert "HEALTH_EVIDENCE_STALE" in _snapshot(stale)["errors"]

    def future(sources):
        sources[2]["generated_at"] = "2026-08-28T23:11:00Z"

    assert "HEALTH_EVIDENCE_STALE" in _snapshot(future)["errors"]


def test_multiple_or_unverified_writer_refuses() -> None:
    def mutate(sources):
        sources[2]["writer_count"] = 2
        sources[2]["writer_exclusive"] = False

    assert "WRITER_EXCLUSIVITY_FAILED" in _snapshot(mutate)["errors"]


def test_unsafe_cadence_and_invalid_boolean_refuse() -> None:
    def mutate(sources):
        sources[1][-1][1] = "5"
        sources[1][0][1] = "off"

    errors = _snapshot(mutate)["errors"]
    assert "UNSAFE_REFRESH_CADENCE" in errors
    assert "INVALID_BOOLEAN:EXECUTION_ENABLED" in errors


def test_unknown_service_or_health_field_refuses() -> None:
    def mutate(sources):
        sources[0]["ui"]["command"] = "unsafe"
        sources[2]["extra"] = True

    errors = _snapshot(mutate)["errors"]
    assert "SERVICE_UI:UNKNOWN_OR_MALFORMED_FIELD" in errors
    assert "HEALTH_SOURCE_UNKNOWN_FIELD" in errors


def test_input_objects_are_not_mutated() -> None:
    services, launcher, health = _sources()
    before = copy.deepcopy((services, launcher, health))
    build_snapshot(services, launcher, health, observed_at="2026-08-28T23:10:00Z")
    assert (services, launcher, health) == before
