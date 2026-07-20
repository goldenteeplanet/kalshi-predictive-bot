from pathlib import Path


source = Path("reports/phase_ui_obs5h/ui_obs5c_normal.html")
target = Path("reports/phase_ui_obs5h/ui_obs5h_mobile_390.html")
html = source.read_text(encoding="utf-8")
mobile_css = """
html,body{width:390px!important;max-width:390px!important;margin:0!important;overflow-x:hidden!important}
main{width:390px!important;max-width:390px!important;padding:12px!important}
.progress-hero{flex-direction:column!important;padding:20px!important}
.progress-hero h1{font-size:28px!important}
.roadmap-summary-grid{grid-template-columns:1fr!important}
.section-band{padding:14px!important}
.safety-banner{display:block!important}
.safety-banner strong,.safety-banner span{display:block!important}
"""
html = html.replace("</style>", mobile_css + "</style>", 1)
target.write_text(html, encoding="utf-8")
print(target)
