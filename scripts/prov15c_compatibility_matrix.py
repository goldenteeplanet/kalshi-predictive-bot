from __future__ import annotations

import argparse
import json
from pathlib import Path

from kalshi_predictor.provenance.export_adapter import normalize_runtime_provenance_export

parser = argparse.ArgumentParser(description="Generate deterministic PROV-15C matrix")
parser.add_argument("--fixtures-dir", type=Path, required=True)
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()

rows = []
for path in sorted(args.fixtures_dir.glob("*.json")):
    if path.name == "normalized_row.json":
        continue
    payload = json.loads(path.read_text(encoding="utf-8"))
    result = normalize_runtime_provenance_export(payload)
    rows.append({
        "fixture": path.name,
        "source_schema": result["source_schema"],
        "normalized_schema": result["normalized_schema"],
        "source_row_count": result["source_row_count"],
        "normalized_row_count": result["normalized_row_count"],
        "compatible": result["compatible"],
        "diagnostics": result["diagnostics"],
    })
report = {
    "phase": "PROV-15C",
    "mode": "DETERMINISTIC_EXPORT_SCHEMA_COMPATIBILITY_MATRIX",
    "database_access": False,
    "execution_enabled": False,
    "summary": {
        "fixtures": len(rows),
        "compatible": sum(row["compatible"] for row in rows),
        "diagnostic_fixtures": sum(not row["compatible"] for row in rows),
    },
    "rows": rows,
}
args.output.parent.mkdir(parents=True, exist_ok=True)
temporary = args.output.with_suffix(args.output.suffix + ".tmp")
temporary.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
temporary.replace(args.output)
print(args.output)
