"""Outcome-blind exact-ticker settlement hint artifacts."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA = "kalshi.settlement-exact-hints.v1"
ALLOWED_HINT_FIELDS = {"ticker", "due_at", "source_capture_id_hash", "bundle_set_hash"}


def _canonical(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def artifact_hash(payload: dict[str, Any]) -> str:
    unsigned = {key: value for key, value in payload.items() if key != "artifact_hash"}
    return hashlib.sha256(_canonical(unsigned)).hexdigest()


def validate_hint_artifact(
    path: Path, *, now: datetime, maximum_age_seconds: int = 3600
) -> list[str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema") != SCHEMA:
        raise ValueError("SETTLEMENT_HINT_SCHEMA_INVALID")
    if payload.get("artifact_hash") != artifact_hash(payload):
        raise ValueError("SETTLEMENT_HINT_HASH_MISMATCH")
    generated_at = datetime.fromisoformat(str(payload["generated_at"]).replace("Z", "+00:00"))
    generated_at = generated_at.replace(tzinfo=generated_at.tzinfo or UTC).astimezone(UTC)
    age = (now.astimezone(UTC) - generated_at).total_seconds()
    if age < 0 or age > maximum_age_seconds:
        raise ValueError("SETTLEMENT_HINT_STALE_OR_FUTURE")
    hints = payload.get("hints")
    if not isinstance(hints, list):
        raise ValueError("SETTLEMENT_HINT_ROWS_INVALID")
    tickers: list[str] = []
    seen: set[str] = set()
    for hint in hints:
        if not isinstance(hint, dict) or set(hint) != ALLOWED_HINT_FIELDS:
            raise ValueError("SETTLEMENT_HINT_FIELDS_INVALID")
        ticker = str(hint["ticker"])
        due_at = datetime.fromisoformat(str(hint["due_at"]).replace("Z", "+00:00"))
        due_at = due_at.replace(tzinfo=due_at.tzinfo or UTC).astimezone(UTC)
        if due_at > now.astimezone(UTC):
            raise ValueError("SETTLEMENT_HINT_NOT_DUE")
        for field in ("source_capture_id_hash", "bundle_set_hash"):
            value = str(hint[field])
            if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
                raise ValueError("SETTLEMENT_HINT_IDENTIFIER_HASH_INVALID")
        if ticker not in seen:
            tickers.append(ticker)
            seen.add(ticker)
    return tickers
