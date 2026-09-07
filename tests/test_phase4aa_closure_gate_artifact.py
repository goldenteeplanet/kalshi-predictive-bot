from __future__ import annotations

import importlib.util
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest


def _module():
    path = Path(__file__).parents[1] / "scripts/local/phase4aa_closure_gate_artifact.py"
    spec = importlib.util.spec_from_file_location("phase4aa_closure_gate_artifact", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _source(module, statuses: list[str]) -> dict[str, object]:
    rows = [{"ticker": f"T{index}", "status": status} for index, status in enumerate(statuses)]
    counts: dict[str, int] = {}
    for status in statuses:
        counts[status] = counts.get(status, 0) + 1
    return {
        "schema": module.SOURCE_SCHEMA,
        "generated_at": "2026-08-25T18:00:00+00:00",
        "hint_artifact_hash": "a" * 64,
        "hint_count": len(rows),
        "canonical_count": sum(status != "UNRESOLVED_AFTER_HINT" for status in statuses),
        "fully_evaluated_count": statuses.count("EVALUATED"),
        "closure_complete": bool(rows) and all(status == "EVALUATED" for status in statuses),
        "status_counts": dict(sorted(counts.items())),
        "rows_hash": module._hash(rows),
        "rows": rows,
    }


@pytest.mark.parametrize(
    ("statuses", "state", "safe"),
    [
        ([], "WAITING_NO_HINTS", False),
        (["UNRESOLVED_AFTER_HINT"], "WAITING_SETTLEMENT", False),
        (["CANONICAL_PRESENT_AWAITING_EVALUATION"], "WAITING_RECONCILIATION", False),
        (["PARTIAL_EVALUATION"], "ATTENTION", False),
        (["HINT_NO_CAPTURE"], "ATTENTION", False),
        (["EVALUATED", "EVALUATED"], "COMPLETE", True),
    ],
)
def test_gate_is_fail_closed(statuses: list[str], state: str, safe: bool) -> None:
    module = _module()
    result = module.build_status(
        _source(module, statuses), now=datetime(2026, 8, 25, 19, tzinfo=UTC)
    )
    assert result["state"] == state
    assert result["safe_to_advance"] is safe
    assert result["trading_mode_changed"] is False
    assert result["production_database_written"] is False
    assert result["artifact_hash"] == module.artifact_hash(result)


def test_source_validation_and_atomic_publication(tmp_path: Path) -> None:
    module = _module()
    source = _source(module, ["UNRESOLVED_AFTER_HINT"])
    path = tmp_path / "closure.json"
    path.write_text(json.dumps(source), encoding="utf-8")
    loaded = module.load_closure(path)
    output = tmp_path / "status.json"
    payload = module.build_status(loaded, now=datetime(2026, 8, 25, 19, tzinfo=UTC))
    module.write_atomic(output, payload)
    assert json.loads(output.read_text(encoding="utf-8")) == payload
    source["status_counts"] = {"EVALUATED": 1}
    path.write_text(json.dumps(source), encoding="utf-8")
    with pytest.raises(ValueError, match="COUNTS_MISMATCH"):
        module.load_closure(path)
