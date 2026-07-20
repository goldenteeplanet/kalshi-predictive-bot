from __future__ import annotations

from copy import deepcopy
from typing import Any


SCENARIOS = ("RUNNING","WAITING","BLOCKED","PASSED","FAILED","STALE","OOM","LOCK_CONTENTION","EXECUTION_DISABLED","DRIFT")


def regression_snapshot(scenario: str) -> dict[str, Any]:
    if scenario not in SCENARIOS:
        raise ValueError("UNKNOWN_UI_REGRESSION_SCENARIO")
    base = {
        "generated_at":"2099-01-01T00:10:00Z","execution_enabled":False,"paper_enabled":False,
        "active_process":{"name":"synthetic-ui-regression","state":"WAITING","stage":"idle","completion_evidence":None},
        "writer":{"state":"WAITING","pid":None,"safe_to_start_write":False,"lock_status":"CLEAR"},
        "backup":{"state":"PASSED","integrity":"ok","path":"synthetic-backup.db","sha256_status":"VERIFIED"},
        "scheduler":{"state":"WAITING","cycle":"synthetic","stage":"idle"},"alerts":[],"reports":[],
        "workstreams":[
            {"id":"pmb","name":"PMB evaluation","state":"WAITING","current_phase":"PMB synthetic"},
            {"id":"prov","name":"PROV attribution","state":"WAITING","current_phase":"PROV synthetic"},
            {"id":"nyc_weather","name":"NYC weather","state":"WAITING","current_phase":"NYC synthetic"},
            {"id":"gh_liquidity","name":"GH liquidity","state":"WAITING","current_phase":"GH synthetic"},
            {"id":"readiness","name":"Paper readiness","state":"BLOCKED","current_phase":"READINESS synthetic"},
        ],
    }
    payload=deepcopy(base); process=payload["active_process"]
    if scenario=="RUNNING": process.update({"state":"RUNNING","pid":4201,"stage":"forecast batch","started_at":"2099-01-01T00:00:00Z","updated_at":"2099-01-01T00:10:00Z","completed_units":2,"total_units":10})
    elif scenario=="BLOCKED": process.update({"state":"BLOCKED","stage":"awaiting writer clearance"})
    elif scenario=="PASSED":
        process.update({"state":"PASSED","stage":"certified","completed_units":10,"total_units":10,"completion_evidence":"reports/synthetic-pass.json"})
        payload["reports"]=[{"phase":"SYNTHETIC","state":"PASSED","path":"reports/synthetic-pass.json","generated_at":payload["generated_at"]}]
    elif scenario=="FAILED": process.update({"state":"FAILED","stage":"failed gate"})
    elif scenario=="STALE": process.update({"state":"RUNNING","pid":4202,"updated_at":"2099-01-01T00:00:00Z","stage":"stale capture"})
    elif scenario=="OOM": payload["alerts"].append({"severity":"CRITICAL","code":"KERNEL_OOM","message":"Synthetic kernel OOM evidence."})
    elif scenario=="LOCK_CONTENTION":
        payload["writer"].update({"state":"RUNNING","pid":4203,"lock_status":"BUSY_WRITER"})
        payload["alerts"].append({"severity":"WARNING","code":"WRITER_LOCK_CONTENTION","message":"Synthetic writer lock contention."})
    elif scenario=="DRIFT": payload["alerts"].append({"severity":"WARNING","code":"GOLDEN_DRIFT_DETECTED","message":"Synthetic golden artifact drift."})
    return payload
