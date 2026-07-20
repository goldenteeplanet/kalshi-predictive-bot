from pathlib import Path

from kalshi_predictor.ui.collector_invocation_preview import certify_invocation_preview


def test_preview_has_exact_invocation_and_safety_guards():
    report = certify_invocation_preview(
        Path("deploy/systemd/kalshi-ui-status-collector.service.ui-obs5fa.preview")
    )
    assert report["status"] == "PASSED"
    assert report["failures"] == []
    assert report["guardrails"]["deployment_performed"] is False
    assert report["guardrails"]["execution_enabled"] is False
    assert report["exact_mapping"]["scheduler_service"] == "kalshi-r5-bounded.service"


def test_old_watcher_override_fails(tmp_path: Path):
    source = Path("deploy/systemd/kalshi-ui-status-collector.service.ui-obs5fa.preview").read_text()
    bad = tmp_path / "bad.service"
    bad.write_text(source.replace("--service kalshi-r5-bounded.service", "--service kalshi-r5-watcher.service"))
    report = certify_invocation_preview(bad)
    assert report["status"] == "FAILED"
    assert any(code.startswith("REQUIRED_ARGUMENT_MISSING") for code in report["failures"])
