"""Build an approval-gated, deterministic GitHub integration recommendation report."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

INPUT_SCHEMA = "phase4fb.recommendation-input.v1"
REPORT_SCHEMA = "phase4fb.recommendation-report.v1"
REQUIRED = (
    "openai_codex_github",
    "github_actions",
    "dependabot",
    "codeql",
    "secret_scanning_push_protection",
    "artifact_attestations",
    "repository_rulesets_required_checks",
    "slack_notifications",
    "teams_notifications",
    "sentry",
    "datadog",
    "pagerduty",
    "renovate",
)
FIELDS = {
    "id",
    "availability",
    "purpose",
    "required_permissions",
    "scope",
    "data_leaving_repository",
    "secret_requirements",
    "cost_plan_uncertainty",
    "security_risks",
    "reversibility",
    "recommendation",
    "source_urls",
    "explicit_approval_required",
}
AVAILABILITY = {"AVAILABLE", "UNAVAILABLE", "UNKNOWN"}
SCOPES = {"READ_ONLY", "REPOSITORY_WRITE", "EXTERNAL_WRITE", "MIXED"}
DECISIONS = {
    "KEEP_AND_HARDEN",
    "RECOMMEND_ENABLE_WITH_APPROVAL",
    "RECOMMEND_REPOSITORY_LOCAL_CONFIG",
    "VERIFY_BEFORE_APPROVAL",
    "OPTIONAL_DEFER",
    "DEFER_UNTIL_NEEDED",
    "DEFER_DEPENDABOT_FIRST",
}


def _hash(value: Any) -> str:
    if isinstance(value, dict):
        value = {k: v for k, v in value.items() if k != "artifact_hash"}
    return canonical_hash(value)


def _strings(value: Any, code: str) -> list[str]:
    if (
        not isinstance(value, list)
        or not value
        or len(value) != len(set(value))
        or any(not isinstance(v, str) or not v or v.strip() != v for v in value)
    ):
        raise ValueError(code)
    return sorted(value)


def build_report(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != {
        "schema",
        "repository",
        "inventory_hash",
        "recommendations",
        "artifact_hash",
    }:
        raise ValueError("PHASE4FB_INPUT_FIELDS_INVALID")
    if payload["schema"] != INPUT_SCHEMA or payload["artifact_hash"] != _hash(payload):
        raise ValueError("PHASE4FB_INPUT_SCHEMA_OR_HASH_INVALID")
    repository = payload["repository"]
    if not isinstance(repository, str) or repository.count("/") != 1:
        raise ValueError("PHASE4FB_REPOSITORY_INVALID")
    inventory_hash = payload["inventory_hash"]
    if not isinstance(inventory_hash, str) or len(inventory_hash) != 64:
        raise ValueError("PHASE4FB_INVENTORY_HASH_INVALID")
    try:
        int(inventory_hash, 16)
    except ValueError as exc:
        raise ValueError("PHASE4FB_INVENTORY_HASH_INVALID") from exc
    values = payload["recommendations"]
    if not isinstance(values, list) or {v.get("id") for v in values if isinstance(v, dict)} != set(
        REQUIRED
    ):
        raise ValueError("PHASE4FB_INTEGRATION_SET_INVALID")
    normalized = []
    for value in values:
        if not isinstance(value, dict) or set(value) != FIELDS:
            raise ValueError("PHASE4FB_RECOMMENDATION_FIELDS_INVALID")
        if value["availability"] not in AVAILABILITY or value["scope"] not in SCOPES:
            raise ValueError("PHASE4FB_ENUM_INVALID")
        if value["recommendation"] not in DECISIONS:
            raise ValueError("PHASE4FB_DECISION_INVALID")
        if value["explicit_approval_required"] is not True:
            raise ValueError("PHASE4FB_APPROVAL_GATE_REQUIRED")
        for field in ("purpose", "cost_plan_uncertainty", "reversibility"):
            if not isinstance(value[field], str) or not value[field].strip():
                raise ValueError("PHASE4FB_TEXT_INVALID")
        item = dict(value)
        for field in (
            "required_permissions",
            "data_leaving_repository",
            "secret_requirements",
            "security_risks",
            "source_urls",
        ):
            item[field] = _strings(value[field], "PHASE4FB_LIST_INVALID")
        if any(not url.startswith("https://") for url in item["source_urls"]):
            raise ValueError("PHASE4FB_SOURCE_URL_INVALID")
        normalized.append(item)
    normalized.sort(key=lambda x: REQUIRED.index(x["id"]))
    report = {
        "schema": REPORT_SCHEMA,
        "phase": "4FB",
        "repository": repository,
        "inventory_hash": inventory_hash,
        "input_hash": payload["artifact_hash"],
        "recommendations": normalized,
        "approval_gate": "EXPLICIT_HUMAN_APPROVAL_REQUIRED",
        "integration_changes_made": False,
        "apps_installed": False,
        "apps_authorized": False,
        "repository_settings_changed": False,
        "secrets_created_or_read": False,
        "production_database_mutated": False,
        "services_controlled": False,
        "exchange_requests_made": False,
        "execution_authorized": False,
    }
    report["artifact_hash"] = _hash(report)
    return report


def publish(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp = Path(temporary)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    publish(args.output, build_report(json.loads(args.input.read_text(encoding="utf-8"))))


if __name__ == "__main__":
    main()
