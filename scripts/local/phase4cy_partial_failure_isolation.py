"""Build isolated synthetic snapshot artifacts from success and failure events."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

INPUT_SCHEMA = "phase4cy.failure-events.v1"
REPORT_SCHEMA = "phase4cy.isolation-report.v1"
SCOPES = ("DATA", "SOURCE", "PAGE", "MARKET")
MAX_EVENTS = 100_000


def _hash(payload: Any) -> str:
    if isinstance(payload, dict):
        payload = {key: value for key, value in payload.items() if key != "artifact_hash"}
    return canonical_hash(payload)


def _key(source: str, market: str) -> str:
    return f"{source}\x1f{market}"


def build_report(payload: dict[str, Any]) -> dict[str, Any]:
    if set(payload) != {"schema", "events", "artifact_hash"}:
        raise ValueError("PHASE4CY_INPUT_FIELDS_INVALID")
    if payload.get("schema") != INPUT_SCHEMA or payload.get("artifact_hash") != _hash(payload):
        raise ValueError("PHASE4CY_INPUT_SCHEMA_OR_HASH_INVALID")
    events = payload.get("events")
    if not isinstance(events, list) or not events or len(events) > MAX_EVENTS:
        raise ValueError("PHASE4CY_EVENT_COUNT_INVALID")

    identifiers: set[str] = set()
    data_events: dict[str, list[dict[str, Any]]] = {}
    failures = []
    source_failures: set[str] = set()
    market_failures: set[str] = set()
    page_failures: set[tuple[str, str, int]] = set()
    required = {"event_id", "source", "page", "market", "scope", "status", "payload"}
    for event in events:
        if not isinstance(event, dict) or set(event) != required:
            raise ValueError("PHASE4CY_EVENT_FIELDS_INVALID")
        identifier = event["event_id"]
        source, market, page = event["source"], event["market"], event["page"]
        scope, status = event["scope"], event["status"]
        if not isinstance(identifier, str) or not identifier or identifier in identifiers:
            raise ValueError("PHASE4CY_EVENT_ID_INVALID")
        if not isinstance(source, str) or not source or not isinstance(market, str) or not market:
            raise ValueError("PHASE4CY_SOURCE_OR_MARKET_INVALID")
        if isinstance(page, bool) or not isinstance(page, int) or page < 0:
            raise ValueError("PHASE4CY_PAGE_INVALID")
        if scope not in SCOPES:
            raise ValueError("PHASE4CY_SCOPE_INVALID")
        if scope == "DATA":
            if status != "SUCCESS" or not isinstance(event["payload"], dict):
                raise ValueError("PHASE4CY_DATA_EVENT_INVALID")
            data_events.setdefault(_key(source, market), []).append(event)
        else:
            if status != "FAILED" or event["payload"] is not None:
                raise ValueError("PHASE4CY_FAILURE_EVENT_INVALID")
            failures.append(event)
            if scope == "SOURCE":
                source_failures.add(source)
            elif scope == "MARKET":
                market_failures.add(_key(source, market))
            else:
                page_failures.add((source, market, page))
        identifiers.add(identifier)

    artifacts = []
    quarantined = []
    for compound_key in sorted(data_events):
        rows = data_events[compound_key]
        source, market = compound_key.split("\x1f", 1)
        reasons = []
        if source in source_failures:
            reasons.append("SOURCE_FAILURE")
        if compound_key in market_failures:
            reasons.append("MARKET_FAILURE")
        failed_pages = sorted(
            page for failed_source, failed_market, page in page_failures
            if failed_source == source and failed_market == market
        )
        if failed_pages:
            reasons.append("PAGE_FAILURE")
        if reasons:
            quarantined.append(
                {
                    "source": source,
                    "market": market,
                    "reasons": reasons,
                    "failed_pages": failed_pages,
                }
            )
            continue
        artifact: dict[str, Any] = {
            "source": source,
            "market": market,
            "records": [
                {"event_id": row["event_id"], "page": row["page"], "payload": row["payload"]}
                for row in sorted(rows, key=lambda value: (value["page"], value["event_id"]))
            ],
        }
        artifact["artifact_hash"] = _hash(artifact)
        artifacts.append(artifact)
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "phase": "4CY",
        "input_hash": payload["artifact_hash"],
        "artifacts": artifacts,
        "quarantined": quarantined,
        "failure_count": len(failures),
        "published_artifact_count": len(artifacts),
        "partial_artifacts_published": 0,
        "production_publications": 0,
        "execution_authorized": False,
        "production_records_created": 0,
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
    parser.add_argument("--events", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = build_report(json.loads(args.events.read_text(encoding="utf-8")))
    publish(args.output, report)
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
