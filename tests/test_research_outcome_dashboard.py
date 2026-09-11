import json
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal

import pytest
import test_research_journal_dashboard as J

from kalshi_predictor.ui import research_journals as R
from kalshi_predictor.ui import research_outcomes as O

cohort = J.cohort


def raw(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def scored(p, y):
    assigned = p if y else 1 - p
    return dict(
        probability=str(p),
        outcome=y,
        brier=str((p - y) ** 2),
        probability_clipped=False,
        log_loss="POSITIVE_INFINITY" if assigned == 0 else -float(assigned.ln()),
    )


@pytest.fixture
def evidence(cohort):
    base, control = cohort
    capture = base / "slot-0"
    root = base / "slot-0-official-outcome"
    ev = root / "evaluation"
    ev.mkdir(parents=True)
    pins = json.loads((capture / "shadow-pins.json").read_bytes())
    with sqlite3.connect(capture / "research.db") as db:
        for pin in pins["decisions"]:
            body = json.loads(
                db.execute(
                    "SELECT payload FROM research_shadow WHERE id=?", (pin["decision_id"],)
                ).fetchone()[0]
            )
            body["decision"]["rows"] = [
                dict(side="YES", executable_price=".4"),
                dict(side="NO", executable_price=".7"),
            ]
            data = raw(body)
            pin["payload_sha256"] = R.digest(data)
            db.execute(
                "UPDATE research_shadow SET payload=?,payload_sha=? WHERE id=?",
                (data, R.digest(data), pin["decision_id"]),
            )
            c = json.loads(
                db.execute(
                    "SELECT payload FROM research_completion WHERE id=?", (pin["decision_id"],)
                ).fetchone()[0]
            )
            c["payload_sha256"] = R.digest(data)
            craw = raw(c)
            pin["completion_sha256"] = R.digest(craw)
            db.execute(
                "UPDATE research_completion SET payload=?,payload_sha=? WHERE id=?",
                (craw, R.digest(craw), pin["decision_id"]),
            )
    (capture / "shadow-pins.json").write_bytes(raw(pins))
    r = json.loads((capture / "shadow-pins.recorded.json").read_bytes())
    r["sha256"] = R.digest(raw(pins))
    (capture / "shadow-pins.recorded.json").write_bytes(raw(r))
    completion = json.loads((capture / "completion.json").read_bytes())
    for name in ("shadow-pins.json", "shadow-pins.recorded.json"):
        completion["files"][name] = R.digest((capture / name).read_bytes())
    manifest = {}
    markets = {}
    labels = {}

    def save(name, body):
        data = body if isinstance(body, bytes) else raw(body)
        path = ev / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        manifest[name] = R.digest(data)
        return data

    for i, ticker in enumerate(["T1", "T2"]):
        m = dict(
            ticker=ticker,
            event_ticker="E0",
            market_type="binary",
            strike_type="between",
            floor_strike="1",
            cap_strike="2",
            custom_strike={},
            rules_primary="synthetic",
            close_time="2026-09-11T20:00:00+00:00",
        )
        markets[ticker] = m
        original = raw(dict(market=m))
        name = f"{i}-market.original"
        completion["files"][name] = R.digest(original)
        save("capture/" + name, original)
        final = dict(
            m,
            status="finalized",
            result="no",
            settlement_value_dollars="0",
            settlement_ts="2026-09-11T20:02:00+00:00",
        )
        fraw = save(f"official/{i}.json", dict(market=final))
        receipt = dict(
            method="GET",
            http_status=200,
            original_complete=True,
            source_sha256=R.digest(fraw),
            url="https://external-api.kalshi.com/trade-api/v2/markets/" + ticker,
            requested_at="2026-09-11T20:05:00+00:00",
            received_at="2026-09-11T20:05:01+00:00",
        )
        rraw = save(f"official/{i}.receipt.json", receipt)
        labels[ticker] = dict(
            original_sha256=R.digest(fraw),
            receipt_sha256=R.digest(rraw),
            received_at=receipt["received_at"],
            settlement_at=final["settlement_ts"],
        )
    completion_raw = raw(completion)
    (capture / "completion.json").write_bytes(completion_raw)
    external = json.loads((control / "slot-0.completion-pin.json").read_bytes())
    external["completion_sha256"] = R.digest(completion_raw)
    (control / "slot-0.completion-pin.json").write_bytes(raw(external))
    save("capture/completion.json", completion_raw)
    code = save("evaluator.original.py", b"# synthetic reviewed evaluator bytes")
    (control / "outcome-source-pins.json").write_bytes(
        raw(
            dict(
                source_sha256={O.EVALUATOR: R.digest(code)},
                frozen_at="2026-09-11T19:00:00+00:00",
                execution_authority=False,
            )
        )
    )
    rows = []
    for pin in pins["decisions"]:
        rows.append(
            dict(
                pin,
                event="E0",
                symbol="SOL",
                scenario=dict(
                    index="SOLUSD_RTI",
                    comparator="RANGE_CLOSED",
                    cadence_ms=1000,
                    ticks=60,
                    decimal_places=4,
                    rounding="HALF_EVEN",
                    include_start=pin["hypothesis"] == "LEFT_CLOSED_RIGHT_OPEN",
                    include_end=pin["hypothesis"] == "LEFT_OPEN_RIGHT_CLOSED",
                ),
                target_at="2026-09-11T20:00:00+00:00",
                source=labels[pin["ticker"]],
                frozen_probability=".25",
                average=scored(Decimal(".25"), 0),
                market_midpoint=scored(Decimal(".35"), 0),
                market_baseline_method="SAME_BOOK_ONE_CONTRACT_BID_ASK_MIDPOINT",
                net_ev=None,
            )
        )
    save(
        "evaluation.json",
        dict(
            schema="crypto-shadow-evaluation-v1",
            capture_completion_sha256=R.digest(completion_raw),
            evaluated_at="2026-09-11T20:05:02+00:00",
            event_count=1,
            independent_n=None,
            paper_pnl=None,
            execution_authority=False,
            calibration_status="DESCRIPTIVE_ONLY",
            rows=rows,
        ),
    )
    (ev / "recording_receipt.json").write_bytes(
        raw(
            dict(
                status="COMPLETE",
                files=manifest,
                evaluator_sha256=R.digest(code),
                recorded_after_artifacts="2026-09-11T20:05:03+00:00",
            )
        )
    )
    (root / "result.json").write_bytes(
        raw(
            dict(
                status="SCORED",
                states=[
                    dict(ticker=t, status="finalized", original_sha256=v["original_sha256"])
                    for t, v in labels.items()
                ],
                target_at="2026-09-11T20:00:00+00:00",
                paper_pnl=None,
                execution_authority=False,
            )
        )
    )
    return base, control, root


def view(evidence):
    return R.read_cohort(*evidence[:2], now=datetime(2026, 9, 11, 20, 6, tzinfo=UTC))


def test_actual_journal_to_official_outcome_projection(evidence):
    report = view(evidence)
    outcome = report["slots"][0]["outcome"]
    assert report["decisions"] == 4 and report["events"] == 1 and report["paper_eligible"] == 0
    assert outcome["status"] == "SCORED_OFFICIAL_RESEARCH"
    assert len(outcome["labels"]) == 2 and all(x["outcome"] == "NO" for x in outcome["labels"])
    assert len(outcome["scenarios"]) == 2
    assert all(
        s["events"] == 1 and s["metrics"]["average"]["brier"] == "0.0625"
        for s in outcome["scenarios"]
    )
    assert "no paper positions" in R.render_cohort(report)


@pytest.mark.parametrize(
    "name",
    [
        "evaluation.json",
        "official/0.json",
        "official/0.receipt.json",
        "capture/completion.json",
        "evaluator.original.py",
    ],
)
def test_original_tamper_rejected(evidence, name):
    (evidence[2] / "evaluation" / name).write_bytes(b"{}")
    assert view(evidence)["slots"][0]["outcome"]["status"] == "UNVERIFIED_RESEARCH_EVALUATION"
    assert view(evidence)["decisions"] == 4


def mutate_evaluation(evidence, change):
    path = evidence[2] / "evaluation/evaluation.json"
    value = json.loads(path.read_bytes())
    change(value)
    path.write_bytes(raw(value))
    receipt = evidence[2] / "evaluation/recording_receipt.json"
    r = json.loads(receipt.read_bytes())
    r["files"]["evaluation.json"] = R.digest(path.read_bytes())
    receipt.write_bytes(raw(r))


@pytest.mark.parametrize(
    "field,value",
    [
        ("decision_id", "wrong"),
        ("payload_sha256", "0" * 64),
        ("completion_sha256", "0" * 64),
        ("frozen_probability", ".26"),
        ("ticker", "OTHER"),
    ],
)
def test_resigned_evaluation_cannot_change_frozen_identity(evidence, field, value):
    mutate_evaluation(evidence, lambda e: e["rows"][0].update({field: value}))
    assert view(evidence)["slots"][0]["outcome"]["status"] == "UNVERIFIED_RESEARCH_EVALUATION"


def test_false_metric_refused_even_when_receipt_rehashed(evidence):
    mutate_evaluation(evidence, lambda e: e["rows"][0]["average"].update(brier="0"))
    assert view(evidence)["slots"][0]["outcome"]["status"] == "UNVERIFIED_RESEARCH_EVALUATION"


def test_failure_and_pending_never_paper_settlement(evidence):
    (evidence[2] / "evaluation/failure.json").write_bytes(b"{}")
    assert view(evidence)["slots"][0]["outcome"]["status"] == "FAILED_RESEARCH_EVALUATION"
    (evidence[2] / "evaluation/failure.json").unlink()
    (evidence[2] / "result.json").unlink()
    assert view(evidence)["slots"][0]["outcome"]["status"] == "PENDING_OFFICIAL_RESEARCH"
    assert view(evidence)["paper_eligible"] == 0


def test_wrong_zero_probability_loss_keeps_infinity():
    result = O.score(scored(Decimal("0"), 1), Decimal("0"), 1)
    assert result == (Decimal("1"), "POSITIVE_INFINITY")


def test_oversize_evaluation_refused(evidence):
    (evidence[2] / "evaluation/evaluation.json").write_bytes(b" " * (R.SMALL + 1))
    assert view(evidence)["slots"][0]["outcome"]["status"] == "UNVERIFIED_RESEARCH_EVALUATION"


def test_collector_nonfinal_status_preserved(evidence):
    path = evidence[2] / "result.json"
    item = json.loads(path.read_bytes())
    item["status"] = "PENDING_OR_UNAVAILABLE_NO_RETRY"
    path.write_bytes(raw(item))
    assert view(evidence)["slots"][0]["outcome"]["status"] == "PENDING_OR_UNAVAILABLE_NO_RETRY"
