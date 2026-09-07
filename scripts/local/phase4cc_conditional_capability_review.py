"""Review authoritative conditional and delta capability evidence without changing collectors."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

INPUT_SCHEMA = "phase4cc.authoritative-capability-evidence.v1"
REPORT_SCHEMA = "phase4cc.conditional-request-proposal.v1"
MECHANISMS = ("ETAG", "LAST_MODIFIED", "CURSOR", "DELTA")
STATUSES = ("SUPPORTED", "UNSUPPORTED", "UNDOCUMENTED")


def _hash(payload: Any) -> str:
    if isinstance(payload, dict):
        payload = {key: value for key, value in payload.items() if key != "artifact_hash"}
    return canonical_hash(payload)


def build_proposal(evidence: dict[str, Any]) -> dict[str, Any]:
    if evidence.get("schema") != INPUT_SCHEMA or evidence.get("artifact_hash") != _hash(evidence):
        raise ValueError("PHASE4CC_INPUT_SCHEMA_OR_HASH_INVALID")
    providers = evidence.get("providers")
    if not isinstance(providers, list) or not providers:
        raise ValueError("PHASE4CC_PROVIDERS_MISSING")
    names = [row.get("provider") for row in providers if isinstance(row, dict)]
    if names != sorted(names) or len(names) != len(set(names)):
        raise ValueError("PHASE4CC_PROVIDER_ORDER_OR_DUPLICATE_INVALID")
    proposals = []
    for provider in providers:
        if set(provider) != {"provider", "capabilities"}:
            raise ValueError("PHASE4CC_PROVIDER_FIELDS_INVALID")
        capabilities = provider["capabilities"]
        if not isinstance(capabilities, list):
            raise ValueError("PHASE4CC_CAPABILITIES_MISSING")
        mechanisms = [row.get("mechanism") for row in capabilities if isinstance(row, dict)]
        if mechanisms != list(MECHANISMS):
            raise ValueError("PHASE4CC_CAPABILITY_COVERAGE_OR_ORDER_INVALID")
        normalized = []
        for row in capabilities:
            fields = {"mechanism", "status", "authoritative_url", "evidence_hash"}
            if set(row) != fields or row["status"] not in STATUSES:
                raise ValueError("PHASE4CC_CAPABILITY_FIELDS_OR_STATUS_INVALID")
            if not isinstance(row["authoritative_url"], str) or not row[
                "authoritative_url"
            ].startswith("https://"):
                raise ValueError("PHASE4CC_AUTHORITATIVE_URL_INVALID")
            if not isinstance(row["evidence_hash"], str) or len(row["evidence_hash"]) != 64:
                raise ValueError("PHASE4CC_EVIDENCE_HASH_INVALID")
            action = (
                "OFFLINE_INTEGRATION_CANDIDATE"
                if row["status"] == "SUPPORTED"
                else "DO_NOT_IMPLEMENT"
            )
            normalized.append({**row, "proposal_action": action})
        proposals.append({"provider": provider["provider"], "capabilities": normalized})
    supported = [
        {"provider": provider["provider"], "mechanism": capability["mechanism"]}
        for provider in proposals
        for capability in provider["capabilities"]
        if capability["status"] == "SUPPORTED"
    ]
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "phase": "4CC",
        "input_hash": evidence["artifact_hash"],
        "providers": proposals,
        "supported_candidates": supported,
        "unknown_support_is_disabled": True,
        "proposal_only": True,
        "collector_changes_applied": 0,
        "live_request_changes_applied": 0,
        "production_records_created": 0,
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
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = build_proposal(json.loads(args.evidence.read_text(encoding="utf-8")))
    publish(args.output, report)
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
