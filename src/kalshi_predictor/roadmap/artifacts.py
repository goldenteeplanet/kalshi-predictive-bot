from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from kalshi_predictor.roadmap.category_contract import (
    CategoryPipelineEvidence,
    certify_category_pipeline,
)
from kalshi_predictor.roadmap.database_authority import (
    DatabaseParityEvidence,
    certify_postgres_authority,
)
from kalshi_predictor.roadmap.paper_scale import PaperScaleEvidence, evaluate_paper_scale_gate


def write_category_certification(
    evidence: CategoryPipelineEvidence,
    *,
    reports_root: Path = Path("reports"),
) -> Path:
    return write_signed_artifact(
        reports_root / "roadmap/categories" / f"{evidence.category}.json",
        certify_category_pipeline(evidence),
    )


def write_paper_scale_certification(
    evidence: PaperScaleEvidence,
    *,
    reports_root: Path = Path("reports"),
) -> Path:
    return write_signed_artifact(
        reports_root / "roadmap/paper_scale_gate.json",
        evaluate_paper_scale_gate(evidence),
    )


def write_postgres_authority_certification(
    evidence: DatabaseParityEvidence,
    *,
    reports_root: Path = Path("reports"),
) -> Path:
    return write_signed_artifact(
        reports_root / "roadmap/postgres_authority.json",
        certify_postgres_authority(evidence),
    )


def verify_signed_artifact(path: Path) -> dict[str, Any]:
    envelope = _read_json(path)
    payload = envelope.get("payload") if isinstance(envelope.get("payload"), dict) else {}
    expected = str(envelope.get("sha256") or "")
    actual = _digest(payload) if payload else ""
    return {
        "verified": bool(payload) and expected == actual,
        "path": str(path),
        "payload": payload if expected == actual else {},
        "expected_sha256": expected or None,
        "actual_sha256": actual or None,
    }


def write_signed_artifact(path: Path, payload: dict[str, Any]) -> Path:
    envelope = {
        "schema_version": "roadmap-evidence-envelope-v1",
        "sha256": _digest(payload),
        "payload": payload,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(envelope, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(path)
    return path


def _digest(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(canonical).hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}
