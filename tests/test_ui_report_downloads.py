from dataclasses import asdict

import pytest
from fastapi.testclient import TestClient

from kalshi_predictor.config import Settings
from kalshi_predictor.ui.app import create_app
from kalshi_predictor.ui.report_files import ALLOWED_REPORTS, report_available
from kalshi_predictor.ui.routes import templates
from kalshi_predictor.ui.service import REPORT_LINKS


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "reports").mkdir()

    # Report downloads have no database dependency and must stay that way.
    def no_database():
        raise AssertionError("A report download must not open a database")

    with TestClient(
        create_app(session_factory=no_database, settings=Settings(_env_file=None))
    ) as value:
        yield value


@pytest.mark.parametrize(
    "name",
    [
        "live_readiness_report.md",
        "link_coverage_report.md",
        "tonight_report.md",
        "system_certification/system_certification_report.md",
        "model_repair/model_repair_audit.md",
        "model_repair/metrics_reconciliation.md",
        "market_coverage/market_coverage_doctor.md",
        "phase3ao/opportunity_link_audit.md",
    ],
)
def test_supported_report_download_returns_original_bytes(client, tmp_path, name):
    path = tmp_path / "reports" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    original = b"# Preserved report\nExact original evidence.\n"
    path.write_bytes(original)
    response = client.get("/reports/" + name)
    assert response.status_code == 200
    assert response.content == original
    assert path.read_bytes() == original


def test_report_registry_covers_every_advertised_report():
    assert all(
        href.removeprefix("/reports/") in ALLOWED_REPORTS for href in asdict(REPORT_LINKS).values()
    )


def test_missing_reports_are_unavailable_without_fabricating_files(client, tmp_path):
    href = "/reports/research_report.md"
    assert client.get(href).status_code == 404
    assert not report_available(href)
    template = templates.env.get_template("report_link.html")
    html = str(template.module.report_link(href, "Research report"))
    assert "Unavailable" in html and "Research report" in html
    assert "href=" not in html
    assert not list((tmp_path / "reports").iterdir())
    (tmp_path / "reports/research_report.md").write_text("Now available")
    assert report_available(href)
    assert 'href="/reports/research_report.md"' in str(
        template.module.report_link(href, "Research report")
    )


@pytest.mark.parametrize(
    "name",
    [
        "private.txt",
        "../private.txt",
        "..%2Fprivate.txt",
        "..%5Cprivate.txt",
        "model_repair/../../private.txt",
    ],
)
def test_unknown_and_traversal_report_paths_are_rejected(client, tmp_path, name):
    (tmp_path / "private.txt").write_text("PRIVATE")
    response = client.get("/reports/" + name)
    assert response.status_code == 404
    assert "PRIVATE" not in response.text


def test_directory_and_escaping_symlink_are_not_downloadable(client, tmp_path):
    (tmp_path / "reports/research_report.md").mkdir()
    assert client.get("/reports/research_report.md").status_code == 404
    external = tmp_path / "private.txt"
    external.write_text("PRIVATE")
    try:
        (tmp_path / "reports/live_readiness_report.md").symlink_to(external)
    except OSError:
        pytest.skip("Symlink creation requires Windows developer mode or privilege")
    assert client.get("/reports/live_readiness_report.md").status_code == 404
    assert not report_available("/reports/live_readiness_report.md")
