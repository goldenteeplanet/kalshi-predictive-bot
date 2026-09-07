"""Normalize a strictly read-only GitHub integration inventory snapshot."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

INPUT_SCHEMA = "phase4fa.inventory-input.v1"
REPORT_SCHEMA = "phase4fa.inventory-report.v1"
REMOTE_FIELDS = {
    "visibility",
    "active_workflow_count",
    "webhooks",
    "environments",
    "actions_secret_names",
    "dependabot_secret_names",
    "branch_protected",
    "rulesets",
    "required_checks",
    "actions_enabled",
    "allowed_actions",
    "sha_pinning_required",
    "default_workflow_permissions",
    "can_approve_pull_request_reviews",
    "codeql_default_setup",
    "secret_scanning",
    "push_protection",
    "dependabot_security_updates",
    "apps_inventory_status",
}


def _hash(value: Any) -> str:
    if isinstance(value, dict):
        value = {key: item for key, item in value.items() if key != "artifact_hash"}
    return canonical_hash(value)


def _digest(value: Any, code: str) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise ValueError(code)
    try:
        int(value, 16)
    except ValueError as exc:
        raise ValueError(code) from exc
    return value


def _strings(value: Any, code: str) -> list[str]:
    if (
        not isinstance(value, list)
        or len(value) != len(set(value))
        or any(not isinstance(item, str) or not item or item.strip() != item for item in value)
    ):
        raise ValueError(code)
    return sorted(value)


def _time(value: Any) -> str:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("PHASE4FA_OBSERVED_AT_INVALID")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ValueError("PHASE4FA_OBSERVED_AT_INVALID") from exc
    if (
        parsed.tzinfo != UTC
        or parsed.isoformat(timespec="milliseconds").replace("+00:00", "Z") != value
    ):
        raise ValueError("PHASE4FA_OBSERVED_AT_INVALID")
    return value


def build_report(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != {
        "schema",
        "repository",
        "observed_at_utc",
        "local_workflows",
        "remote",
        "artifact_hash",
    }:
        raise ValueError("PHASE4FA_INPUT_FIELDS_INVALID")
    if payload.get("schema") != INPUT_SCHEMA or payload.get("artifact_hash") != _hash(payload):
        raise ValueError("PHASE4FA_INPUT_SCHEMA_OR_HASH_INVALID")
    repository = payload["repository"]
    if (
        not isinstance(repository, str)
        or repository.count("/") != 1
        or repository.strip() != repository
    ):
        raise ValueError("PHASE4FA_REPOSITORY_INVALID")
    observed_at = _time(payload["observed_at_utc"])
    workflows = payload["local_workflows"]
    if not isinstance(workflows, list):
        raise ValueError("PHASE4FA_WORKFLOWS_INVALID")
    normalized_workflows: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    for workflow in workflows:
        if not isinstance(workflow, dict) or set(workflow) != {
            "path",
            "sha256",
            "declared_permissions",
            "action_refs",
        }:
            raise ValueError("PHASE4FA_WORKFLOW_FIELDS_INVALID")
        path = workflow["path"]
        if (
            not isinstance(path, str)
            or not path.startswith(".github/workflows/")
            or Path(path).is_absolute()
            or ".." in Path(path).parts
            or path in seen_paths
        ):
            raise ValueError("PHASE4FA_WORKFLOW_PATH_INVALID")
        seen_paths.add(path)
        permissions = workflow["declared_permissions"]
        if not isinstance(permissions, dict) or any(
            not isinstance(key, str) or not key or value not in {"none", "read", "write"}
            for key, value in permissions.items()
        ):
            raise ValueError("PHASE4FA_WORKFLOW_PERMISSIONS_INVALID")
        normalized_workflows.append(
            {
                "path": path,
                "sha256": _digest(workflow["sha256"], "PHASE4FA_WORKFLOW_HASH_INVALID"),
                "declared_permissions": dict(sorted(permissions.items())),
                "action_refs": _strings(workflow["action_refs"], "PHASE4FA_ACTION_REFS_INVALID"),
            }
        )
    normalized_workflows.sort(key=lambda item: item["path"])

    remote = payload["remote"]
    if not isinstance(remote, dict) or set(remote) != REMOTE_FIELDS:
        raise ValueError("PHASE4FA_REMOTE_FIELDS_INVALID")
    for field in (
        "branch_protected",
        "actions_enabled",
        "sha_pinning_required",
        "can_approve_pull_request_reviews",
    ):
        if not isinstance(remote[field], bool):
            raise ValueError("PHASE4FA_REMOTE_STATUS_INVALID")
    count = remote["active_workflow_count"]
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise ValueError("PHASE4FA_REMOTE_WORKFLOW_COUNT_INVALID")
    if count != len(normalized_workflows):
        raise ValueError("PHASE4FA_LOCAL_REMOTE_WORKFLOW_COUNT_MISMATCH")
    normalized_remote = {
        **remote,
        "webhooks": _strings(remote["webhooks"], "PHASE4FA_WEBHOOKS_INVALID"),
        "environments": _strings(remote["environments"], "PHASE4FA_ENVIRONMENTS_INVALID"),
        "actions_secret_names": _strings(
            remote["actions_secret_names"], "PHASE4FA_ACTIONS_SECRETS_INVALID"
        ),
        "dependabot_secret_names": _strings(
            remote["dependabot_secret_names"], "PHASE4FA_DEPENDABOT_SECRETS_INVALID"
        ),
        "rulesets": _strings(remote["rulesets"], "PHASE4FA_RULESETS_INVALID"),
        "required_checks": _strings(remote["required_checks"], "PHASE4FA_REQUIRED_CHECKS_INVALID"),
    }
    if remote["visibility"] not in {"public", "private", "internal"}:
        raise ValueError("PHASE4FA_VISIBILITY_INVALID")
    if remote["allowed_actions"] not in {"all", "local_only", "selected"}:
        raise ValueError("PHASE4FA_ALLOWED_ACTIONS_INVALID")
    if remote["default_workflow_permissions"] not in {"read", "write"}:
        raise ValueError("PHASE4FA_DEFAULT_PERMISSIONS_INVALID")
    for field in (
        "codeql_default_setup",
        "secret_scanning",
        "push_protection",
        "dependabot_security_updates",
    ):
        if remote[field] not in {"enabled", "disabled", "not-configured"}:
            raise ValueError("PHASE4FA_SECURITY_STATUS_INVALID")
    if remote["apps_inventory_status"] not in {"VERIFIED", "UNVERIFIED_TOKEN_LIMITATION"}:
        raise ValueError("PHASE4FA_APPS_STATUS_INVALID")

    risks: list[str] = []
    if not remote["branch_protected"]:
        risks.append("BRANCH_PROTECTION_ABSENT")
    if not remote["rulesets"]:
        risks.append("RULESETS_ABSENT")
    if not remote["required_checks"]:
        risks.append("REQUIRED_CHECKS_ABSENT")
    if remote["secret_scanning"] != "enabled":
        risks.append("SECRET_SCANNING_DISABLED")
    if remote["push_protection"] != "enabled":
        risks.append("PUSH_PROTECTION_DISABLED")
    if remote["dependabot_security_updates"] != "enabled":
        risks.append("DEPENDABOT_SECURITY_UPDATES_DISABLED")
    if remote["codeql_default_setup"] != "enabled":
        risks.append("CODEQL_NOT_CONFIGURED")
    if remote["allowed_actions"] == "all":
        risks.append("ACTIONS_ALLOW_ALL")
    if not remote["sha_pinning_required"]:
        risks.append("ACTION_SHA_PINNING_NOT_REQUIRED")
    if any("@v" in ref for workflow in normalized_workflows for ref in workflow["action_refs"]):
        risks.append("FLOATING_ACTION_REFERENCES")
    limitations = []
    if remote["apps_inventory_status"] != "VERIFIED":
        risks.append("APP_INVENTORY_UNVERIFIED")
        limitations.append(
            "GitHub App installations could not be enumerated with the existing OAuth token type."
        )

    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "phase": "4FA",
        "input_hash": payload["artifact_hash"],
        "repository": repository,
        "observed_at_utc": observed_at,
        "local_workflows": normalized_workflows,
        "remote": normalized_remote,
        "risk_codes": risks,
        "limitations": limitations,
        "inventory_complete_except_explicit_limitations": True,
        "configuration_changed": False,
        "github_app_installed": False,
        "github_app_authorized": False,
        "repository_settings_changed": False,
        "secrets_read": False,
        "secret_values_read": False,
        "production_database_mutated": False,
        "services_controlled": False,
        "exchange_requests_made": False,
        "execution_authorized": False,
    }
    report["artifact_hash"] = _hash(report)
    return report


def publish(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = build_report(json.loads(args.input.read_text(encoding="utf-8")))
    publish(args.output, report)
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
