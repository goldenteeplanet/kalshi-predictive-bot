import json
from pathlib import Path

from kalshi_predictor.ui.workstream_evidence import discover_workstream_evidence


def write(root: Path, directory: str, name: str, payload: dict) -> None:
    target = root / directory / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload), encoding="utf-8")


def test_discovers_all_exact_workstreams_with_hashes(tmp_path: Path) -> None:
    write(tmp_path, "phase_pmb34f", "report.json", {"phase":"PMB-34F","summary":{"certification_passed":True}})
    write(tmp_path, "phase_prov14", "report.json", {"phase":"PROV-14B","status":"BLOCKED","next_phase":"PROV-14C"})
    write(tmp_path, "phase_nyc_w10", "report.json", {"phase":"NYC-W10","status":"PASSED"})
    write(tmp_path, "phase_gh1v", "report.json", {"phase":"GH-1V","multi_window_complete":True})
    write(tmp_path, "readiness_1", "report.json", {"phase":"READINESS-1","status":"WAITING"})
    result = discover_workstream_evidence(tmp_path)
    assert not result["diagnostics"]
    assert len(result["workstreams"]) == 5
    assert {row["state"] for row in result["workstreams"]} == {"PASSED", "BLOCKED", "WAITING"}
    assert all(len(row["evidence_sha256"]) == 64 for row in result["workstreams"])


def test_missing_evidence_is_explicit_not_fabricated(tmp_path: Path) -> None:
    result = discover_workstream_evidence(tmp_path)
    assert result["workstreams"] == []
    assert len(result["diagnostics"]) == 5


def test_oversized_and_invalid_reports_are_skipped(tmp_path: Path) -> None:
    target = tmp_path / "phase_pmb99" / "bad.json"
    target.parent.mkdir()
    target.write_text("not json")
    result = discover_workstream_evidence(tmp_path)
    assert "WORKSTREAM_EVIDENCE_MISSING:pmb" in result["diagnostics"]
