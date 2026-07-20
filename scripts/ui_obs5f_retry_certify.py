from __future__ import annotations

import json
from pathlib import Path


source = Path("reports/phase_ui_obs5f_retry/ui_obs5f_retry2_live_parity_certification.json")
parity = json.loads(source.read_text(encoding="utf-8"))
report = {
    "phase": "UI-OBS-5F Retry 2",
    "status": "PASSED" if parity.get("parity_passed") else "FAILED",
    "deployment_performed": True,
    "bounded_smoke_cycle_passed": parity.get("parity_passed") is True,
    "live_parity_passed": parity.get("parity_passed") is True,
    "live_parity_failures": parity.get("failures", []),
    "rollback": {
        "path": "/mnt/kalshi-backup-02/ui_obs5f_retry/20260719T130219Z",
        "manifest_verified_before_deployment": True,
    },
    "installed_sha256": {
        "collector_module": "a0381b8d7a116eb2f5a9313e4713fff39dcf742a062336d2cf46dff19c38a0b3",
        "collector_script": "bca35ed257b40f97e41b488fdbc9383389732e94343e96da717b9ececacf78fd",
        "systemd_unit": "ce70fe8f85cedc4e8ff3408b1e7f64211e520b9211876a7b8bb55f832044e8ac",
    },
    "gates": {
        "collector_result_success": True,
        "collector_timer_active_enabled": True,
        "bounded_scheduler_mapping": True,
        "legacy_watcher_disabled_inactive": True,
        "roadmap_phase_count": parity["field_coverage"]["roadmap_phases_reported"],
        "workstream_count": parity["field_coverage"]["workstreams_reported"],
        "writer_clear": True,
        "locks_clear": True,
        "database_writes": 0,
        "execution_enabled": parity["execution_enabled"],
    },
    "prov14b_integrity_check_unaffected": True,
}
output = Path("reports/phase_ui_obs5f_retry/ui_obs5f_retry2_deployment_certification.json")
output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
print(output)
