from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

STATUS_NOT_OBSERVED = "NOT_OBSERVED"
STATUS_PASS = "PASS"
STATUS_FAIL = "FAIL"


def observe_runtime(*, runtime_url: str | None = None) -> dict[str, Any]:
    observed_at = datetime.now(tz=UTC).isoformat()
    if not runtime_url:
        return {
            "status": STATUS_NOT_OBSERVED,
            "runtime_url": None,
            "observed_at": observed_at,
            "message": "No runtime URL was provided; runtime behavior was not observed.",
            "checks": [],
        }
    return {
        "status": STATUS_NOT_OBSERVED,
        "runtime_url": runtime_url,
        "observed_at": observed_at,
        "message": (
            "Runtime URL was supplied, but Phase 3W-R does not perform live or deployed "
            "runtime probes without an explicit read-only probe adapter."
        ),
        "checks": [],
    }
