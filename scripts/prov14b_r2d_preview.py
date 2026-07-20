"""Generate the local PROV-14B-R2D workflow certification preview."""

from pathlib import Path

from kalshi_predictor.phase_prov14b_r2d import write_ci_workflow_preview

print(write_ci_workflow_preview(Path("."), Path("reports/phase_prov14b_r2d")))
