from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

REQUIRED_STATES = {
    "NO_SOURCE_DATA",
    "INVALID_SOURCE",
    "STALE_INPUT",
    "CURRENT",
    "VALID_ZERO_ACTIVE_MARKETS",
    "NO_ELIGIBLE_ROWS",
}
REQUIRED_SECTIONS = {
    "Source authority",
    "What changed?",
    "Data-quality scorecard",
    "Refresh pipeline",
    "Blocker trends",
    "Candidate lifecycle",
    "Operational incidents",
    "Safety boundary",
}


def certify_refresh_readiness(project_root: Path) -> dict[str, Any]:
    adapter = project_root / "src/kalshi_predictor/ui/refresh_readiness.py"
    template = project_root / "src/kalshi_predictor/ui/templates/refresh_readiness.html"
    routes = project_root / "src/kalshi_predictor/ui/routes.py"
    styles = project_root / "src/kalshi_predictor/ui/static/styles.css"
    runbook = project_root / "docs/REFRESH_READINESS_RUNBOOK.md"
    texts = {
        "adapter": adapter.read_text(encoding="utf-8"),
        "template": template.read_text(encoding="utf-8"),
        "routes": routes.read_text(encoding="utf-8"),
        "styles": styles.read_text(encoding="utf-8"),
        "runbook": runbook.read_text(encoding="utf-8"),
    }
    checks = {
        "all_explicit_states": all(state in texts["adapter"] for state in REQUIRED_STATES),
        "all_required_sections": all(
            section in texts["template"] for section in REQUIRED_SECTIONS
        ),
        "read_only_get_routes": (
            '@router.get("/system/refresh-readiness"' in texts["routes"]
            and '@router.get("/api/system/refresh-readiness"' in texts["routes"]
            and '@router.post("/api/system/refresh-readiness"' not in texts["routes"]
        ),
        "responsive_css_available": "@media (max-width:" in texts["styles"],
        "accessibility_landmarks": (
            'aria-label="Refresh summary"' in texts["template"]
            and 'role="status"' in texts["template"]
        ),
        "operator_runbook": "Rollback" in texts["runbook"]
        and "No trading state" in texts["runbook"],
    }
    return {
        "schema_version": "refresh-readiness-certification-v1",
        "decision": "PASS" if all(checks.values()) else "INCOMPLETE",
        "checks": checks,
        "template_sha256": hashlib.sha256(template.read_bytes()).hexdigest(),
        "read_only": True,
    }


def write_refresh_readiness_certification(project_root: Path, output_path: Path) -> Path:
    payload = certify_refresh_readiness(project_root)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return output_path
