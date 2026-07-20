from __future__ import annotations

import argparse
from pathlib import Path

from kalshi_predictor.benchmarking.provenance_portfolio import (
    write_provenance_aware_portfolio_benchmark,
)

parser = argparse.ArgumentParser(description="Run local PMB-10 provenance portfolio replay")
parser.add_argument("--output-dir", type=Path, default=Path("reports/phase_pmb10"))
args = parser.parse_args()
print(write_provenance_aware_portfolio_benchmark(args.output_dir))
