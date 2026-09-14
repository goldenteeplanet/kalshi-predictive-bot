"""Actual owner, public runner and receipt replay; HTTP is intercepted locally."""

import json
from dataclasses import replace
from datetime import datetime, timedelta
from unittest.mock import Mock

import httpx
import pytest
import test_overnight_activation as fixtures
from sqlalchemy import text

from kalshi_predictor.overnight_paper import monitoring, settlement_runner
from kalshi_predictor.overnight_paper.runtime_owner import acquire_runtime_owner

baseline_template = fixtures.baseline_template
prepared = fixtures.prepared


@pytest.fixture
def monitored(prepared, monkeypatch):
    now = prepared["now"]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    monkeypatch.setattr(settlement_runner, "datetime", Clock)
    monkeypatch.setattr(monitoring, "_now", lambda: now)
    real_client = httpx.Client
    requests = []

    def handler(request):
        requests.append(request)
        assert request.method == "GET"
        assert "authorization" not in request.headers
        assert not any("kalshi-access" in key for key in request.headers)
        shadow = prepared["shadow_payload"]
        return httpx.Response(200, json={"market": {
            "ticker": shadow["ticker"], "event_ticker": shadow["event_ticker"],
            "series_ticker": shadow["series_ticker"], "status": "active",
            "close_time": shadow["close_time"],
        }})

    monkeypatch.setattr(settlement_runner.httpx, "Client", lambda **kwargs: real_client(
        **{**kwargs, "transport": httpx.MockTransport(handler)}
    ))
    with acquire_runtime_owner(prepared["database_path"]) as owner:
        result = monitoring.monitor_owned_once(
            owner=owner, session_factory=prepared["session_factory"], code_sha="a" * 40,
        )
        assert len(result.permits) == 1
        assert result.report.cycles_completed == 1
        assert len(requests) == 1
        yield prepared, owner, result.permits[0]
    monitoring.revoke_monitoring(owner)


def verify(prepared, permit):
    with prepared["session_factory"]() as session:
        session.execute(text("BEGIN IMMEDIATE"))
        before = session.execute(text("SELECT count(*) FROM paper_orders")).scalar_one()
        result = monitoring.verify_monitoring(
            session, permit, database_path=prepared["database_path"],
            database_id=prepared["authorization"].database_id,
            code_sha="a" * 40, shadow_id=prepared["shadow_id"],
        )
        assert session.execute(text("SELECT count(*) FROM paper_orders")).scalar_one() == before
        return result


def test_live_owner_actual_public_open_receipt_passes_read_only(monitored):
    prepared, _, permit = monitored
    result = verify(prepared, permit)
    assert result.passed, result.blockers
    assert len(result.verified_hashes) == 1


def test_supplied_pass_and_copied_permit_do_not_grant_admission(monitored):
    prepared, _, permit = monitored
    for forged in (None, {"passed": True}, Mock(passed=True), replace(permit)):
        assert not verify(prepared, forged).passed


def test_revocation_and_failed_cycle_invalidate_previous_permit(monitored, monkeypatch):
    prepared, owner, permit = monitored

    def failed(**kwargs):
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(monitoring, "run_settlement_cycles", failed)
    with pytest.raises(httpx.ConnectError):
        monitoring.monitor_owned_once(
            owner=owner, session_factory=prepared["session_factory"], code_sha="a" * 40,
        )
    assert "MONITOR_PERMIT_NOT_ACTIVE" in verify(prepared, permit).blockers


def test_expired_or_rolled_back_clock_rejects(monitored, monkeypatch):
    prepared, _, permit = monitored
    for seconds in (-1, 61):
        monkeypatch.setattr(
            monitoring, "_now",
            lambda seconds=seconds: permit.completed_at + timedelta(seconds=seconds),
        )
        assert "MONITOR_PERMIT_EXPIRED_OR_CLOCK_INVALID" in verify(prepared, permit).blockers


def test_monotonic_expiry_rejects_even_with_frozen_wall_clock(monitored, monkeypatch):
    prepared, _, permit = monitored
    monkeypatch.setattr(monitoring.time, "monotonic", lambda: permit.monotonic_at + 61)
    assert "MONITOR_PERMIT_EXPIRED_OR_CLOCK_INVALID" in verify(prepared, permit).blockers


def test_changed_original_receipt_rejects(monitored):
    prepared, _, permit = monitored
    with prepared["session_factory"]() as session:
        raw = session.execute(text(
            "SELECT payload FROM overnight_sprint_cycles WHERE id=:key"
        ), {"key": permit.receipt_id}).scalar_one()
        row = json.loads(raw)
        row["source"]["sha256"] = "0" * 64
        session.execute(text("UPDATE overnight_sprint_cycles SET payload=:raw WHERE id=:key"),
                        {"raw": json.dumps(row), "key": permit.receipt_id})
        session.commit()
    assert "MONITOR_RECEIPT_INTEGRITY" in verify(prepared, permit).blockers


def test_unrelated_shadow_or_release_rejects(monitored):
    prepared, _, permit = monitored
    with prepared["session_factory"]() as session, session.begin():
        result = monitoring.verify_monitoring(
            session, permit, database_path=prepared["database_path"],
            database_id=prepared["authorization"].database_id,
            code_sha="b" * 40, shadow_id=prepared["shadow_id"],
        )
    assert "MONITOR_RELEASE_IDENTITY_MISMATCH" in result.blockers
