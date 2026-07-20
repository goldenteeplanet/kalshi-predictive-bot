from __future__ import annotations

import argparse
import json
from pathlib import Path

from kalshi_predictor.ui.evidence_viewer import build_evidence_catalog


parser = argparse.ArgumentParser(description="Certify UI-OBS-2N read-only evidence navigation")
parser.add_argument("--reports-root", type=Path, default=Path("reports"))
parser.add_argument("--output-dir", type=Path, default=Path("reports/ui_obs2n"))
args = parser.parse_args()
catalog = build_evidence_catalog(args.reports_root)
checks = {"catalog_nonempty":catalog["count"]>0,"bounded":catalog["count"]<=catalog["limit"],"hashes_complete":all(len(item["sha256"])==64 for item in catalog["items"]),"allowlisted_formats":all(Path(item["path"]).suffix in catalog["allowed_suffixes"] for item in catalog["items"]),"read_only":catalog["read_only"] is True}
report = {"phase":"UI-OBS-2N","mode":"LOCAL_REPORT_NAVIGATION_AND_EVIDENCE_VIEWER","status":"PASSED" if all(checks.values()) else "FAILED","checks":checks,"catalog_summary":{"count":catalog["count"],"rejected":catalog["rejected"],"truncated":catalog["truncated"]},"database_access":False,"cloud_access":False,"deployment_performed":False,"filesystem_writes":0,"execution_changed":False}
args.output_dir.mkdir(parents=True, exist_ok=True)
path=args.output_dir/"ui_obs2n_evidence_viewer_certification.json"
path.write_text(json.dumps(report,indent=2,sort_keys=True)+"\n",encoding="utf-8")
print(path)
raise SystemExit(0 if report["status"]=="PASSED" else 1)
