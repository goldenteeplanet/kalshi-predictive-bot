from __future__ import annotations

import hashlib
import json
from typing import Any


def json_value_digest(value: Any) -> str:
    """Hash the existing sorted, compact JSON representation without side effects."""
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def json_payload_digest(payload: Any) -> str:
    """Retain the payload keyword used by the dashboard and read-model helpers."""
    return json_value_digest(payload)
