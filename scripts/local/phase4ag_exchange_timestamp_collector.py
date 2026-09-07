"""Phase 4AG artifact-only authoritative exchange timestamp collector."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import (
    binary_outcome,
    canonical_hash,
    normalized_timestamp,
    settlement_lineage_hash,
)

SCHEMA = "phase4ag.settlement-timestamp-collection.v1"
EVIDENCE_SCHEMA = "phase4af.settlement-timestamp-evidence.v1"
ARCHIVE_SCHEMA = "phase4ag.raw-exchange-response.v1"
AF_SCHEMA = "phase4af.settlement-timestamp-audit.v1"
AD_SCHEMA = "phase4ad.reconciliation-attribution.v1"
AE_SCHEMA = "phase4ae.reconciliation-plan.v1"
TIMESTAMP_FIELDS = (
    "settled_at",
    "settlement_ts",
    "settlement_time",
    "determined_at",
    "determination_ts",
    "result_ts",
)
RESULT_FIELDS = ("result", "settlement_result")
ALLOWED_PATH_PREFIX = "/trade-api/v2/markets/"
Transport = Callable[[str, float, float, int], tuple[int, str, bytes]]


def artifact_hash(payload: dict[str, Any]) -> str:
    return canonical_hash({key: value for key, value in payload.items() if key != "artifact_hash"})


def _strict_timestamp(value: object) -> str:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("TIMEZONE_MISSING")
    return normalized_timestamp(parsed)


def _load(path: Path, schema: str, label: str) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema") != schema:
        raise ValueError(f"PHASE4AG_{label}_SCHEMA_INVALID")
    if payload.get("artifact_hash") != artifact_hash(payload):
        raise ValueError(f"PHASE4AG_{label}_HASH_MISMATCH")
    rows = payload.get("rows")
    if not isinstance(rows, list) or payload.get("rows_hash") != canonical_hash(rows):
        raise ValueError(f"PHASE4AG_{label}_ROWS_HASH_MISMATCH")
    return payload


def _validate_inputs(
    af: dict[str, Any], ad: dict[str, Any], ae: dict[str, Any], manifest_hash: str
) -> None:
    if af.get("source_phase4ad_artifact_hash") != ad["artifact_hash"]:
        raise ValueError("PHASE4AG_AF_AD_LINEAGE_MISMATCH")
    if af.get("source_phase4ae_artifact_hash") != ae["artifact_hash"]:
        raise ValueError("PHASE4AG_AF_AE_LINEAGE_MISMATCH")
    if af.get("source_phase4ac_manifest_hash") != manifest_hash:
        raise ValueError("PHASE4AG_AF_AC_LINEAGE_MISMATCH")
    if ae.get("source_phase4ad_artifact_hash") != ad["artifact_hash"]:
        raise ValueError("PHASE4AG_AE_AD_LINEAGE_MISMATCH")
    if ad.get("source_phase4ac_manifest_hash") != manifest_hash:
        raise ValueError("PHASE4AG_AD_AC_LINEAGE_MISMATCH")


def _history_hash(history_dir: Path) -> str:
    import importlib.util

    script = Path(__file__).with_name("phase4ac_artifact_history.py")
    spec = importlib.util.spec_from_file_location("phase4ac_for_4ag", script)
    if not spec or not spec.loader:
        raise ValueError("PHASE4AG_HISTORY_LOADER_UNAVAILABLE")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return str(module.reconstruct_phase4ab_history(history_dir)["manifest_hash"])


def _ro(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise ValueError("PHASE4AG_DATABASE_NOT_FILE")
    connection = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    if connection.execute("PRAGMA query_only").fetchone()[0] != 1:
        connection.close()
        raise ValueError("PHASE4AG_QUERY_ONLY_NOT_ENFORCED")
    return connection


def _eligible(af: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    seen: set[tuple[object, object]] = set()
    for row in af["rows"]:
        key = (row.get("capture_id"), row.get("ticker"))
        if key in seen:
            raise ValueError("PHASE4AG_DUPLICATE_INPUT_ROW")
        seen.add(key)
        if row.get("classification") == "RESULT_PRESENT_TIMESTAMP_MISSING":
            grouped.setdefault(str(row["ticker"]), []).append(row)
    return grouped


def _archive_index(directory: Path | None) -> dict[str, list[dict[str, Any]]]:
    indexed: dict[str, list[dict[str, Any]]] = {}
    if directory is None:
        return indexed
    if not directory.is_dir():
        raise ValueError("PHASE4AG_ARCHIVE_DIRECTORY_MISSING")
    for path in sorted(directory.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("schema") != ARCHIVE_SCHEMA:
            raise ValueError("PHASE4AG_ARCHIVE_SCHEMA_INVALID")
        if payload.get("artifact_hash") != artifact_hash(payload):
            raise ValueError("PHASE4AG_ARCHIVE_HASH_MISMATCH")
        allowed = {"schema", "requested_ticker", "retrieved_at", "response", "artifact_hash"}
        if set(payload) != allowed or not isinstance(payload.get("response"), dict):
            raise ValueError("PHASE4AG_ARCHIVE_FIELDS_INVALID")
        _strict_timestamp(payload["retrieved_at"])
        indexed.setdefault(str(payload["requested_ticker"]), []).append(payload)
    return indexed


def _market_payload(response: dict[str, Any]) -> dict[str, Any] | None:
    market = response.get("market", response)
    return market if isinstance(market, dict) else None


def inspect_exchange_payload(
    ticker: str,
    expected_result: object,
    responses: list[dict[str, Any]],
    *,
    source_kind: str,
) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    response_hashes: list[str] = []
    for response in responses:
        response_hash = canonical_hash(response)
        response_hashes.append(response_hash)
        market = _market_payload(response)
        if market is None:
            return {
                "disposition": "EXCHANGE_PAYLOAD_INVALID",
                "reason_codes": ["MARKET_OBJECT_MISSING"],
            }
        if str(market.get("ticker") or "") != ticker:
            return {
                "disposition": "MARKET_IDENTITY_MISMATCH",
                "reason_codes": ["EXACT_TICKER_MISMATCH"],
            }
        result = next(
            (market.get(key) for key in RESULT_FIELDS if market.get(key) not in (None, "")), None
        )
        if binary_outcome(result) != binary_outcome(expected_result):
            return {"disposition": "EXCHANGE_RESULT_CONFLICT", "reason_codes": ["RESULT_MISMATCH"]}
        for field in TIMESTAMP_FIELDS:
            value = market.get(field)
            if value in (None, ""):
                continue
            try:
                normalized = _strict_timestamp(value)
            except ValueError as error:
                reason = str(error)
                disposition = (
                    "EXCHANGE_TIMESTAMP_TIMEZONE_AMBIGUOUS"
                    if reason == "TIMEZONE_MISSING"
                    else "EXCHANGE_PAYLOAD_INVALID"
                )
                return {"disposition": disposition, "reason_codes": [reason]}
            candidates.append(
                {
                    "field_path": f"market.{field}",
                    "original_timestamp": value,
                    "normalized_timestamp": normalized,
                    "exchange_result": str(result).lower(),
                    "response_hash": response_hash,
                }
            )
    if not candidates:
        return {
            "disposition": "EXCHANGE_TIMESTAMP_MISSING",
            "reason_codes": ["ALLOWED_FIELD_MISSING"],
        }
    times = {candidate["normalized_timestamp"] for candidate in candidates}
    if len(times) != 1:
        return {
            "disposition": "EXCHANGE_TIMESTAMP_CONFLICT",
            "reason_codes": ["NORMALIZED_TIMES_DIFFER"],
        }
    chosen = sorted(candidates, key=lambda item: (item["field_path"], item["response_hash"]))[0]
    disposition = (
        "ARCHIVED_EXCHANGE_EVIDENCE_FOUND"
        if source_kind == "archive"
        else "DIRECT_EXCHANGE_EVIDENCE_FOUND"
    )
    return {
        "disposition": disposition,
        "reason_codes": ["EXPLICIT_EXCHANGE_SETTLEMENT_TIMESTAMP"],
        **chosen,
        "response_hashes": sorted(response_hashes),
    }


def _default_transport(url: str, connect: float, response: float, maximum: int):
    request = urllib.request.Request(url, method="GET", headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=max(connect, response)) as result:
            final_url = result.geturl()
            body = result.read(maximum + 1)
            if len(body) > maximum:
                raise ValueError("RESPONSE_TOO_LARGE")
            return int(result.status), final_url, body
    except urllib.error.HTTPError as error:
        return int(error.code), error.geturl(), error.read(maximum + 1)


def _api_url(base_url: str, allowed_host: str, ticker: str) -> str:
    parsed = urllib.parse.urlparse(base_url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != allowed_host
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("PHASE4AG_API_BASE_URL_UNSAFE")
    path = f"{ALLOWED_PATH_PREFIX}{urllib.parse.quote(ticker, safe='')}"
    return urllib.parse.urlunparse(("https", parsed.netloc, path, "", "", ""))


def _fetch(
    url: str,
    allowed_host: str,
    *,
    connect_timeout: float,
    response_timeout: float,
    maximum_bytes: int,
    retries: int,
    transport: Transport,
) -> tuple[list[dict[str, Any]] | None, int, str | None]:
    requests = 0
    for attempt in range(retries + 1):
        requests += 1
        try:
            status, final_url, body = transport(
                url, connect_timeout, response_timeout, maximum_bytes
            )
            final = urllib.parse.urlparse(final_url)
            if final.scheme != "https" or final.hostname != allowed_host:
                return None, requests, "REDIRECT_HOST_DISALLOWED"
            if len(body) > maximum_bytes:
                return None, requests, "RESPONSE_TOO_LARGE"
            if status in {401, 403}:
                return None, requests, "AUTHORIZATION_FAILED"
            if status != 200:
                if status < 500 or attempt == retries:
                    return None, requests, f"HTTP_{status}"
                continue
            decoded = json.loads(body.decode("utf-8"))
            return [decoded] if isinstance(decoded, dict) else None, requests, None
        except (
            OSError,
            TimeoutError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            ValueError,
        ) as error:
            if attempt == retries:
                return None, requests, type(error).__name__
    return None, requests, "BOUNDED_RETRIES_EXHAUSTED"


def collect(
    source_db: Path,
    af_path: Path,
    ad_path: Path,
    ae_path: Path,
    history_dir: Path,
    *,
    now: datetime,
    mode: str,
    archive_dir: Path | None = None,
    api_base_url: str | None = None,
    allowed_host: str | None = None,
    connect_timeout: float = 3,
    response_timeout: float = 10,
    maximum_response_bytes: int = 1_000_000,
    maximum_retries: int = 1,
    maximum_pages: int = 1,
    transport: Transport | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if mode not in {"offline", "bounded-read-only-api"}:
        raise ValueError("PHASE4AG_MODE_INVALID")
    if min(connect_timeout, response_timeout, maximum_response_bytes) <= 0:
        raise ValueError("PHASE4AG_BOUND_INVALID")
    if maximum_retries < 0 or maximum_pages != 1:
        raise ValueError("PHASE4AG_RETRY_OR_PAGE_BOUND_INVALID")
    now_iso = _strict_timestamp(now)
    af = _load(af_path, AF_SCHEMA, "PHASE4AF")
    ad = _load(ad_path, AD_SCHEMA, "PHASE4AD")
    ae = _load(ae_path, AE_SCHEMA, "PHASE4AE")
    manifest_hash = _history_hash(history_dir)
    _validate_inputs(af, ad, ae, manifest_hash)
    grouped = _eligible(af)
    archives = _archive_index(archive_dir)
    if mode == "bounded-read-only-api" and (not api_base_url or not allowed_host):
        raise ValueError("PHASE4AG_READ_ONLY_REQUEST_NOT_AUTHORIZED")
    connection = _ro(source_db)
    rows: list[dict[str, Any]] = []
    request_count = 0
    try:
        for ticker in sorted(grouped):
            captures = grouped[ticker]
            settlement = connection.execute(
                "SELECT * FROM settlements WHERE ticker=?", (ticker,)
            ).fetchone()
            if settlement is None:
                inspected = {
                    "disposition": "LINEAGE_FAILURE",
                    "reason_codes": ["SETTLEMENT_MISSING"],
                }
                expected_result, current_hash = None, None
            else:
                settlement_dict = dict(settlement)
                expected_result = settlement["result"]
                current_hash = settlement_lineage_hash(settlement_dict)
                expected_hashes = {row.get("settlement_lineage_hash") for row in captures}
                if expected_hashes != {current_hash}:
                    inspected = {
                        "disposition": "LINEAGE_FAILURE",
                        "reason_codes": ["SETTLEMENT_HASH_MISMATCH"],
                    }
                elif archives.get(ticker):
                    responses = [item["response"] for item in archives[ticker]]
                    inspected = inspect_exchange_payload(
                        ticker, expected_result, responses, source_kind="archive"
                    )
                elif mode == "offline":
                    inspected = {
                        "disposition": "SOURCE_ARCHIVE_MISSING",
                        "reason_codes": ["NO_EXACT_TICKER_ARCHIVE"],
                    }
                else:
                    url = _api_url(str(api_base_url), str(allowed_host), ticker)
                    responses, used, error = _fetch(
                        url,
                        str(allowed_host),
                        connect_timeout=connect_timeout,
                        response_timeout=response_timeout,
                        maximum_bytes=maximum_response_bytes,
                        retries=maximum_retries,
                        transport=transport or _default_transport,
                    )
                    request_count += used
                    if responses is None:
                        disposition = (
                            "READ_ONLY_REQUEST_NOT_AUTHORIZED"
                            if error == "AUTHORIZATION_FAILED"
                            else "HTTP_SOURCE_UNAVAILABLE"
                        )
                        inspected = {"disposition": disposition, "reason_codes": [str(error)]}
                    else:
                        inspected = inspect_exchange_payload(
                            ticker, expected_result, responses, source_kind="api"
                        )
                        inspected["sanitized_request"] = {
                            "method": "GET",
                            "host": allowed_host,
                            "path": urllib.parse.urlparse(url).path,
                        }
            row: dict[str, Any] = {
                "ticker": ticker,
                "linked_capture_ids": sorted(str(item["capture_id"]) for item in captures),
                "capture_lineage_hashes": sorted(
                    str(item["capture_lineage_hash"]) for item in captures
                ),
                "settlement_lineage_hash": current_hash,
                "existing_canonical_result": expected_result,
                "source_kind": (
                    "archive" if archives.get(ticker) else ("api" if mode != "offline" else None)
                ),
                **inspected,
            }
            provenance = {
                key: value
                for key, value in row.items()
                if key not in {"provenance_hash", "row_hash"}
            }
            row["provenance_hash"] = canonical_hash(provenance)
            row["row_hash"] = canonical_hash(row)
            rows.append(row)
    finally:
        connection.close()
    found = {
        "ARCHIVED_EXCHANGE_EVIDENCE_FOUND",
        "DIRECT_EXCHANGE_EVIDENCE_FOUND",
    }
    evidence_rows = [
        {
            "ticker": row["ticker"],
            "settlement_timestamp": row["normalized_timestamp"],
            "source_record_identity": (
                f"phase4ag:{row['source_kind']}:{row['response_hash']}:{row['field_path']}"
            ),
        }
        for row in rows
        if row["disposition"] in found
    ]
    evidence_rows.sort(
        key=lambda item: (
            item["ticker"],
            item["settlement_timestamp"],
            item["source_record_identity"],
        )
    )
    pair_id = canonical_hash(
        {"phase4af": af["artifact_hash"], "generated_at": now_iso, "rows": rows}
    )
    counts = Counter(row["disposition"] for row in rows)
    stat = source_db.stat()
    status: dict[str, Any] = {
        "schema": SCHEMA,
        "phase": "4AG",
        "generated_at": now_iso,
        "pair_id": pair_id,
        "source_phase4af_artifact_hash": af["artifact_hash"],
        "source_phase4ad_artifact_hash": ad["artifact_hash"],
        "source_phase4ae_artifact_hash": ae["artifact_hash"],
        "source_phase4ac_manifest_hash": manifest_hash,
        "production_database_identity": {
            "path": str(source_db),
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
        },
        "execution_mode": mode,
        "request_policy": {
            "methods": ["GET"],
            "allowed_host": allowed_host,
            "allowed_path_prefix": ALLOWED_PATH_PREFIX,
            "connect_timeout_seconds": connect_timeout,
            "response_timeout_seconds": response_timeout,
            "maximum_response_bytes": maximum_response_bytes,
            "maximum_retries": maximum_retries,
            "maximum_pages": maximum_pages,
        },
        "input_capture_count": sum(len(items) for items in grouped.values()),
        "eligible_ticker_count": len(grouped),
        "request_count": request_count,
        "archive_hit_count": sum(bool(archives.get(ticker)) for ticker in grouped),
        "disposition_counts": dict(sorted(counts.items())),
        "evidence_found_count": len(evidence_rows),
        "blocked_count": len(rows) - len(evidence_rows),
        "safe_for_phase4af_reaudit": bool(evidence_rows),
        "production_database_written": False,
        "research_database_written": False,
        "services_controlled": False,
        "trading_mode_changed": False,
        "orders_created": False,
        "existing_artifacts_modified": False,
        "rows_hash": canonical_hash(rows),
        "rows": rows,
    }
    status["artifact_hash"] = artifact_hash(status)
    evidence: dict[str, Any] = {
        "schema": EVIDENCE_SCHEMA,
        "generated_at": now_iso,
        "pair_id": pair_id,
        "source_phase4ag_artifact_hash": status["artifact_hash"],
        "rows_hash": canonical_hash(evidence_rows),
        "rows": evidence_rows,
    }
    evidence["artifact_hash"] = artifact_hash(evidence)
    return status, evidence


def publish_pair(
    status_path: Path,
    evidence_path: Path,
    status: dict[str, Any],
    evidence: dict[str, Any],
    *,
    replace: bool = False,
) -> None:
    if status_path.parent != evidence_path.parent:
        raise ValueError("PHASE4AG_OUTPUT_DIRECTORIES_DIFFER")
    status_path.parent.mkdir(parents=True, exist_ok=True)
    if not replace and (status_path.exists() or evidence_path.exists()):
        raise FileExistsError("PHASE4AG_OUTPUT_EXISTS")
    temporaries = [
        status_path.with_name(f".{status_path.name}.{os.getpid()}.tmp"),
        evidence_path.with_name(f".{evidence_path.name}.{os.getpid()}.tmp"),
    ]
    try:
        for path, payload in zip(temporaries, (status, evidence), strict=True):
            with path.open("x", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        os.replace(temporaries[1], evidence_path)
        os.replace(temporaries[0], status_path)
        try:
            fd = os.open(status_path.parent, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError:
            pass
    finally:
        for temporary in temporaries:
            if temporary.exists():
                temporary.unlink()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase4af-artifact", type=Path, required=True)
    parser.add_argument("--phase4ad-artifact", type=Path, required=True)
    parser.add_argument("--phase4ae-artifact", type=Path, required=True)
    parser.add_argument("--history-dir", type=Path, required=True)
    parser.add_argument("--production-db", type=Path, required=True)
    parser.add_argument("--archive-dir", type=Path)
    parser.add_argument("--mode", choices=("offline", "bounded-read-only-api"), required=True)
    parser.add_argument("--api-base-url")
    parser.add_argument("--allowed-host")
    parser.add_argument("--connect-timeout-seconds", type=float, default=3)
    parser.add_argument("--response-timeout-seconds", type=float, default=10)
    parser.add_argument("--maximum-response-bytes", type=int, default=1_000_000)
    parser.add_argument("--maximum-retries", type=int, default=1)
    parser.add_argument("--maximum-pages", type=int, default=1)
    parser.add_argument("--evaluation-time")
    parser.add_argument("--status-output", type=Path, required=True)
    parser.add_argument("--evidence-output", type=Path, required=True)
    parser.add_argument("--replace", action="store_true")
    args = parser.parse_args()
    status, evidence = collect(
        args.production_db,
        args.phase4af_artifact,
        args.phase4ad_artifact,
        args.phase4ae_artifact,
        args.history_dir,
        now=_strict_timestamp(args.evaluation_time) if args.evaluation_time else datetime.now(UTC),
        mode=args.mode,
        archive_dir=args.archive_dir,
        api_base_url=args.api_base_url,
        allowed_host=args.allowed_host,
        connect_timeout=args.connect_timeout_seconds,
        response_timeout=args.response_timeout_seconds,
        maximum_response_bytes=args.maximum_response_bytes,
        maximum_retries=args.maximum_retries,
        maximum_pages=args.maximum_pages,
    )
    publish_pair(args.status_output, args.evidence_output, status, evidence, replace=args.replace)
    print(
        json.dumps({key: value for key, value in status.items() if key != "rows"}, sort_keys=True)
    )


if __name__ == "__main__":
    main()
