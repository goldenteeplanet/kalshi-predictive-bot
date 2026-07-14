from pathlib import Path
from types import SimpleNamespace

from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.economic.actuals import TRADING_ECONOMICS_ENV_NAMES
from kalshi_predictor.economic.evidence_activation import (
    write_phase3bd_r8_economic_evidence_activation_report,
)


def test_phase3bd_r8_blocks_without_source_and_writes_template(
    tmp_path,
    monkeypatch,
) -> None:
    _clear_consensus_env(monkeypatch)
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        artifacts = write_phase3bd_r8_economic_evidence_activation_report(
            session=session,
            output_dir=tmp_path / "r8",
            r7_output_dir=tmp_path / "r7",
            r7_writer=_fake_r7_writer(_calendar_only_r7_payload()),
            r5_writer=_raising_r5_writer,
        )

    summary = artifacts.payload["summary"]
    assert summary["status"] == "BLOCKED_BY_MISSING_VERIFIED_CONSENSUS_SOURCE"
    assert summary["source_mode"] == "NONE"
    assert summary["r5_ran"] is False
    assert summary["template_rows_written"] == 1
    template = artifacts.template_csv_path.read_text(encoding="utf-8")
    assert "forecast_value" in template
    assert "NEEDS_VERIFIED_SOURCE" in template
    assert artifacts.template_json_path.exists()
    assert artifacts.payload["live_demo_execution"] == "blocked"
    assert artifacts.payload["order_submission_cancel_replace"] == "blocked"


def test_phase3bd_r8_blocks_missing_verified_input_file(tmp_path, monkeypatch) -> None:
    _clear_consensus_env(monkeypatch)
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        artifacts = write_phase3bd_r8_economic_evidence_activation_report(
            session=session,
            output_dir=tmp_path / "r8",
            input_file=tmp_path / "missing.csv",
            r7_output_dir=tmp_path / "r7",
            r7_writer=_fake_r7_writer(_calendar_only_r7_payload()),
            r5_writer=_raising_r5_writer,
        )

    assert artifacts.payload["summary"]["status"] == "BLOCKED_BY_MISSING_VERIFIED_INPUT_FILE"
    assert artifacts.payload["summary"]["r5_ran"] is False
    assert artifacts.payload["source_state"]["verified_input_file_exists"] is False


def test_phase3bd_r8_runs_r5_and_reruns_r7_with_verified_file(
    tmp_path,
    monkeypatch,
) -> None:
    _clear_consensus_env(monkeypatch)
    verified = tmp_path / "verified.csv"
    verified.write_text(
        "event_key,event_time,source_url,forecast_value,actual_value\n"
        "cpi,2026-07-01T12:00:00+00:00,https://example.test/cpi,3.2,3.5\n",
        encoding="utf-8",
    )
    r7_payloads = [
        _calendar_only_r7_payload(),
        _actual_consensus_r7_payload(preflight_ready_rows=0),
    ]
    r5_calls: list[dict] = []
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        artifacts = write_phase3bd_r8_economic_evidence_activation_report(
            session=session,
            output_dir=tmp_path / "r8",
            input_file=verified,
            r5_output_dir=tmp_path / "r5",
            r7_output_dir=tmp_path / "r7",
            r7_writer=_sequence_r7_writer(r7_payloads),
            r5_writer=_tracking_r5_writer(r5_calls),
        )

    summary = artifacts.payload["summary"]
    assert len(r5_calls) == 1
    assert r5_calls[0]["force_refresh"] is True
    assert summary["status"] == "ACTUAL_CONSENSUS_LOADED_BUT_R7_BLOCKED"
    assert summary["source_mode"] == "VERIFIED_INPUT_FILE"
    assert summary["source_evidence_ready_rows"] == 1
    assert summary["preflight_ready_rows"] == 0


def test_phase3bd_r8_reports_preflight_ready_without_live_execution(
    tmp_path,
    monkeypatch,
) -> None:
    _clear_consensus_env(monkeypatch)
    verified = tmp_path / "verified.csv"
    verified.write_text(
        "event_key,event_time,source_url,forecast_value,actual_value\n"
        "cpi,2026-07-01T12:00:00+00:00,https://example.test/cpi,3.2,3.5\n",
        encoding="utf-8",
    )
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        artifacts = write_phase3bd_r8_economic_evidence_activation_report(
            session=session,
            output_dir=tmp_path / "r8",
            input_file=verified,
            r7_output_dir=tmp_path / "r7",
            r7_writer=_sequence_r7_writer(
                [
                    _calendar_only_r7_payload(),
                    _actual_consensus_r7_payload(preflight_ready_rows=1),
                ]
            ),
            r5_writer=_tracking_r5_writer([]),
        )

    assert artifacts.payload["summary"]["status"] == "R7_PREFLIGHT_READY"
    assert artifacts.payload["summary"]["preflight_ready_rows"] == 1
    assert artifacts.payload["summary"]["phase3m_phase3n_preflight_recorded"] == 0
    assert artifacts.payload["live_demo_execution"] == "blocked"


