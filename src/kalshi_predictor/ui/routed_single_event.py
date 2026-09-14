"""One separately registered routed research event, never a paper position."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from kalshi_predictor.ui import research_journals as R

TARGET = datetime(2026, 9, 12, 1, tzinfo=UTC)
START = TARGET - timedelta(minutes=10)
END = START + timedelta(seconds=50)
EVENT = "KXSOLE-26SEP1121"


def registration(control: Path, *, now: datetime) -> tuple[dict, dict, dict]:
    raw = R.read(control / "registration.json")
    value = R.decode(raw)
    receipt = R.decode(R.read(control / "registration.receipt.json"))
    plan_raw = R.read(control / "slot-0.protocol.json")
    plan = R.decode(plan_raw)
    method_raw = R.read(control / "outcome-source-pins.json")
    method = R.decode(method_raw)
    expected = [{"slot": 0, "protocol_sha256": R.digest(plan_raw)}]
    if (
        value["schema"] != "cf-routed-single-event-registration-v1"
        or value["status"] != "REGISTERED_BEFORE_CAPTURE"
        or value["event_ticker"] != EVENT
        or R.clock(value["target_at"]) != TARGET
        or value["execution_authority"] is not False
        or value["slots"] != expected
        or type(value["slots"][0]["slot"]) is not int
        or receipt["registration_sha256"] != R.digest(raw)
        or not R.clock(value["registered_at"])
        <= R.clock(receipt["recorded_after_registration"])
        < START
        or R.clock(receipt["recorded_after_registration"]) > now
        or value["outcome_source_pins_sha256"] != R.digest(method_raw)
        or not R.clock(method["frozen_at"]) <= R.clock(value["registered_at"])
        or method["execution_authority"] is not False
        or type(method["max_gets"]) is not int
        or method["max_gets"] != 2
        or type(method["retries"]) is not int
        or method["retries"] != 0
        or plan["schema"] != "cf-average-prospective-slot-v2"
        or plan["research_route"] != "crypto_v3/settlement_average"
        or plan["event_ticker"] != EVENT
        or R.clock(plan["target_at"]) != TARGET
        or R.clock(plan["not_before"]) != START
        or R.clock(plan["not_after"]) != END
        or plan["symbol"] != "SOL"
        or plan["benchmark"] != "SOLUSD_RTI"
        or type(plan["max_gets"]) is not int
        or plan["max_gets"] != 6
        or plan["selection"] != "nearest_two_range_midpoints_then_ticker"
        or plan["rounding"] != "HALF_EVEN"
        or type(plan["decimal_places"]) is not int
        or plan["decimal_places"] != 4
        or plan["rule_authority"] != "DECLARED_UNCERTIFIED"
        or plan["net_costs"] != "UNKNOWN"
        or json.dumps(plan["hypotheses"], sort_keys=True)
        != json.dumps(
            [
                dict(name="LEFT_CLOSED_RIGHT_OPEN", include_start=True, include_end=False),
                dict(name="LEFT_OPEN_RIGHT_CLOSED", include_start=False, include_end=True),
            ],
            sort_keys=True,
        )
    ):
        raise ValueError("EXACT_SINGLE_ROUTED_REGISTRATION_REQUIRED")
    for mapping in (plan["source_sha256"], method["source_sha256"]):
        if type(mapping) is not dict or not 1 <= len(mapping) <= 32:
            raise ValueError("BOUNDED_SOURCE_MAP")
        if any(
            type(v) is not str or len(v) != 64 or any(c not in "0123456789abcdef" for c in v)
            for v in mapping.values()
        ):
            raise ValueError("EXACT_SOURCE_DIGEST")
    return value, plan, method


def read_single_event(base: Path, control: Path, *, now: datetime | None = None) -> dict:
    current = now or datetime.now(UTC)
    try:
        registered, _, _ = registration(control, now=current)
        slot = R.slot_view(base, control, 0, registered["slots"][0], current)
    except (OSError, ValueError, KeyError, TypeError, IndexError, AttributeError):
        slot = dict(slot=0, status="UNVERIFIED", rows=[])
    from kalshi_predictor.ui.research_outcomes import outcome_view

    slot["outcome"] = outcome_view(base, control, slot, current)
    complete = slot["status"] == "COMPLETE_PIN_BOUND_DISPLAY"
    return dict(
        slots=[slot],
        decisions=len(slot["rows"]) if complete else 0,
        events=1 if complete else 0,
        paper_eligible=0,
    )


def render_single_event(report: dict) -> str:
    content = R.render_cohort(report)
    return content.replace(
        "Prospective CF research journals",
        "Separate routed research event &mdash; no paper positions",
    ).replace("id='prospective-research'", "id='single-routed-research'")
