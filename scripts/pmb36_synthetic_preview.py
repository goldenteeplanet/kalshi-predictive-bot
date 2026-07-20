"""Generate the local PMB-36 preview from the disabled PMB-33 fixture."""

from __future__ import annotations

import copy
import json
from pathlib import Path

from kalshi_predictor.benchmarking.shadow_census import build_shadow_census, write_report

source = Path("reports/phase_pmb33/pmb33_exposure_guard_shadow_adapter_preview.json")
payload = json.loads(source.read_text(encoding="utf-8"))
cycles = []
for index in range(1, 4):
    cycle = copy.deepcopy(payload)
    cycle["cycle_id"] = f"synthetic-{index}"
    cycles.append(cycle)
print(write_report(build_shadow_census(cycles), Path("reports/phase_pmb36_preview")))
