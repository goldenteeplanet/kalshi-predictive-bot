"""Bounded display projection of externally pinned research journals; no admission."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import time
from contextlib import closing
from datetime import UTC, datetime, timedelta
from html import escape
from pathlib import Path

SMALL = 256_000
PAYLOAD = 16_000_000


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def decode(raw: bytes) -> dict:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("DUPLICATE_KEY")
            result[key] = value
        return result

    def bad(value):
        raise ValueError("NONFINITE")

    result = json.loads(raw, object_pairs_hook=pairs, parse_constant=bad)
    # Serialization rejects exponent overflow, including unused nested fields.
    json.dumps(result, allow_nan=False)
    if not isinstance(result, dict):
        raise ValueError("OBJECT_REQUIRED")
    return result


def clock(value: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError("CLOCK_STRING_REQUIRED")
    value_at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value_at.tzinfo is None:
        raise ValueError("AWARE_CLOCK_REQUIRED")
    return value_at


def safe(path: Path) -> Path:
    if any(
        p.is_symlink() or getattr(p, "is_junction", lambda: False)() for p in (path, *path.parents)
    ):
        raise ValueError("LINK_REFUSED")
    return path


def read(path: Path, limit: int = SMALL) -> bytes:
    with safe(path).open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("ARTIFACT_BOUND")
    return raw


def slot_view(base: Path, control: Path, index: int, registered: dict, now: datetime) -> dict:
    result = {"slot": index, "status": "UNVERIFIED", "rows": []}
    capture = base / f"slot-{index}"
    try:
        plan_raw = read(control / f"slot-{index}.protocol.json")
        if registered["protocol_sha256"] != digest(plan_raw):
            raise ValueError("REGISTERED_PROTOCOL_HASH")
        plan = decode(plan_raw)
        start, end, target = (clock(plan[k]) for k in ("not_before", "not_after", "target_at"))
        result.update(event=plan["event_ticker"], target=plan["target_at"])
        routed = plan["schema"] == "cf-average-prospective-slot-v2"
        if not start < end < target or plan["schema"] not in {
            "cf-average-prospective-slot-v1",
            "cf-average-prospective-slot-v2",
        }:
            raise ValueError("PROTOCOL")
        if routed and plan.get("research_route") != "crypto_v3/settlement_average":
            raise ValueError("EXACT_RESEARCH_ROUTE")
        if not routed and "research_route" in plan:
            raise ValueError("LEGACY_ROUTE_CLAIM")
        if safe(capture / "failure.json").exists():
            result["status"] = "FAILED_CAPTURE"
            return result
        if not (control / f"slot-{index}.completion-pin.json").exists():
            result["status"] = "SCHEDULED" if now < start else "PENDING_UNVERIFIED"
            return result
        external = decode(read(control / f"slot-{index}.completion-pin.json"))
        completion_raw = read(capture / "completion.json")
        completion = decode(completion_raw)
        observed, completed = (
            clock(external["observed_at"]),
            clock(completion["recorded_after_result"]),
        )
        if (
            external["schema"] != "cf-cohort-external-completion-pin-v1"
            or type(external["slot"]) is not int
            or external["slot"] != index
            or external["protocol_sha256"] != digest(plan_raw)
            or external["completion_sha256"] != digest(completion_raw)
            or external["target_at"] != plan["target_at"]
            or external["execution_authority"] is not False
            or completion["status"] != "COMPLETE"
            or not start <= completed <= observed < end < target - timedelta(minutes=1)
            or observed > now
        ):
            raise ValueError("EXTERNAL_PIN_OR_CLOCK")
        files = completion["files"]
        if not isinstance(files, dict) or len(files) > 100:
            raise ValueError("MANIFEST_BOUND")
        originals = {}
        for name in (
            "plan.original.json",
            "shadow-pins.json",
            "shadow-pins.recorded.json",
            "selection.json",
        ):
            raw = read(capture / name)
            if digest(raw) != files[name]:
                raise ValueError("DISPLAY_ORIGINAL_HASH")
            originals[name] = raw
        if originals["plan.original.json"] != plan_raw:
            raise ValueError("PROTOCOL_ORIGINAL")
        pins = decode(originals["shadow-pins.json"])
        receipt = decode(originals["shadow-pins.recorded.json"])
        selected = decode(originals["selection.json"])["selected"]
        hypotheses = {"LEFT_CLOSED_RIGHT_OPEN", "LEFT_OPEN_RIGHT_CLOSED"}
        if (
            pins["schema"] != ("cf-shadow-pins-v2" if routed else "cf-shadow-pins-v1")
            or pins["event"] != plan["event_ticker"]
            or pins["target"] != plan["target_at"]
            or len(pins["decisions"]) != 4
            or len(selected) != 2
            or len(set(selected)) != 2
            or {(p["ticker"], p["hypothesis"]) for p in pins["decisions"]}
            != {(t, h) for t in selected for h in hypotheses}
            or len({p["decision_id"] for p in pins["decisions"]}) != 4
            or receipt["sha256"] != digest(originals["shadow-pins.json"])
            or not start <= clock(receipt["recorded_at"]) <= completed
        ):
            raise ValueError("COHORT_IDENTITY")
        route_sources = {}
        proof = {}
        if routed:
            from kalshi_predictor.crypto import research_shadow as S

            proof_raw = read(capture / "source.originals.json", 2_000_000)
            if digest(proof_raw) != files["source.originals.json"]:
                raise ValueError("ROUTE_SOURCE_MANIFEST")
            proof = decode(proof_raw)
            if not S.same(plan["source_sha256"], {k: v["sha256"] for k, v in proof.items()}):
                raise ValueError("REGISTERED_ROUTE_SOURCE_CLOSURE")
            for source in proof.values():
                S.original(source)
            route_sources = {
                k.removeprefix("route."): v for k, v in proof.items() if k.startswith("route.")
            }
        elif any("route_sha256" in p or "route_completion_sha256" in p for p in pins["decisions"]):
            raise ValueError("LEGACY_ROUTE_CLAIM")
        journal = safe(capture / "research.db")
        with closing(
            sqlite3.connect(journal.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)
        ) as db:
            deadline = time.monotonic() + 1.0
            db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
            db.execute("PRAGMA query_only=ON")
            if db.execute("PRAGMA application_id").fetchone()[0] != 0x43525348:
                raise ValueError("JOURNAL_IDENTITY")
            if len(db.execute("SELECT id FROM research_shadow LIMIT 5").fetchall()) != 4:
                raise ValueError("FOUR_RECORD_BOUND")
            rows = []
            for pin in pins["decisions"]:
                # Check lengths in SQL before materializing potentially huge originals.
                lengths = db.execute(
                    "SELECT length(payload) FROM research_shadow WHERE id=?", (pin["decision_id"],)
                ).fetchone()
                if not lengths or not 0 < lengths[0] <= PAYLOAD:
                    raise ValueError("PAYLOAD_BOUND")
                item = db.execute(
                    "SELECT payload,payload_sha FROM research_shadow WHERE id=?",
                    (pin["decision_id"],),
                ).fetchone()
                c = db.execute(
                    "SELECT payload,payload_sha FROM research_completion WHERE id=? AND "
                    "length(payload)<=?",
                    (pin["decision_id"], SMALL),
                ).fetchone()
                if (
                    not item
                    or not c
                    or digest(item[0]) != item[1]
                    or item[1] != pin["payload_sha256"]
                    or digest(c[0]) != c[1]
                    or c[1] != pin["completion_sha256"]
                ):
                    raise ValueError("JOURNAL_HASH")
                stored, committed = decode(item[0]), decode(c[0])
                decision = stored["decision"]
                if (
                    decision["decision_id"] != pin["decision_id"]
                    or decision["event"] != pins["event"]
                    or decision["ticker"] != pin["ticker"]
                    or decision["rule_version"] != pin["rule_version"]
                    or decision["request_sha256"] != pin["request_sha256"]
                    or committed["decision_id"] != pin["decision_id"]
                    or committed["payload_sha256"] != item[1]
                    or committed["status"] != "COMPLETE_RESEARCH"
                    or not start
                    <= clock(decision["decision_at"])
                    <= clock(decision["computed_at"])
                    <= clock(committed["original_committed_before"])
                    <= completed
                    or decision["paper_eligible"] is not False
                    or decision["execution_authority"] is not False
                ):
                    raise ValueError("DECISION_BINDING")
                route_fields = {}
                if routed:
                    from kalshi_predictor.forecasting.crypto_average_shadow_route import (
                        validate_route_receipt,
                    )

                    identity = pin["decision_id"]
                    if len(identity) != 64 or any(c not in "0123456789abcdef" for c in identity):
                        raise ValueError("ROUTE_IDENTITY")
                    writer_sources = stored["source_originals"]
                    if not writer_sources or any(
                        not S.same(source, proof.get(name))
                        for name, source in writer_sources.items()
                    ):
                        raise ValueError("WRITER_SOURCE_NOT_PLANNED")
                    route_raw = read(capture / f"route-{identity}.json")
                    route_completion = read(capture / f"route_completion-{identity}.json")
                    if (
                        digest(route_raw) != pin["route_sha256"]
                        or digest(route_raw) != files[f"route-{identity}.json"]
                        or digest(route_completion) != pin["route_completion_sha256"]
                        or digest(route_completion) != files[f"route_completion-{identity}.json"]
                    ):
                        raise ValueError("ROUTE_PIN_BINDING")
                    linked = validate_route_receipt(
                        route_raw,
                        route_completion,
                        decision=dict(decision, journal_completion=committed),
                        payload_sha=item[1],
                        completion_sha=c[1],
                        source_originals=route_sources,
                        as_of=completed,
                    )
                    if clock(linked["route_receipt"]["invoked_at"]) < start:
                        raise ValueError("ROUTE_PRECEDES_PLAN")
                    route_fields = dict(
                        research_route=linked["route"], route_sha256=pin["route_sha256"]
                    )
                p = decision["forecast"]["probability"]
                if type(p) not in (int, float) or not math.isfinite(p) or not 0 <= p <= 1:
                    raise ValueError("PROBABILITY")
                rows.append(
                    dict(
                        ticker=pin["ticker"],
                        hypothesis=pin["hypothesis"],
                        probability=p,
                        payload_sha256=pin["payload_sha256"],
                        completion_sha256=pin["completion_sha256"],
                        rule_version=pin["rule_version"],
                        quotes=decision.get("rows"),
                        decision_id=pin["decision_id"],
                        recorded_at=committed["original_committed_before"],
                        **route_fields,
                    )
                )
        if (capture / "failure.json").exists():
            raise ValueError("FAILED_CAPTURE")
        result.update(
            status="COMPLETE_PIN_BOUND_DISPLAY",
            rows=rows,
            capture_completion_sha256=external["completion_sha256"],
        )
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        IndexError,
        AttributeError,
        sqlite3.Error,
    ) as exc:
        result.update(status="UNVERIFIED", reason=type(exc).__name__, rows=[])
    return result


def read_cohort(base: Path, control: Path, *, now: datetime | None = None) -> dict:
    current = now or datetime.now(UTC)
    try:
        registration = decode(read(control / "registration.json"))
        slots = registration["slots"]
        if (
            registration["status"] != "REGISTERED_BEFORE_CAPTURE"
            or len(slots) != 5
            or any(type(s["slot"]) is not int or s["slot"] != i for i, s in enumerate(slots))
        ):
            raise ValueError("FIVE_REGISTERED_SLOTS_REQUIRED")
        results = [slot_view(base, control, i, slots[i], current) for i in range(5)]
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        results = [dict(slot=i, status="UNVERIFIED", rows=[]) for i in range(5)]
    from kalshi_predictor.ui.research_outcomes import outcome_view

    for slot in results:
        slot["outcome"] = outcome_view(base, control, slot, current)
    completed = [s for s in results if s["status"] == "COMPLETE_PIN_BOUND_DISPLAY"]
    return dict(
        slots=results,
        decisions=sum(len(s["rows"]) for s in completed),
        events=len({s["event"] for s in completed}),
        paper_eligible=0,
    )


def render_cohort(report: dict) -> str:
    def text(value):
        return escape(str(value))

    body = ""
    for slot in report["slots"]:
        body += f"<h3>Slot {text(slot['slot'])}: {text(slot['status'])}</h3>"
        body += (
            f"<p>{text(slot.get('event', 'Unknown'))} - target "
            f"{text(slot.get('target', 'Unknown'))}</p>"
        )
        outcome = slot.get("outcome", {})
        body += "<h4>Official research outcomes &mdash; no paper positions</h4>"
        body += f"<p>{text(outcome.get('status', 'PENDING_OFFICIAL_RESEARCH'))}</p>"
        for label in outcome.get("labels", []):
            body += f"<p>{text(label['ticker'])}: {text(label['outcome'])}</p>"
        for scenario in outcome.get("scenarios", []):
            body += f"<p>{text(scenario['hypothesis'])}: two contracts, one temporal event. "
            for model, metrics in scenario["metrics"].items():
                body += (
                    f"{text(model)} Brier {text(metrics['brier'])}, "
                    f"log loss {text(metrics['log_loss'])}; "
                )
            body += "descriptive only; no calibration or promotion.</p>"
        for row in slot["rows"]:
            if "research_route" in row:
                body += f"<p>Verified research route: {text(row['research_route'])}</p>"
            body += (
                f"<p>{text(row['ticker'])} Â· {text(row['hypothesis'])} Â· P(YES) "
                f"{text(row['probability'])} Â· durable {text(row['recorded_at'])}</p>"
            )
    return (
        "<section><h2>Prospective CF research journals</h2>"
        f"<p>{report['decisions']} durable decisions; {report['events']} temporal events; "
        "paper eligible 0.</p>"
        "<p>Display projection checks external pins and journal hashes, not full original "
        "semantic validation. "
        "Costs unknown; rules uncertified; uncalibrated research. Historical quotes are "
        "not currently executable. "
        "Four scenarios per event are not four independent observations. Existing report "
        "metrics are separate.</p>" + body + "</section>"
    )
