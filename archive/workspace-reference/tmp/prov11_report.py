import json
from pathlib import Path

from kalshi_predictor.config import get_settings
from kalshi_predictor.data.db import get_session_factory, make_engine
from kalshi_predictor.data.backend import database_url_from_settings
from kalshi_predictor.provenance.diagnostics import write_provenance_diagnostics_report

settings = get_settings()
engine = make_engine(database_url_from_settings(settings))
factory = get_session_factory(engine)
with factory() as session:
    path = write_provenance_diagnostics_report(
        session,
        output_dir=Path("reports/phase_prov11"),
        event_limit=250,
        execution_enabled=settings.execution_enabled,
    )
payload = json.loads(path.read_text(encoding="utf-8"))
print(json.dumps({
    "path": str(path),
    "status": payload["status"],
    "summary": payload["summary"],
    "scheduler": payload["scheduler_certification"],
    "guardrails": payload["guardrails"],
}, sort_keys=True))
