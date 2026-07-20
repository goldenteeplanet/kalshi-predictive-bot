from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from kalshi_predictor.config import Settings
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.ui.app import create_app
from kalshi_predictor.ui.evidence_viewer import EvidenceRejected, MAX_FILE_BYTES, build_evidence_catalog, load_evidence_artifact


def _safe_report(root: Path) -> Path:
    path=root/"ui_obs_test/report.json"; path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"phase":"UI-OBS-TEST","status":"FAILED","diagnostics":["GATE_FAILED"],"provenance":{"chain":"valid"},"rollback_evidence":"backup.json","api_key":"sensitive"}),encoding="utf-8")
    return path


def test_catalog_hashes_and_summarizes_allowlisted_evidence(tmp_path: Path) -> None:
    _safe_report(tmp_path)
    catalog=build_evidence_catalog(tmp_path)
    assert catalog["count"]==1
    item=catalog["items"][0]
    assert len(item["sha256"])==64
    assert item["gate_failures"]==["GATE_FAILED"]
    assert item["rollback"]=="backup.json"
    artifact=load_evidence_artifact(item["id"],tmp_path)
    assert artifact["verified_sha256"]==item["sha256"]
    assert "sensitive" not in artifact["content"]
    assert "[REDACTED]" in artifact["content"]


def test_non_allowlisted_oversized_and_invalid_ids_fail_closed(tmp_path: Path) -> None:
    _safe_report(tmp_path)
    bad=tmp_path/"other/report.json"; bad.parent.mkdir(); bad.write_text("{}")
    huge=tmp_path/"ui_obs_test/huge.json"; huge.write_bytes(b"x"*(MAX_FILE_BYTES+1))
    catalog=build_evidence_catalog(tmp_path)
    assert catalog["count"]==1
    assert catalog["rejected"]==2
    with pytest.raises(EvidenceRejected,match="ID_INVALID"):
        load_evidence_artifact("../../etc/passwd",tmp_path)


def test_symlink_is_rejected(tmp_path: Path) -> None:
    outside=tmp_path.parent/(tmp_path.name+"-outside.json"); outside.write_text("{}")
    directory=tmp_path/"ui_obs_test"; directory.mkdir()
    link=directory/"link.json"
    try:
        os.symlink(outside,link)
    except OSError:
        pytest.skip("symlink creation unavailable")
    catalog=build_evidence_catalog(tmp_path)
    assert catalog["count"]==0
    assert catalog["rejected"]==1


def test_malformed_json_is_visible_as_invalid_not_executed(tmp_path: Path) -> None:
    path=tmp_path/"ui_obs_test/bad.json"; path.parent.mkdir(); path.write_text("{bad")
    catalog=build_evidence_catalog(tmp_path)
    assert catalog["items"][0]["status"]=="INVALID"
    assert catalog["items"][0]["gate_failures"]==["ARTIFACT_JSON_INVALID"]


def test_read_only_routes_catalog_and_render_verified_artifact(monkeypatch,tmp_path: Path) -> None:
    _safe_report(tmp_path)
    monkeypatch.setenv("KALSHI_EVIDENCE_ROOT",str(tmp_path))
    engine=init_db(f"sqlite:///{tmp_path/'ui.db'}")
    client=TestClient(create_app(session_factory=get_session_factory(engine),settings=Settings()))
    catalog=client.get("/api/system/evidence")
    assert catalog.status_code==200
    artifact_id=catalog.json()["items"][0]["id"]
    assert client.get("/system/evidence").status_code==200
    detail=client.get(f"/system/evidence/{artifact_id}")
    assert detail.status_code==200 and "Verified read-only artifact" in detail.text
    assert client.get("/api/system/evidence/not-valid").status_code==404
    assert client.post("/api/system/evidence").status_code==405
