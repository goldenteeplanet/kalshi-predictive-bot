import json
import sqlite3
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta

import pytest
from test_settlement_rule_version import rule

from kalshi_predictor.crypto import multiasset_capture as C

NOW = datetime(2026, 9, 12, 2, 50, tzinfo=UTC)
TARGET = NOW + timedelta(minutes=10)
EVENT = "KXSOLE-26SEP1123"


def setup():
    first = rule()
    plan = dict(
        symbol="SOL",
        benchmark="SOLUSD_RTI",
        event=EVENT,
        target_at=TARGET.isoformat(),
        not_before=NOW.isoformat(),
        not_after=(NOW + timedelta(seconds=90)).isoformat(),
        max_gets=6,
        source_manifest=C.source_manifest(),
        seed=123,
        rule_hypotheses=[
            asdict(first),
            asdict(replace(first, include_start=False, include_end=True)),
        ],
    )
    markets = [
        dict(
            ticker=EVENT + "-B" + str(i),
            event_ticker=EVENT,
            market_type="binary",
            status="active",
            close_time=TARGET.isoformat(),
            strike_type="between",
            floor_strike=str(100 + i),
            cap_strike=str(101 + i),
        )
        for i in range(2)
    ]
    start = int(NOW.timestamp() * 1000) - 3600000
    cf = {
        "data": {
            "serverTime": NOW.isoformat(),
            "payload": [
                {"time": start + i * 1000, "value": "100.50" if i % 2 else "100.51"}
                for i in range(3600)
            ],
        }
    }
    calls = []

    def transport(url, timeout):
        calls.append(url)
        if "/cfbenchmarks/" in url:
            return 200, C.encode(cf)
        if "/orderbook" in url:
            return 200, C.encode(
                {"orderbook_fp": {"yes_dollars": [["0.40", "3"]], "no_dollars": [["0.55", "4"]]}}
            )
        return 200, C.encode({"markets": markets, "cursor": ""})

    return plan, transport, calls


def test_capture_commits_matched_decisions_and_hashed_originals(tmp_path):
    plan, transport, calls = setup()
    output = tmp_path / "capture"
    result = C.capture(output, plan, transport, clock=lambda: NOW)
    assert result["requests"] == len(calls) == 6
    assert result["decisions"] == 4
    complete = json.loads((output / "completion.json").read_bytes())
    for name, expected in complete["files"].items():
        assert C.digest((output / name).read_bytes()) == expected
    with sqlite3.connect(output / "research.db") as db:
        rows = db.execute("SELECT payload,sha256 FROM decisions").fetchall()
    assert len(rows) == 4
    for raw, digest in rows:
        assert C.digest(raw) == digest
        value = json.loads(raw)
        assert value["rule_status"] == "UNCERTIFIED"
        assert value["models"]["models"]["credible_book_midpoint_v1"]["probability"] == 0.425
        assert all(r["costs"] is None or r["costs"]["full_net_ev"] is None for r in value["rows"])
    with pytest.raises(FileExistsError):
        C.capture(output, plan, transport, clock=lambda: NOW)
    assert len(calls) == 6


def test_http_failure_is_terminal_without_fallback(tmp_path):
    plan, transport, calls = setup()

    def failing(url, timeout):
        status, raw = transport(url, timeout)
        return (503, raw) if len(calls) == 2 else (status, raw)

    with pytest.raises(ValueError, match="HTTP"):
        C.capture(tmp_path / "capture", plan, failing, clock=lambda: NOW)
    assert len(calls) == 2
    assert (tmp_path / "capture/failure.json").exists()
    assert not (tmp_path / "capture/completion.json").exists()


def test_source_change_before_start_makes_no_requests(tmp_path):
    plan, transport, calls = setup()
    plan["source_manifest"] = {}
    with pytest.raises(ValueError, match="SOURCE_PINNED"):
        C.capture(tmp_path / "capture", plan, transport, clock=lambda: NOW)
    assert not calls
