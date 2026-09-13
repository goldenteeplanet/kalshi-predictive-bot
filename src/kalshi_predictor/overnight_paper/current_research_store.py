"""Append-only current research in the existing isolated mission journal.

No paper table, schema migration, or exchange interface is used. These records
cannot satisfy a release qualification. Caller owns the writer transaction.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from kalshi_predictor.crypto.prospective_calibration import (
    calibration_dataset,
    prospective_decision,
)
from kalshi_predictor.overnight_paper.store import aware, digest, encode

PREFIX = "current-research-v1:"
KINDS = {"SCAN", "ASSESSMENT", "PROSPECTIVE_SHADOW", "SHADOW_OBSERVATION", "EVALUATION"}
MAX_RECORD_BYTES = 8_000_000
MAX_READ_BYTES = 32_000_000


def _hash(value: Any) -> bool:
    return (
        isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)
    )


def _shadow(payload: dict[str, Any]) -> dict[str, Any]:
    decision = {key: value for key, value in payload.items() if key != "execution_authority"}
    replayed = prospective_decision(
        decision["decision_original_json"].encode(),
        protocol=decision["protocol_original_json"].encode(),
        recorded_at=aware(decision["recorded_at"]),
    )
    if replayed != decision:
        raise ValueError("CURRENT_RESEARCH_SHADOW_ORIGINAL_REPLAY")
    return decision


def _validate_payload(kind: str, payload: dict[str, Any], recorded_at: datetime) -> None:
    if (
        not isinstance(payload, dict)
        or payload.get("paper_eligible") is not False
        or payload.get("execution_authority") is not False
    ):
        raise ValueError("CURRENT_RESEARCH_CANNOT_AUTHORIZE_EXECUTION")
    decision = payload.get("decision_time", payload.get("assessed_at"))
    if decision is not None and aware(decision) > recorded_at:
        raise ValueError("CURRENT_RESEARCH_FUTURE_RECORD")
    if kind == "SCAN":
        if (
            payload.get("version") != "PAGINATED_CURRENT_RESEARCH_V2"
            or not isinstance(payload.get("rows"), list)
            or len(payload["rows"]) > 600
            or not isinstance(payload.get("funnel"), dict)
            or not _hash(payload.get("protocol_sha256"))
            or decision is None
        ):
            raise ValueError("CURRENT_RESEARCH_SCAN_SCHEMA")
    elif kind == "ASSESSMENT":
        if (
            not isinstance(payload.get("ticker"), str)
            or not payload["ticker"]
            or payload.get("side") not in ("YES", "NO")
            or not _hash(payload.get("scan_sha256"))
            or decision is None
            or payload.get("scope") != "CURRENT_UNCALIBRATED_RESEARCH"
        ):
            raise ValueError("CURRENT_RESEARCH_ASSESSMENT_SCHEMA")
    elif kind == "PROSPECTIVE_SHADOW":
        if (
            decision is None
            or recorded_at >= aware(payload["target_at"])
            or payload.get("state") != "OPEN"
        ):
            raise ValueError("CURRENT_SHADOW_MUST_BE_PERSISTED_BEFORE_TARGET")
        replay = _shadow(payload)
        if aware(replay["recorded_at"]) > recorded_at:
            raise ValueError("CURRENT_RESEARCH_FUTURE_RECORD")
    elif kind == "EVALUATION":
        shadow, evaluation = payload["decision"], payload["evaluation"]
        calibration_dataset([shadow], [evaluation])
        if (
            decision != shadow["decision_time"]
            or payload.get("assessed_at") != evaluation["evaluated_at"]
            or aware(evaluation["evaluated_at"]) > recorded_at
        ):
            raise ValueError("CURRENT_RESEARCH_EVALUATION_CLOCK")
    elif kind == "SHADOW_OBSERVATION":
        _validate_observation(payload, recorded_at)


def _validate_observation(payload: dict[str, Any], recorded_at: datetime) -> None:
    if not _hash(payload.get("decision_id")):
        raise ValueError("CURRENT_RESEARCH_OBSERVATION_IDENTITY")
    raw = payload["official_original_json"].encode()
    receipt = json.loads(payload["official_receipt_json"])
    market = json.loads(raw)["market"]
    url = urlsplit(receipt["url"])
    if (
        payload.get("state") not in ("CLOSED", "AWAITING_FINAL", "FINAL")
        or url.scheme != "https"
        or url.netloc not in ("external-api.kalshi.com", "api.elections.kalshi.com")
        or url.path != "/trade-api/v2/markets/" + market["ticker"]
        or url.query
        or url.fragment
        or receipt.get("method") != "GET"
        or receipt.get("http_status") != 200
        or receipt.get("original_complete") is not True
        or receipt.get("source_sha256") != hashlib.sha256(raw).hexdigest()
        or not aware(receipt["requested_at"])
        <= aware(receipt["received_at"])
        <= aware(payload["observed_at"])
        <= recorded_at
    ):
        raise ValueError("CURRENT_RESEARCH_OBSERVATION_ORIGINAL")
    if payload["state"] == "FINAL" and (
        market.get("status") != "finalized"
        or market.get("result") not in ("yes", "no")
        or market.get("is_provisional") not in (None, False)
    ):
        raise ValueError("CURRENT_RESEARCH_OBSERVATION_NOT_FINAL")


def _linked_shadow(db: sqlite3.Connection, kind: str, payload: dict[str, Any]) -> None:
    if kind not in ("SHADOW_OBSERVATION", "EVALUATION"):
        return
    identity = (
        payload["decision_id"]
        if kind == "SHADOW_OBSERVATION"
        else payload["decision"]["decision_id"]
    )
    key = PREFIX + "prospective_shadow:" + digest({"identity": identity})
    previous = db.execute(
        "SELECT captured_at,payload FROM overnight_sprint_cycles WHERE id=?",
        (key,),
    ).fetchone()
    if previous is None:
        raise ValueError("CURRENT_RESEARCH_PRIOR_SHADOW_REQUIRED")
    envelope = _read_envelope(key, previous[0], previous[1])
    decision = _shadow(envelope["record"])
    if kind == "EVALUATION" and decision != payload["decision"]:
        raise ValueError("CURRENT_RESEARCH_EVALUATION_SHADOW_MISMATCH")
    if kind == "SHADOW_OBSERVATION":
        market = json.loads(payload["official_original_json"])["market"]
        if (
            market.get("ticker") != decision["ticker"]
            or market.get("event_ticker") != decision["event"]
            or aware(payload["observed_at"]) < aware(decision["target_at"])
        ):
            raise ValueError("CURRENT_RESEARCH_OBSERVATION_SHADOW_MISMATCH")


def _read_envelope(key: str, at: str, raw: str) -> dict[str, Any]:
    if not isinstance(raw, str) or len(raw.encode()) > MAX_RECORD_BYTES:
        raise ValueError("CURRENT_RESEARCH_RECORD_SIZE_LIMIT")
    try:
        envelope = json.loads(raw)
        kind = envelope["record_kind"]
        if (
            envelope["kind"] != "CURRENT_MISSION_RESEARCH_V1"
            or kind not in KINDS
            or envelope["recorded_at"] != at
            or digest(envelope["record"]) != envelope["payload_sha256"]
            or key != PREFIX + kind.lower() + ":" + digest({"identity": envelope["identity"]})
        ):
            raise ValueError("CURRENT_RESEARCH_JOURNAL_INTEGRITY")
        _validate_payload(kind, envelope["record"], aware(at))
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise ValueError("CURRENT_RESEARCH_JOURNAL_INTEGRITY") from exc
    return envelope


def append_current_record(
    db: sqlite3.Connection,
    *,
    kind: str,
    identity: str,
    payload: dict[str, Any],
    recorded_at: datetime,
) -> str:
    """Persist identical replay once; conflicting identities fail without overwrite."""
    if not db.in_transaction or kind not in KINDS:
        raise ValueError("CURRENT_RESEARCH_WRITER_TRANSACTION_AND_KIND_REQUIRED")
    if not identity or len(identity) > 240 or recorded_at.utcoffset() is None:
        raise ValueError("CURRENT_RESEARCH_IDENTITY_AND_AWARE_CLOCK_REQUIRED")
    _validate_payload(kind, payload, recorded_at)
    _linked_shadow(db, kind, payload)
    if kind in ("PROSPECTIVE_SHADOW", "EVALUATION"):
        expected = (
            payload["decision_id"]
            if kind == "PROSPECTIVE_SHADOW"
            else payload["decision"]["decision_id"]
        )
        if identity != expected:
            raise ValueError("CURRENT_RESEARCH_DECISION_IDENTITY_MISMATCH")
    key = PREFIX + kind.lower() + ":" + digest({"identity": identity})
    envelope = {
        "kind": "CURRENT_MISSION_RESEARCH_V1",
        "record_kind": kind,
        "identity": identity,
        "payload_sha256": digest(payload),
        "record": payload,
        "recorded_at": recorded_at.isoformat(),
    }
    raw = encode(envelope)
    if len(raw.encode()) > MAX_RECORD_BYTES:
        raise ValueError("CURRENT_RESEARCH_RECORD_SIZE_LIMIT")
    existing = db.execute(
        "SELECT captured_at,payload FROM overnight_sprint_cycles WHERE id=?",
        (key,),
    ).fetchone()
    if existing is not None:
        previous = _read_envelope(key, existing[0], existing[1])
        if previous["record"] != payload:
            raise ValueError("CURRENT_RESEARCH_IDEMPOTENCY_CONFLICT")
        return key
    db.execute(
        "INSERT INTO overnight_sprint_cycles(id,captured_at,payload) VALUES(?,?,?)",
        (key, recorded_at.isoformat(), raw),
    )
    return key


def read_current_records(
    db: sqlite3.Connection,
    *,
    max_records: int = 10000,
) -> list[dict[str, Any]]:
    if type(max_records) is not int or not 1 <= max_records <= 10000:
        raise ValueError("CURRENT_RESEARCH_READ_BOUND_INVALID")
    result: list[dict[str, Any]] = []
    total_bytes = 0
    for key, at, raw in db.execute(
        "SELECT id,captured_at,payload FROM overnight_sprint_cycles "
        "WHERE id LIKE ? ORDER BY captured_at,id LIMIT ?",
        (PREFIX + "%", max_records + 1),
    ):
        if len(result) == max_records:
            raise ValueError("CURRENT_RESEARCH_READ_BOUND_EXCEEDED")
        total_bytes += len(raw.encode())
        if total_bytes > MAX_READ_BYTES:
            raise ValueError("CURRENT_RESEARCH_READ_BYTES_EXCEEDED")
        envelope = _read_envelope(key, at, raw)
        _linked_shadow(db, envelope["record_kind"], envelope["record"])
        result.append({**envelope, "journal_id": key, "recorded_at": at})
    return result
