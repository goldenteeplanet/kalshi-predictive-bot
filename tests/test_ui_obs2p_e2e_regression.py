from __future__ import annotations
import json
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from kalshi_predictor.config import Settings
from kalshi_predictor.data.db import get_session_factory,init_db
from kalshi_predictor.ui.app import create_app
from kalshi_predictor.ui.regression_scenarios import SCENARIOS,regression_snapshot


@pytest.mark.parametrize("scenario",SCENARIOS)
def test_every_operational_state_renders_in_page_and_api(monkeypatch,tmp_path:Path,scenario:str)->None:
    snapshot=tmp_path/f"{scenario}.json"; snapshot.write_text(json.dumps(regression_snapshot(scenario)))
    monkeypatch.setenv("KALSHI_PROGRESS_SNAPSHOT_PATH",str(snapshot)); monkeypatch.setenv("KALSHI_CERTIFICATION_REPORTS_ROOT",str(tmp_path/"certifications"))
    engine=init_db(f"sqlite:///{tmp_path/f'{scenario}.db'}"); client=TestClient(create_app(session_factory=get_session_factory(engine),settings=Settings()))
    api=client.get("/api/system/progress"); page=client.get("/system/progress")
    assert api.status_code==200 and page.status_code==200
    payload=api.json(); assert payload["execution"]["label"]=="DISABLED" and "NO CONTROLS" in page.text
    if scenario=="RUNNING": assert payload["active_process"]["state"]=="RUNNING" and payload["active_process"]["estimated_remaining"]=="40m"
    elif scenario=="STALE": assert payload["active_process"]["state"]=="BLOCKED" and "PROCESS_EVIDENCE_STALE" in payload["diagnostics"]
    elif scenario in {"OOM","LOCK_CONTENTION","DRIFT"}: assert {item["code"] for item in payload["alerts"]}&{"KERNEL_OOM","WRITER_LOCK_CONTENTION","GOLDEN_DRIFT_DETECTED"}
    elif scenario=="EXECUTION_DISABLED": assert "Paper / live execution" in page.text and "DISABLED" in page.text
    else: assert payload["active_process"]["state"]==scenario


def test_no_mutating_progress_or_evidence_routes(tmp_path:Path)->None:
    engine=init_db(f"sqlite:///{tmp_path/'ui.db'}"); client=TestClient(create_app(session_factory=get_session_factory(engine),settings=Settings()))
    for route in ("/api/system/progress","/api/system/evidence","/system/progress","/system/evidence"):
        assert client.post(route).status_code==405
