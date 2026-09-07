from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

SCRIPT = Path(__file__).parents[1] / "scripts/local/phase4fa_github_integration_inventory.py"
SPEC = importlib.util.spec_from_file_location("phase4fa", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _seal(value):
    value.pop("artifact_hash", None)
    value["artifact_hash"] = canonical_hash(value)
    return value


def payload():
    workflows = [
        {
            "path": ".github/workflows/a.yml",
            "sha256": "a" * 64,
            "declared_permissions": {"contents": "read"},
            "action_refs": ["actions/checkout@v4", "actions/setup-python@v5"],
        }
    ]
    return _seal(
        {
            "schema": MODULE.INPUT_SCHEMA,
            "repository": "owner/repo",
            "observed_at_utc": "2026-08-26T12:00:00.000Z",
            "local_workflows": workflows,
            "remote": {
                "visibility": "public",
                "active_workflow_count": 1,
                "webhooks": [],
                "environments": [],
                "actions_secret_names": [],
                "dependabot_secret_names": [],
                "branch_protected": False,
                "rulesets": [],
                "required_checks": [],
                "actions_enabled": True,
                "allowed_actions": "all",
                "sha_pinning_required": False,
                "default_workflow_permissions": "read",
                "can_approve_pull_request_reviews": False,
                "codeql_default_setup": "not-configured",
                "secret_scanning": "disabled",
                "push_protection": "disabled",
                "dependabot_security_updates": "disabled",
                "apps_inventory_status": "UNVERIFIED_TOKEN_LIMITATION",
            },
        }
    )


def test_inventory_preserves_observations_and_explicit_limitation():
    report = MODULE.build_report(payload())
    assert report["repository"] == "owner/repo"
    assert len(report["local_workflows"]) == 1
    assert report["remote"]["default_workflow_permissions"] == "read"
    assert report["limitations"]
    assert "APP_INVENTORY_UNVERIFIED" in report["risk_codes"]
    assert report["inventory_complete_except_explicit_limitations"] is True


@pytest.mark.parametrize(
    "risk",
    [
        "BRANCH_PROTECTION_ABSENT",
        "RULESETS_ABSENT",
        "REQUIRED_CHECKS_ABSENT",
        "SECRET_SCANNING_DISABLED",
        "PUSH_PROTECTION_DISABLED",
        "DEPENDABOT_SECURITY_UPDATES_DISABLED",
        "CODEQL_NOT_CONFIGURED",
        "ACTIONS_ALLOW_ALL",
        "ACTION_SHA_PINNING_NOT_REQUIRED",
        "FLOATING_ACTION_REFERENCES",
    ],
)
def test_each_observed_risk_is_reported(risk):
    assert risk in MODULE.build_report(payload())["risk_codes"]


def test_verified_hardened_configuration_has_no_risks():
    value = payload()
    remote = value["remote"]
    remote.update(
        branch_protected=True,
        rulesets=["main"],
        required_checks=["safety"],
        secret_scanning="enabled",
        push_protection="enabled",
        dependabot_security_updates="enabled",
        codeql_default_setup="enabled",
        allowed_actions="selected",
        sha_pinning_required=True,
        apps_inventory_status="VERIFIED",
    )
    value["local_workflows"][0]["action_refs"] = ["actions/checkout@" + "a" * 40]
    _seal(value)
    report = MODULE.build_report(value)
    assert report["risk_codes"] == []
    assert report["limitations"] == []


def test_local_remote_workflow_count_mismatch_fails_closed():
    value = payload()
    value["remote"]["active_workflow_count"] = 2
    _seal(value)
    with pytest.raises(ValueError, match="COUNT_MISMATCH"):
        MODULE.build_report(value)


def test_duplicate_workflow_path_fails_closed():
    value = payload()
    value["local_workflows"].append(dict(value["local_workflows"][0]))
    value["remote"]["active_workflow_count"] = 2
    _seal(value)
    with pytest.raises(ValueError, match="WORKFLOW_PATH"):
        MODULE.build_report(value)


@pytest.mark.parametrize("field", MODULE.REMOTE_FIELDS)
def test_missing_remote_field_fails_closed(field):
    value = payload()
    value["remote"].pop(field)
    _seal(value)
    with pytest.raises(ValueError, match="REMOTE_FIELDS"):
        MODULE.build_report(value)


def test_bad_workflow_hash_fails_closed():
    value = payload()
    value["local_workflows"][0]["sha256"] = "bad"
    _seal(value)
    with pytest.raises(ValueError, match="WORKFLOW_HASH"):
        MODULE.build_report(value)


def test_noncanonical_time_fails_closed():
    value = payload()
    value["observed_at_utc"] = "2026-08-26T12:00:00Z"
    _seal(value)
    with pytest.raises(ValueError, match="OBSERVED_AT"):
        MODULE.build_report(value)


def test_input_order_is_normalized():
    value = payload()
    value["local_workflows"][0]["action_refs"].reverse()
    _seal(value)
    report = MODULE.build_report(value)
    assert report["local_workflows"][0]["action_refs"] == sorted(
        report["local_workflows"][0]["action_refs"]
    )


def test_input_tampering_fails_closed():
    value = payload()
    value["remote"]["branch_protected"] = True
    with pytest.raises(ValueError, match="SCHEMA_OR_HASH"):
        MODULE.build_report(value)


def test_atomic_publication(tmp_path):
    report = MODULE.build_report(payload())
    output = tmp_path / "inventory.json"
    MODULE.publish(output, report)
    assert json.loads(output.read_text()) == report
    assert not list(tmp_path.glob(".*"))


def test_inventory_never_changes_github_or_runtime():
    report = MODULE.build_report(payload())
    for field in (
        "configuration_changed",
        "github_app_installed",
        "github_app_authorized",
        "repository_settings_changed",
        "secrets_read",
        "secret_values_read",
        "production_database_mutated",
        "services_controlled",
        "exchange_requests_made",
        "execution_authorized",
    ):
        assert report[field] is False


def test_source_has_no_github_write_or_connected_surface():
    source = SCRIPT.read_text()
    for token in (
        "gh api",
        "requests.",
        "urllib.",
        "graphql",
        "--method POST",
        "--method PATCH",
        "--method PUT",
        "--method DELETE",
        "subprocess",
        "/home/james",
    ):
        assert token not in source