def _fake_r7_writer(payload):
    def _writer(*args, **kwargs):
        del args, kwargs
        return SimpleNamespace(
            payload=payload,
            json_path=Path("r7.json"),
            markdown_path=Path("r7.md"),
            rows_path=Path("r7_rows.json"),
            preflight_rows_path=Path("r7_preflight.json"),
        )

    return _writer


def _sequence_r7_writer(payloads):
    calls = {"count": 0}

    def _writer(*args, **kwargs):
        del args, kwargs
        index = min(calls["count"], len(payloads) - 1)
        calls["count"] += 1
        return SimpleNamespace(
            payload=payloads[index],
            json_path=Path(f"r7_{index}.json"),
            markdown_path=Path(f"r7_{index}.md"),
            rows_path=Path(f"r7_rows_{index}.json"),
            preflight_rows_path=Path(f"r7_preflight_{index}.json"),
        )

    return _writer


def _tracking_r5_writer(calls: list[dict]):
    def _writer(*args, **kwargs):
        del args
        calls.append(kwargs)
        payload = {
            "summary": {
                "status": "R4_ACTIVE_WITH_VERIFIED_CONSENSUS",
                "consensus_value_observations": 1,
                "actual_and_consensus_observations": 1,
                "features_inserted": 1,
                "forecasts_inserted": 1,
                "rankings_inserted": 1,
            },
            "recommended_next_action": "fixture",
        }
        return SimpleNamespace(
            payload=payload,
            json_path=Path("r5.json"),
            markdown_path=Path("r5.md"),
            history_path=Path("r5.jsonl"),
        )

    return _writer


def _raising_r5_writer(*args, **kwargs):
    del args, kwargs
    raise AssertionError("R5 should not run without a configured source")


def _calendar_only_r7_payload() -> dict:
    return {
        "generated_at": "2026-07-01T12:00:00+00:00",
        "summary": {
            "status": "WAITING_FOR_ACTUAL_CONSENSUS_EVIDENCE",
            "primary_gap": "MISSING_CONSENSUS_EVIDENCE",
            "economic_rankings_scanned": 1,
            "source_evidence_ready_rows": 0,
            "positive_ev_rows": 0,
            "clean_execution_rows": 1,
            "risk_ready_rows": 0,
            "preflight_ready_rows": 0,
            "phase3m_phase3n_preflight_recorded": 0,
        },
        "evidence_state_counts": {"CALENDAR_ONLY": 1},
        "blocker_counts": {"MISSING_CONSENSUS_EVIDENCE": 1},
        "rows": [
            {
                "ticker": "KXCPI-R8",
                "title": "Core CPI above consensus?",
                "event_ticker": "KXCPI-R8-EVENT",
                "series_ticker": "KXCPI",
                "market_status": "open",
                "ranked_at": "2026-07-01T12:00:00+00:00",
                "economic_evidence_state": "CALENDAR_ONLY",
                "economic_evidence": {
                    "state": "CALENDAR_ONLY",
                    "event_key": "cpi",
                    "event_title": "Core CPI release",
                    "event_time": "2026-07-01T12:00:00+00:00",
                    "source_url": None,
                    "actual_value": None,
                    "forecast_value": None,
                    "previous_value": None,
                    "actual_and_consensus": False,
                },
                "blockers": ["MISSING_CONSENSUS_EVIDENCE"],
            }
        ],
    }


def _actual_consensus_r7_payload(*, preflight_ready_rows: int) -> dict:
    return {
        "generated_at": "2026-07-01T12:02:00+00:00",
        "summary": {
            "status": "PREFLIGHT_READY" if preflight_ready_rows else "WAITING_FOR_EV",
            "primary_gap": None if preflight_ready_rows else "EV_NOT_POSITIVE",
            "economic_rankings_scanned": 1,
            "source_evidence_ready_rows": 1,
            "positive_ev_rows": preflight_ready_rows,
            "clean_execution_rows": 1,
            "risk_ready_rows": 0,
            "preflight_ready_rows": preflight_ready_rows,
            "phase3m_phase3n_preflight_recorded": 0,
        },
        "evidence_state_counts": {"ACTUAL_AND_CONSENSUS": 1},
        "blocker_counts": {} if preflight_ready_rows else {"EV_NOT_POSITIVE": 1},
        "rows": [
            {
                "ticker": "KXCPI-R8",
                "title": "Core CPI above consensus?",
                "economic_evidence_state": "ACTUAL_AND_CONSENSUS",
                "economic_evidence": {
                    "state": "ACTUAL_AND_CONSENSUS",
                    "event_key": "cpi",
                    "event_title": "Core CPI release",
                    "event_time": "2026-07-01T12:00:00+00:00",
                    "source_url": "https://example.test/cpi",
                    "actual_value": "3.5",
                    "forecast_value": "3.2",
                    "previous_value": "3.1",
                    "actual_and_consensus": True,
                },
                "blockers": [] if preflight_ready_rows else ["EV_NOT_POSITIVE"],
            }
        ],
    }


def _session_factory(tmp_path: Path):
    engine = init_db(f"sqlite:///{tmp_path / 'phase3bd_r8.db'}")
    return get_session_factory(engine)


def _clear_consensus_env(monkeypatch) -> None:
    for name in TRADING_ECONOMICS_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
