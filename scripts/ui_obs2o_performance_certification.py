from __future__ import annotations

import argparse
import json
import tempfile
import threading
import time
import tracemalloc
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from kalshi_predictor.ui.evidence_viewer import get_cached_evidence_catalog
from kalshi_predictor.ui.performance_cache import BoundedSingleFlightCache
from kalshi_predictor.ui.progress_history import load_progress_timeline


parser=argparse.ArgumentParser(description="Certify UI-OBS-2O dashboard performance and load bounds")
parser.add_argument("--project-root",type=Path,default=Path(".")); parser.add_argument("--output-dir",type=Path,default=Path("reports/ui_obs2o")); args=parser.parse_args()
with tempfile.TemporaryDirectory() as temporary:
    root=Path(temporary); reports=root/"ui_obs_load"; reports.mkdir()
    for index in range(25): (reports/f"{index:03}.json").write_text(json.dumps({"phase":f"LOAD-{index}","status":"PASSED"}))
    calls=0; lock=threading.Lock(); cache=BoundedSingleFlightCache[dict](ttl_seconds=30,max_entries=2)
    def loader():
        global calls
        with lock: calls+=1
        time.sleep(.03); return {"ok":True}
    with ThreadPoolExecutor(max_workers=16) as pool: concurrent=list(pool.map(lambda _:cache.get("shared",loader),range(32)))
    cold_start=time.perf_counter(); catalog=get_cached_evidence_catalog(root,force=True); cold_ms=(time.perf_counter()-cold_start)*1000
    tracemalloc.start(); warm_start=time.perf_counter()
    for _ in range(500): get_cached_evidence_catalog(root)
    warm_ms=(time.perf_counter()-warm_start)*1000; _,peak=tracemalloc.get_traced_memory(); tracemalloc.stop()
    entries=[{"generated_at":"2026-07-18T00:00:00Z","process":{},"writer":{},"scheduler":{},"incidents":[]} for _ in range(1000)]
    history=root/"history.json"; history.write_text(json.dumps({"entries":entries})); visible=load_progress_timeline(history)["count"]
script=(args.project_root/"src/kalshi_predictor/ui/static/app.js").read_text(encoding="utf-8")
checks={"single_flight":calls==1 and len(concurrent)==32,"catalog_bound":catalog["count"]==25,"warm_500_under_1s":warm_ms<1000,"memory_peak_under_5mb":peak<5_000_000,"large_history_visible_bound":visible==20,"api_timeout_control":"new AbortController()" in script and "controller.abort()" in script,"failure_pause":"POLLING PAUSED" in script}
report={"phase":"UI-OBS-2O","mode":"LOCAL_DASHBOARD_PERFORMANCE_AND_LOAD_CERTIFICATION","status":"PASSED" if all(checks.values()) else "FAILED","checks":checks,"metrics":{"cold_catalog_ms":round(cold_ms,2),"warm_500_ms":round(warm_ms,2),"memory_peak_bytes":peak,"concurrent_callers":32,"loader_executions":calls,"visible_history_entries":visible},"database_access":False,"cloud_access":False,"deployment_performed":False,"execution_changed":False}
args.output_dir.mkdir(parents=True,exist_ok=True); path=args.output_dir/"ui_obs2o_dashboard_performance_certification.json"; path.write_text(json.dumps(report,indent=2,sort_keys=True)+"\n"); print(path); raise SystemExit(0 if report["status"]=="PASSED" else 1)
