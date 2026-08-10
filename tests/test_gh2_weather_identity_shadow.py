from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

from kalshi_predictor.config import get_settings
from kalshi_predictor.gh2_weather_identity_shadow import append_weather_identity_shadow


def _report() -> dict:
    return {
        "candidate_alignment": {"tickers": ["KXBTC-KEEP"]},
        "paper_readiness": {"total_paper_ready_candidates": 0},
        "weather_gate": {
            "status": "PAPER_GATE_BLOCKED",
            "summary": {"paper_ready_rows": 0},
            "next_action": {"reason": "LINK_EXACT_CATALOG_URL_UNCONFIRMED"},
            "weather_rows": [
                {
                    "ticker": "KXTEMPNYCH-TEST-T90",
                    "first_blocker": "LINK_EXACT_CATALOG_URL_UNCONFIRMED",
                    "paper_ready": False,
                    "kalshi_url_verified": False,
                    "gross_ev": "0.01",
                    "executable_book": False,
                }
            ],
        },
    }


def _write_report(tmp_path: Path, payload: dict) -> tuple[Path, Path]:
    report = tmp_path / "gh2.json"
    markdown = tmp_path / "gh2.md"
    report.write_text(json.dumps(payload), encoding="utf-8")
    markdown.write_text("# GH-2\n", encoding="utf-8")
    return report, markdown


def test_natural_cycle_shadow_enriches_diagnostics_without_selection_changes(
    tmp_path: Path,
) -> None:
    original = _report()
    report_path, markdown_path = _write_report(tmp_path, original)

    def collect(tickers: list[str]) -> dict:
        assert tickers == ["KXTEMPNYCH-TEST-T90"]
        return {
            "rows": [
                {
                    "ticker": tickers[0],
                    "authoritative_identity_verified": True,
                    "evidence_class": "EXACT_EVENT_AND_SERIES_CATALOG",
                    "source_identity": {
                        "market_ticker": tickers[0],
                        "event_ticker": "KXTEMPNYCH-TEST",
                        "series_ticker": "KXTEMPNYCH",
                    },
                    "source_sha256": {
                        "market": "a" * 64,
                        "event": "b" * 64,
                        "series": "c" * 64,
                    },
                    "fetched_at": "2026-08-10T12:00:00+00:00",
                    "freshness_status": "FRESH",
                    "reason": "AUTHORITATIVE_IDENTITY_VERIFIED",
                }
            ],
            "summary": {
                "requested": 1,
                "authoritative_identity_verified": 1,
                "blocked": 0,
                "reasons": {"AUTHORITATIVE_IDENTITY_VERIFIED": 1},
            },
        }

    append_weather_identity_shadow(
        report_path=report_path,
        markdown_path=markdown_path,
        settings=get_settings(),
        writer_monitor=lambda: {"writer_count": 0, "safe_to_start_write": True},
        collector=collect,
    )

    rendered = json.loads(report_path.read_text(encoding="utf-8"))
    row = rendered["weather_gate"]["weather_rows"][0]
    assert row["authoritative_identity_verified"] is True
    assert row["evidence_class"] == "EXACT_EVENT_AND_SERIES_CATALOG"
    assert row["kalshi_url_verified"] is False
    assert row["first_blocker"] == "LINK_EXACT_CATALOG_URL_UNCONFIRMED"
    assert row["paper_ready"] is False
    assert rendered["candidate_alignment"] == original["candidate_alignment"]
    assert rendered["paper_readiness"] == original["paper_readiness"]
    assert "Candidate selection and paper readiness: `UNCHANGED`" in markdown_path.read_text(
        encoding="utf-8"
    )


def test_shadow_defers_before_collector_when_writer_is_active(tmp_path: Path) -> None:
    report_path, markdown_path = _write_report(tmp_path, _report())
    calls = 0

    def collect(tickers: list[str]) -> dict:
        nonlocal calls
        calls += 1
        return {}

    shadow = append_weather_identity_shadow(
        report_path=report_path,
        markdown_path=markdown_path,
        settings=get_settings(),
        writer_monitor=lambda: {"writer_count": 1, "safe_to_start_write": False},
        collector=collect,
    )

    assert calls == 0
    assert shadow["status"] == "DEFERRED"
    assert shadow["safety"]["database_opened"] is False
    assert shadow["safety"]["database_writes"] == 0
    row = json.loads(report_path.read_text(encoding="utf-8"))["weather_gate"][
        "weather_rows"
    ][0]
    assert row["authoritative_identity_verified"] is False
    assert row["freshness_status"] == "NOT_VERIFIED"
    assert row["authoritative_identity_reason"] == "DEFERRED_ACTIVE_WRITER"


def test_shadow_is_idempotent_and_preserves_authoritative_gate_fields(tmp_path: Path) -> None:
    original = _report()
    report_path, markdown_path = _write_report(tmp_path, original)
    authority_before = deepcopy(original["weather_gate"])
    def deferred() -> dict:
        return {"writer_count": 1, "safe_to_start_write": False}

    for _ in range(2):
        append_weather_identity_shadow(
            report_path=report_path,
            markdown_path=markdown_path,
            settings=get_settings(),
            writer_monitor=deferred,
        )

    rendered = json.loads(report_path.read_text(encoding="utf-8"))
    row = rendered["weather_gate"]["weather_rows"][0]
    source = authority_before["weather_rows"][0]
    for field in (
        "first_blocker",
        "paper_ready",
        "kalshi_url_verified",
        "gross_ev",
        "executable_book",
    ):
        assert row[field] == source[field]
    markdown = markdown_path.read_text(encoding="utf-8")
    assert markdown.count("<!-- weather-identity-shadow:start -->") == 1


def test_scheduler_runs_shadow_in_post_lock_diagnostics() -> None:
    root = Path(__file__).parents[1]
    script = (root / "scripts/cloud/kalshi-gh2-decision-refresh.sh").read_text(
        encoding="utf-8"
    )
    cli = (root / "src/kalshi_predictor/cli.py").read_text(encoding="utf-8")

    assert script.index("flock -u 9") < script.index("roadmap-runtime-reports")
    assert '--gh2-report-path "$GH2_ROOT/reports/gh2_active_candidate_refresh.json"' in script
    assert '--gh2-markdown-path "$GH2_ROOT/reports/gh2_active_candidate_refresh.md"' in script
    assert "append_weather_identity_shadow(" in cli
    fast_path = cli.index('if command == "roadmap-runtime-reports":')
    typer_command = cli.index('@app.command("roadmap-runtime-reports")')
    assert "append_weather_identity_shadow(" in cli[fast_path:typer_command]
    assert cli[fast_path:typer_command].index(
        "write_runtime_roadmap_reports("
    ) < cli[fast_path:typer_command].index("append_weather_identity_shadow(")
