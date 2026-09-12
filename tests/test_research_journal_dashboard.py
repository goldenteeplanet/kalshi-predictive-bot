import json
import sqlite3
from datetime import UTC, datetime

import pytest

from kalshi_predictor.ui import research_journals as R


def raw(value):
    return json.dumps(value).encode()


@pytest.fixture
def cohort(tmp_path):
    base, control = tmp_path / "cohort", tmp_path / "control"
    base.mkdir()
    control.mkdir()
    slots = []
    for i in range(5):
        plan = dict(
            schema="cf-average-prospective-slot-v1",
            event_ticker=f"E{i}",
            target_at=f"2026-09-11T{20 + i:02}:00:00+00:00"
            if i < 4
            else "2026-09-12T00:00:00+00:00",
            not_before=f"2026-09-11T{19 + i:02}:50:00+00:00",
            not_after=f"2026-09-11T{19 + i:02}:50:50+00:00",
        )
        data = raw(plan)
        (control / f"slot-{i}.protocol.json").write_bytes(data)
        slots.append(dict(slot=i, protocol_sha256=R.digest(data)))
    (control / "registration.json").write_bytes(
        raw(dict(status="REGISTERED_BEFORE_CAPTURE", slots=slots))
    )
    cap = base / "slot-0"
    cap.mkdir()
    plan_raw = (control / "slot-0.protocol.json").read_bytes()
    plan = json.loads(plan_raw)
    selected = ["T1", "T2"]
    pins = []
    db = sqlite3.connect(cap / "research.db")
    db.executescript(
        "PRAGMA application_id=1129468744; CREATE TABLE research_shadow(id TEXT,payload "
        "BLOB,payload_sha TEXT); CREATE TABLE research_completion(id TEXT,"
        "payload BLOB,payload_sha TEXT);"
    )
    for t in selected:
        for h in ["LEFT_CLOSED_RIGHT_OPEN", "LEFT_OPEN_RIGHT_CLOSED"]:
            ident = R.digest((t + h).encode())
            decision = dict(
                decision_id=ident,
                event="E0",
                ticker=t,
                rule_version="rule",
                request_sha256="request",
                decision_at="2026-09-11T19:50:01+00:00",
                computed_at="2026-09-11T19:50:02+00:00",
                paper_eligible=False,
                execution_authority=False,
                forecast=dict(probability=0.25),
            )
            body = raw(dict(decision=decision))
            completion = raw(
                dict(
                    decision_id=ident,
                    payload_sha256=R.digest(body),
                    status="COMPLETE_RESEARCH",
                    original_committed_before="2026-09-11T19:50:03+00:00",
                )
            )
            db.execute("INSERT INTO research_shadow VALUES(?,?,?)", (ident, body, R.digest(body)))
            db.execute(
                "INSERT INTO research_completion VALUES(?,?,?)",
                (ident, completion, R.digest(completion)),
            )
            pins.append(
                dict(
                    decision_id=ident,
                    ticker=t,
                    hypothesis=h,
                    rule_version="rule",
                    request_sha256="request",
                    payload_sha256=R.digest(body),
                    completion_sha256=R.digest(completion),
                )
            )
    db.commit()
    db.close()
    pinraw = raw(
        dict(schema="cf-shadow-pins-v1", event="E0", target=plan["target_at"], decisions=pins)
    )
    files = {
        "plan.original.json": plan_raw,
        "shadow-pins.json": pinraw,
        "selection.json": raw(dict(selected=selected)),
        "shadow-pins.recorded.json": raw(
            dict(sha256=R.digest(pinraw), recorded_at="2026-09-11T19:50:04+00:00")
        ),
    }
    for name, data in files.items():
        (cap / name).write_bytes(data)
    complete = raw(
        dict(
            status="COMPLETE",
            recorded_after_result="2026-09-11T19:50:05+00:00",
            files={k: R.digest(v) for k, v in files.items()},
        )
    )
    (cap / "completion.json").write_bytes(complete)
    (control / "slot-0.completion-pin.json").write_bytes(
        raw(
            dict(
                schema="cf-cohort-external-completion-pin-v1",
                slot=0,
                observed_at="2026-09-11T19:50:06+00:00",
                target_at=plan["target_at"],
                execution_authority=False,
                protocol_sha256=R.digest(plan_raw),
                completion_sha256=R.digest(complete),
            )
        )
    )
    return base, control


