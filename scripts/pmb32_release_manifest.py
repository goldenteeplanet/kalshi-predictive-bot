from __future__ import annotations

import argparse
from pathlib import Path

from kalshi_predictor.benchmarking.release_manifest import write_benchmark_release_manifest

parser = argparse.ArgumentParser(description="Build local PMB-32 benchmark release manifest")
parser.add_argument("--project-root", type=Path, default=Path("."))
parser.add_argument("--output-dir", type=Path, default=Path("reports/phase_pmb32"))
args = parser.parse_args()
print(write_benchmark_release_manifest(args.project_root, args.output_dir))
