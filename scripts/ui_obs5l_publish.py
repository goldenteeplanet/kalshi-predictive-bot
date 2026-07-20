from __future__ import annotations

import json
from pathlib import Path

from kalshi_predictor.ui.phase_evidence_publisher import publish_exact_phase_roadmap
from kalshi_predictor.ui.phase_reconciler import reconcile_phase_roadmap


REPORTS = Path("reports")
OUTPUT = Path("reports/phase_ui_obs5l/ui_obs5l_exact_roadmap_evidence_preview.json")


def main() -> None:
    published = publish_exact_phase_roadmap(
        REPORTS,
        approvals={
            "R5-RECOVERY-9": True,
            "STORAGE-CAP-2": True,
            "PROV-14D": True,
            "PMB-35": True,
        },
        runtime_states={"PROV-14B": "RUNNING"},
    )
    reconciled = reconcile_phase_roadmap(published)
    report = {
        "phase": "UI-OBS-5L",
        "status": "PASSED_LOCAL_PREVIEW",
        "cloud_access": False,
        "database_writes": 0,
        "execution_enabled": False,
        "published": published,
        "reconciled": reconciled,
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(OUTPUT)


if __name__ == "__main__":
    main()
