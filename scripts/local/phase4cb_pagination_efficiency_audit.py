"""Audit pagination efficiency and stop correctness from captured fixtures."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

INPUT_SCHEMA = "phase4cb.captured-pagination.v1"
REPORT_SCHEMA = "phase4cb.pagination-efficiency-audit.v1"
MAX_PAGES = 10_000
MAX_ATTEMPTS_PER_PAGE = 20


def _hash(payload: Any) -> str:
    if isinstance(payload, dict):
        payload = {key: value for key, value in payload.items() if key != "artifact_hash"}
    return canonical_hash(payload)


def build_audit(capture: dict[str, Any]) -> dict[str, Any]:
    if capture.get("schema") != INPUT_SCHEMA or capture.get("artifact_hash") != _hash(capture):
        raise ValueError("PHASE4CB_INPUT_SCHEMA_OR_HASH_INVALID")
    page_size, pages = capture.get("page_size"), capture.get("pages")
    if isinstance(page_size, bool) or not isinstance(page_size, int) or page_size <= 0:
        raise ValueError("PHASE4CB_PAGE_SIZE_INVALID")
    if not isinstance(pages, list) or not pages or len(pages) > MAX_PAGES:
        raise ValueError("PHASE4CB_PAGE_COUNT_INVALID")
    seen_records: set[str] = set()
    duplicate_count = 0
    empty_count = 0
    total_records = 0
    total_attempts = 0
    stop_violations: list[dict[str, Any]] = []
    page_metrics = []
    expected_cursor: str | None = None
    for index, page in enumerate(pages, start=1):
        fields = {
            "page_number",
            "request_cursor",
            "next_cursor",
            "request_attempts",
            "records",
            "server_has_more",
        }
        if not isinstance(page, dict) or set(page) != fields:
            raise ValueError("PHASE4CB_PAGE_FIELDS_INVALID")
        if page["page_number"] != index or page["request_cursor"] != expected_cursor:
            raise ValueError("PHASE4CB_PAGE_OR_CURSOR_ORDER_INVALID")
        attempts = page["request_attempts"]
        if (
            isinstance(attempts, bool)
            or not isinstance(attempts, int)
            or not 1 <= attempts <= MAX_ATTEMPTS_PER_PAGE
        ):
            raise ValueError("PHASE4CB_REQUEST_ATTEMPTS_INVALID")
        records = page["records"]
        if not isinstance(records, list) or len(records) > page_size:
            raise ValueError("PHASE4CB_RECORD_COUNT_INVALID")
        if any(not isinstance(record, str) or not record for record in records):
            raise ValueError("PHASE4CB_RECORD_ID_INVALID")
        if not isinstance(page["server_has_more"], bool):
            raise ValueError("PHASE4CB_SERVER_HAS_MORE_INVALID")
        next_cursor = page["next_cursor"]
        if next_cursor is not None and (not isinstance(next_cursor, str) or not next_cursor):
            raise ValueError("PHASE4CB_NEXT_CURSOR_INVALID")
        if page["server_has_more"] != (next_cursor is not None):
            stop_violations.append({"page_number": index, "reason": "CURSOR_HAS_MORE_MISMATCH"})
        if index < len(pages) and next_cursor is None:
            stop_violations.append({"page_number": index, "reason": "PAGES_AFTER_TERMINAL"})
        if index == len(pages) and page["server_has_more"]:
            stop_violations.append({"page_number": index, "reason": "PREMATURE_CAPTURE_STOP"})
        duplicates_on_page = sum(record in seen_records for record in records)
        duplicate_count += duplicates_on_page
        seen_records.update(records)
        total_records += len(records)
        total_attempts += attempts
        empty_count += not records
        page_metrics.append(
            {
                "page_number": index,
                "record_count": len(records),
                "utilization_ppm": len(records) * 1_000_000 // page_size,
                "duplicate_record_count": duplicates_on_page,
                "retry_count": attempts - 1,
            }
        )
        expected_cursor = next_cursor
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "phase": "4CB",
        "input_hash": capture["artifact_hash"],
        "page_count": len(pages),
        "record_count": total_records,
        "unique_record_count": len(seen_records),
        "duplicate_record_count": duplicate_count,
        "empty_page_count": empty_count,
        "request_attempt_count": total_attempts,
        "retry_amplification_ppm": (total_attempts - len(pages)) * 1_000_000 // len(pages),
        "overall_page_utilization_ppm": total_records * 1_000_000 // (len(pages) * page_size),
        "stop_condition_correct": not stop_violations,
        "stop_violations": stop_violations,
        "page_metrics": page_metrics,
        "captured_fixture_only": True,
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
    parser.add_argument("--capture", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = build_audit(json.loads(args.capture.read_text(encoding="utf-8")))
    publish(args.output, report)
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
