from __future__ import annotations

import argparse
from pathlib import Path

from kalshi_predictor.benchmarking.shadow_adapter import (
    write_exposure_guard_shadow_adapter_preview,
)

parser = argparse.ArgumentParser(description="Run local PMB-33 shadow adapter preview")
parser.add_argument("--output-dir", type=Path, default=Path("reports/phase_pmb33"))
args = parser.parse_args()
print(write_exposure_guard_shadow_adapter_preview(args.output_dir))
