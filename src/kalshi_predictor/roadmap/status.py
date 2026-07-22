from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from kalshi_predictor.roadmap.artifacts import verify_signed_artifact

PHASES = (
    (0, "Integration baseline"),
    (1, "Authoritative runtime evidence"),
    (2, "Category ingestion expansion"),
    (3, "Paper-trade generation factory"),
    (4, "PostgreSQL authority"),
    (5, "Model and strategy certification"),
    (6, "Production-grade paper risk"),
    (7, "Authenticated demo gateway"),
    (8, "Live readiness and approval"),
    (9, "Multi-category micro live"),
    (10, "Controlled expansion"),
)


def build_roadmap_status(reports_root: Path = Path("reports")) -> dict[str, Any]:
    phase_gh2 = _read_json(reports_root / "phase_gh2/gh2_active_candidate_refresh.json")
    paper = _read_evidence(reports_root / "roadmap/paper_scale_gate.json")
    postgres = _read_evidence(reports_root / "roadmap/postgres_authority.json")
    model = _read_evidence(reports_root / "roadmap/model_strategy_certification.json")
    risk = _read_evidence(reports_root / "roadmap/risk_operations_certification.json")
    demo = _read_evidence(reports_root / "roadmap/demo_gateway_certification.json")
    live = _read_json(reports_root / "live_readiness_decision.json")
    system = _read_json(
        reports_root / "system_certification/system_certification_report.json"
    )
    categories = _category_certifications(reports_root / "roadmap/categories")
    soak = phase_gh2.get("soak") or {}
    phase_checks = {
        0: bool(phase_gh2),
        1: bool(soak.get("soak_complete")),
        2: bool(categories) and all(
            row.get("paper_pipeline_certified") for row in categories.values()
        ),
        3: bool(paper.get("passed")),
        4: bool(postgres.get("passed")),
        5: bool(model.get("passed")),
        6: bool(risk.get("passed")),
        7: bool(demo.get("passed")),
        8: (
            str(live.get("decision")) in {"GO", "CONDITIONAL_GO"}
            and str(system.get("outcome")) == "SYSTEM_PASS"
        ),
        9: False,
        10: False,
    }
    rows = [
        {
            "phase": number,
            "name": name,
            "status": "PASSED" if phase_checks[number] else "BLOCKED",
            "blocking_predecessor": next(
                (
                    prior
                    for prior in range(number)
                    if not phase_checks.get(prior, False)
                ),
                None,
            ),
        }
        for number, name in PHASES
    ]
    first_blocked = next((row for row in rows if row["status"] == "BLOCKED"), None)
    return {
        "schema_version": "paper-to-live-roadmap-status-v1",
        "overall_status": "BLOCKED" if first_blocked else "PASSED",
        "first_blocked_phase": first_blocked,
        "phases": rows,
        "category_certifications": categories,
        "live_execution_enabled": False,
        "autopilot_enabled": False,
    }


def write_roadmap_status(
    reports_root: Path = Path("reports"),
    output_path: Path = Path("reports/roadmap/roadmap_status.json"),
) -> Path:
    payload = build_roadmap_status(reports_root)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return output_path


def _category_certifications(path: Path) -> dict[str, dict[str, Any]]:
    result = {}
    for artifact in sorted(path.glob("*.json")) if path.exists() else []:
        verified = verify_signed_artifact(artifact)
        payload = verified.get("payload") if verified.get("verified") else _read_json(artifact)
        category = str(payload.get("category") or artifact.stem)
        result[category] = payload
    return result


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _read_evidence(path: Path) -> dict[str, Any]:
    verified = verify_signed_artifact(path)
    if verified.get("verified"):
        return dict(verified.get("payload") or {})
    return _read_json(path)
