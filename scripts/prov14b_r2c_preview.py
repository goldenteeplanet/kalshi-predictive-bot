"""Generate the deterministic PROV-14B-R2C one-command pipeline preview."""

from __future__ import annotations

import runpy
from datetime import UTC, datetime, timedelta
from pathlib import Path

from kalshi_predictor.phase_prov14b_r2c import (
    run_capture_certification_pipeline,
    write_pipeline_report,
)

AS_OF = datetime(2026, 7, 19, 18, 0, tzinfo=UTC)
runpy.run_path("scripts/prov14b_r2b_preview.py", run_name="prov14b_r2b_fixture_setup")
fixtures = Path("reports/phase_prov14b_r2b/fixtures")
rollback_root = Path("scripts")
capture_kwargs = {
    "backup_path": fixtures / "backup.json",
    "writer_monitor_path": fixtures / "writer_monitor.txt",
    "locks_path": fixtures / "db_locks.txt",
    "services_path": fixtures / "services.json",
    "execution_path": fixtures / "execution.txt",
    "cycle_path": fixtures / "cycle.json",
    "attribution_path": fixtures / "attribution.json",
    "rollback_root": rollback_root,
    "rollback_paths": ["prov14_bounded_cycle.py", "prov14_certify.py"],
    "captured_at": AS_OF - timedelta(minutes=1),
}
report = run_capture_certification_pipeline(
    capture_kwargs=capture_kwargs,
    rollback_root=rollback_root,
    as_of=AS_OF,
    synthetic_preview=True,
)
print(
    write_pipeline_report(
        report,
        Path("reports/phase_prov14b_r2c/prov14b_r2c_ci_preview.json"),
    )
)
raise SystemExit(report["summary"]["ci_exit_code"])
