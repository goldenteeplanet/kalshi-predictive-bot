from __future__ import annotations
import argparse,json
from pathlib import Path
from kalshi_predictor.ui.regression_scenarios import SCENARIOS
parser=argparse.ArgumentParser(); parser.add_argument("--browser-results",type=Path,default=Path("reports/ui_obs2p/browser_results.json")); parser.add_argument("--output-dir",type=Path,default=Path("reports/ui_obs2p")); args=parser.parse_args(); data=json.loads(args.browser_results.read_text()); observations={item["scenario"]:item for item in data["observations"]}
checks={
 "all_scenarios":set(observations)==set(SCENARIOS),
 "state_lifecycle":all(observations[name].get("state")==name for name in ("RUNNING","WAITING","BLOCKED","PASSED","FAILED")),
 "stale_fails_closed":observations["STALE"].get("state")=="BLOCKED" and "PROCESS_EVIDENCE_STALE" in observations["STALE"].get("alerts",""),
 "oom_visible":"KERNEL_OOM" in observations["OOM"].get("alerts",""),
 "lock_visible":observations["LOCK_CONTENTION"].get("locks")=="BUSY_WRITER" and "WRITER_LOCK_CONTENTION" in observations["LOCK_CONTENTION"].get("alerts",""),
 "execution_disabled":observations["EXECUTION_DISABLED"].get("execution")=="DISABLED" and observations["EXECUTION_DISABLED"].get("noControls") is True,
 "drift_visible":"GOLDEN_DRIFT_DETECTED" in observations["DRIFT"].get("alerts",""),
 "eta_exact":observations["RUNNING"].get("eta")=="40m" and observations["PASSED"].get("eta")=="0m",
 "poll_pause_resume":data["interaction"]["pause"]=={"connection":"PAUSED","pressed":"true"} and data["interaction"]["resume"]=={"connection":"CONNECTED","pressed":"false"},
 "console_clean":data.get("console_errors")==[],"screenshot_inspected":data.get("rendered_screenshot_inspected") is True,
}
report={"phase":"UI-OBS-2P","mode":"LOCAL_END_TO_END_BROWSER_REGRESSION","status":"PASSED" if all(checks.values()) else "FAILED","checks":checks,"browser":data["browser"],"scenario_count":len(observations),"observations":data["observations"],"database_access":"SYNTHETIC_HARNESS_ONLY","cloud_access":False,"deployment_performed":False,"execution_changed":False}
args.output_dir.mkdir(parents=True,exist_ok=True); path=args.output_dir/"ui_obs2p_end_to_end_browser_certification.json"; path.write_text(json.dumps(report,indent=2,sort_keys=True)+"\n"); print(path); raise SystemExit(0 if report["status"]=="PASSED" else 1)
