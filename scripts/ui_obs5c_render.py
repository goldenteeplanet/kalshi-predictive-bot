from __future__ import annotations

import argparse
import html
import json
from pathlib import Path


def render(report: dict, css: str, title: str) -> str:
    scheduler = report["scheduler"]
    alerts = report.get("alerts", [])
    phase_cards = "".join(
        f'<article class="workstream-card"><div><h3>{row["number"]}. {html.escape(row["phase"])}</h3>'
        f'<span class="state-chip state-{row["status"].lower()}">{row["status"]}</span></div>'
        f'<p class="muted">{html.escape(row["evidence"])}</p></article>'
        for row in report["phases"]
    )
    alert_html = "".join(
        f'<div class="ops-alert severity-{row["severity"].lower()}"><strong>{row["code"]}</strong></div>'
        for row in alerts
    ) or '<div class="ops-alert severity-info">No active alerts</div>'
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(title)}</title><style>{css}</style></head><body><a class="skip-link" href="#main-content">Skip to main content</a><div class="safety-banner" role="status"><strong>READ ONLY</strong><span>No service, database, or trading controls.</span></div><main id="main-content" tabindex="-1"><section class="progress-hero state-{scheduler["state"].lower()}" aria-labelledby="page-title"><div><p class="page-kicker">Operations · Read only</p><h1 id="page-title">{html.escape(title)}</h1><p>Bounded scheduler and 20-phase operational evidence.</p></div><div class="execution-lock"><span>Paper / live execution</span><strong>DISABLED</strong></div></section><section class="section-band" data-live-roadmap><div class="section-head"><div><h2>Bounded scheduler and 20-phase status</h2><p class="muted">Evidence only. Controls are intentionally absent.</p></div><span class="state-chip state-{scheduler["state"].lower()}">{scheduler["state"]}</span></div><dl><div><dt>Timer</dt><dd>{html.escape(str(scheduler["timer"]))}</dd></div><div><dt>Next run</dt><dd>{html.escape(str(scheduler.get("next_run") or "Unavailable"))}</dd></div><div><dt>Cycle</dt><dd>{html.escape(str(scheduler.get("current_cycle") or "Idle"))}</dd></div><div><dt>Runtime</dt><dd>{scheduler.get("runtime_seconds") or 0}s</dd></div><div><dt>Peak memory</dt><dd>{scheduler.get("memory_peak_bytes") or "Unavailable"}</dd></div><div><dt>Heartbeat age</dt><dd>{scheduler.get("heartbeat_age_seconds") if scheduler.get("heartbeat_age_seconds") is not None else "Unavailable"}</dd></div><div><dt>Legacy watcher</dt><dd>{"DISABLED" if scheduler["legacy_watcher_disabled"] else "CHECK REQUIRED"}</dd></div><div><dt>PROV-14B</dt><dd>{html.escape(str(report["prov14b"].get("state")))}</dd></div></dl><div class="roadmap-summary-grid">{phase_cards}</div></section><section class="section-band" aria-labelledby="alerts-title"><h2 id="alerts-title">Operational alerts</h2><div class="alert-stack">{alert_html}</div></section></main></body></html>'''


parser = argparse.ArgumentParser()
parser.add_argument("--report", type=Path, default=Path("reports/phase_ui_obs5b/ui_obs5b_live_roadmap_scheduler_preview.json"))
parser.add_argument("--css", type=Path, default=Path("src/kalshi_predictor/ui/static/styles.css"))
parser.add_argument("--output-dir", type=Path, default=Path("reports/phase_ui_obs5c"))
parser.add_argument("--title-prefix", default="UI-OBS-5C")
args = parser.parse_args()
base = json.loads(args.report.read_text(encoding="utf-8"))
css = args.css.read_text(encoding="utf-8")
scenarios = {"normal": base}
stale = json.loads(json.dumps(base)); stale["scheduler"].update({"state": "RUNNING", "heartbeat_age_seconds": 61, "heartbeat_stale": True}); stale["alerts"] = [{"severity": "WARNING", "code": "BOUNDED_CYCLE_HEARTBEAT_STALE"}]; scenarios["stale"] = stale
failed = json.loads(json.dumps(base)); failed["scheduler"]["state"] = "FAILED"; failed["alerts"] = [{"severity": "CRITICAL", "code": "BOUNDED_CYCLE_FAILED"}]; scenarios["failed"] = failed
args.output_dir.mkdir(parents=True, exist_ok=True)
for name, payload in scenarios.items():
    path = args.output_dir / f"ui_obs5c_{name}.html"
    path.write_text(render(payload, css, f"{args.title_prefix} · {name.title()}"), encoding="utf-8")
    print(path)