def view(cohort):
    return R.read_cohort(*cohort, now=datetime(2026, 9, 11, 20, tzinfo=UTC))


def test_actual_readonly_journal_four_scenarios_one_event(cohort):
    before = {str(p): p.read_bytes() for root in cohort for p in root.rglob("*") if p.is_file()}
    report = view(cohort)
    assert report["decisions"] == 4 and report["events"] == 1 and report["paper_eligible"] == 0
    assert report["slots"][1]["status"] == "SCHEDULED"
    assert before == {
        str(p): p.read_bytes() for root in cohort for p in root.rglob("*") if p.is_file()
    }


@pytest.mark.parametrize("artifact", ["completion.json", "shadow-pins.json", "selection.json"])
def test_changed_original_refused(cohort, artifact):
    (cohort[0] / "slot-0" / artifact).write_bytes(b"{}")
    assert view(cohort)["decisions"] == 0


def test_payload_and_completion_tamper(cohort):
    with sqlite3.connect(cohort[0] / "slot-0/research.db") as db:
        db.execute("UPDATE research_shadow SET payload=?", (b"{}",))
    assert view(cohort)["decisions"] == 0


def test_failure_dominates_and_missing_external_pin(cohort):
    (cohort[1] / "slot-0.completion-pin.json").unlink()
    assert view(cohort)["slots"][0]["status"] == "PENDING_UNVERIFIED"
    (cohort[0] / "slot-0/failure.json").write_bytes(b"{}")
    assert view(cohort)["slots"][0]["status"] == "FAILED_CAPTURE"


def test_missing_db_never_created(cohort):
    path = cohort[0] / "slot-0/research.db"
    path.unlink()
    assert view(cohort)["decisions"] == 0
    assert not path.exists()


def test_bounds_and_xss(cohort):
    (cohort[1] / "registration.json").write_bytes(b" " * (R.SMALL + 1))
    assert view(cohort)["decisions"] == 0
    text = R.render_cohort(
        dict(decisions=0, events=0, slots=[dict(slot=0, status="<script>", event="<img>", rows=[])])
    )
    assert "<script>" not in text and "&lt;script&gt;" in text


@pytest.mark.parametrize("value", [b'{"a":1,"a":2}', b'{"a":1e999}', b'{"a":NaN}'])
def test_strict_json(value):
    with pytest.raises(ValueError):
        R.decode(value)


def test_optional_route_keeps_old_metrics_separate(cohort, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from kalshi_predictor.ui.positive_ev import create_router

    monkeypatch.setenv("POSITIVE_EV_COHORT_ROOT", str(cohort[0]))
    monkeypatch.setenv("POSITIVE_EV_CONTROL_ROOT", str(cohort[1]))
    projection = view(cohort)
    monkeypatch.setattr(R, "read_cohort", lambda *args: projection)
    app = FastAPI()
    app.include_router(create_router())
    with TestClient(app) as client:
        response = client.get("/positive-ev")
        assert response.status_code == 200
        assert "4 durable decisions; 1 temporal events" in response.text
        assert "2026-09-11T20:00:00+00:00" in response.text
        assert "Existing report metrics are separate" in response.text
        assert client.post("/positive-ev").status_code == 405


def test_wrong_external_pin_slot_and_late_clock(cohort):
    path = cohort[1] / "slot-0.completion-pin.json"
    original = json.loads(path.read_bytes())
    for key, value in [
        ("slot", True),
        ("observed_at", "2026-09-11T20:00:00+00:00"),
        ("completion_sha256", "0" * 64),
    ]:
        item = dict(original)
        item[key] = value
        path.write_bytes(raw(item))
        assert view(cohort)["decisions"] == 0


def test_more_than_four_records_refused(cohort):
    with sqlite3.connect(cohort[0] / "slot-0/research.db") as db:
        db.execute("INSERT INTO research_shadow VALUES(?,?,?)", ("extra", b"{}", "x"))
    assert view(cohort)["decisions"] == 0


@pytest.mark.parametrize("value", [None, True, [], {}])
def test_malformed_clock_refuses(value):
    with pytest.raises(ValueError, match="CLOCK_STRING_REQUIRED"):
        R.clock(value)
