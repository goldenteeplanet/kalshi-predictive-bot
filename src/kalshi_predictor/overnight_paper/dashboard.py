"""Read-only local-paper dashboard; never constructs a writer or execution client."""

from __future__ import annotations

import html
import json
import os
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from kalshi_predictor.overnight_paper.qualification import GATE_NAMES, decision_fingerprint
from kalshi_predictor.overnight_paper.runtime_liveness import inspect_process
from kalshi_predictor.overnight_paper.source_health import aware, classify_source
from kalshi_predictor.overnight_paper.watcher import verified_paper_marker


def _runtime_snapshot(db: sqlite3.Connection, path: Path) -> dict[str, Any]:
    latest = db.execute(
        "SELECT id,payload FROM overnight_sprint_cycles "
        "WHERE id LIKE 'runtime-health:%' ORDER BY captured_at DESC,id DESC LIMIT 1"
    ).fetchone()
    if latest is None:
        return {}
    event = json.loads(latest["payload"])
    generation = event["generation"]
    rows = db.execute(
        "SELECT id,payload FROM overnight_sprint_cycles WHERE id LIKE ? ORDER BY id",
        ("runtime-health:" + generation + ":%",),
    ).fetchall()
    if not 1 <= len(rows) <= 500:
        raise ValueError("RUNTIME_HEALTH_GENERATION_INVALID")
    baseline_rows = db.execute(
        "SELECT payload FROM overnight_sprint_cycles WHERE id LIKE 'authorization-baseline:%'"
    ).fetchall()
    if len(baseline_rows) != 1:
        raise ValueError("RUNTIME_HEALTH_BASELINE_REQUIRED")
    baseline = json.loads(baseline_rows[0][0])
    if (baseline.get("kind") != "LOCAL_PAPER_AUTHORIZATION_BASELINE_V1"
            or Path(baseline["database_path"]).resolve() != path.resolve()):
        raise ValueError("RUNTIME_HEALTH_BASELINE_IDENTITY_INVALID")
    info = path.stat()
    watcher_status = None
    previous_at = None
    for sequence, row in enumerate(rows):
        item = json.loads(row["payload"])
        captured = aware(item["captured_at"])
        if (item["kind"] != "PAPER_RUNTIME_HEALTH_V1"
                or item["generation"] != generation or item["sequence"] != sequence
                or row["id"] != f"runtime-health:{generation}:{sequence:06d}"
                or Path(item["database_path"]).resolve() != path.resolve()
                or item["database_id"] != baseline["database_id"]
                or item["database_file_identity"] != [info.st_dev, info.st_ino]
                or (previous_at is not None and captured < previous_at)):
            raise ValueError("RUNTIME_HEALTH_IDENTITY_INVALID")
        if item.get("watcher_status") is not None:
            watcher_status = item["watcher_status"]
        previous_at = captured
    event = json.loads(rows[-1]["payload"])
    if type(event["pid"]) is not int or type(event["entries_enabled"]) is not bool:
        raise ValueError("RUNTIME_HEALTH_FIELDS_INVALID")
    process = inspect_process(event["pid"], event.get("process_start_identity"))
    age = (datetime.now(UTC) - aware(event["captured_at"])).total_seconds()
    # Process presence and recent records do not prove the supervisor still holds
    # its owner lock or is progressing. Keep those observations separate.
    state = "UNVERIFIED"
    if event["state"] == "STOPPED" or process.state == "STOPPED":
        state = "STOPPED"
    elif not 0 <= age <= 90:
        state = "DEGRADED"
    return {
        "runtime_state": state,
        "runtime_process_state": process.state,
        "runtime_last_reported_state": event["state"],
        "runtime_last_cycle_at": event["captured_at"],
        "runtime_generation": generation,
        "runtime_entries_reported": event["entries_enabled"],
        "runtime_watcher_last_status": watcher_status,
        "runtime_current_monitor_verified": False,
    }


def snapshot(path: Path | None) -> dict:
    result: dict[str, Any] = {
        "label": "LOCAL PAPER — NO REAL MONEY",
        "paper_mode": "NOT_ACTIVE",
        "live_exchange": "DISABLED",
        "demo_exchange": "DISABLED",
        "open_positions": 0,
        "settled": 0,
        "evaluated": 0,
        "realized_pnl": "0",
        "historical_evaluated_events": 0,
        "shadow_settled_events": 0,
        "local_paper_settled_events": 0,
        "fast_candidates_available": None,
        "last_capture_at": None,
        "weather_source_state": "UNVERIFIED",
        "weather_provider_updated_at": None,
        "reported_final_examples": 0,
        "independent_final_reproductions": 0,
        "next_expected_settlement": None,
        "positions": [],
        "shadow_candidates": 0,
        "last_decision_at": None,
        "last_qualification_status": None,
        "qualification_gates": [],
        "qualification_current": False,
        "first_blocker": None,
        "rule_version": None,
        "book_captured_at": None,
        "blockers": [],
        "read_only": True,
        "runtime_state": "UNVERIFIED",
        "runtime_process_state": "UNVERIFIED",
        "runtime_last_reported_state": None,
        "runtime_last_cycle_at": None,
        "runtime_generation": None,
        "runtime_entries_reported": None,
        "runtime_watcher_last_status": None,
        "runtime_current_monitor_verified": False,
    }
    if path is None or not path.is_file():
        result["blockers"] = ["ISOLATED_PAPER_DATABASE_NOT_CONFIGURED"]
        return result
    try:
        with closing(sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA query_only=ON")
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {"overnight_shadow", "overnight_history", "paper_orders"}.issubset(tables):
                raise ValueError("SPRINT_SCHEMA_MISSING")
            result.update(_runtime_snapshot(db, path))
            result["historical_evaluated_events"] = db.execute(
                "SELECT count(DISTINCT event_ticker) FROM overnight_history"
            ).fetchone()[0]
            result["shadow_candidates"] = db.execute(
                "SELECT count(*) FROM overnight_shadow WHERE paper_order_id IS NULL"
            ).fetchone()[0]
            result["shadow_settled_events"] = db.execute(
                "SELECT count(DISTINCT event_ticker) FROM overnight_shadow "
                "WHERE evaluation_json IS NOT NULL AND paper_order_id IS NULL"
            ).fetchone()[0]
            realized = Decimal(0)
            settlements = []
            for row in db.execute(
                "SELECT s.id,s.event_ticker,s.payload,s.evaluation_json,o.id AS order_id,"
                "o.ticker,o.side,o.quantity,o.status,o.limit_price,o.model_name "
                "FROM overnight_shadow s "
                "JOIN paper_orders o ON o.id=s.paper_order_id ORDER BY o.id"
            ):
                item = dict(row)
                payload = json.loads(item.pop("payload"))
                pnl = db.execute(
                    "SELECT id,ticker,realized_pnl,settlement_result FROM paper_pnl WHERE ticker=? "
                    "ORDER BY id DESC LIMIT 1",
                    (item["ticker"],),
                ).fetchone()
                state = "OPEN"
                if pnl and pnl["settlement_result"] in {"yes", "no"}:
                    markers = []
                    for key in (
                        "settlement-final:" + item["ticker"],
                        "paper-evaluation:" + str(item["order_id"]),
                    ):
                        marker = db.execute(
                            "SELECT payload FROM overnight_sprint_cycles WHERE id=?", (key,)
                        ).fetchone()
                        if marker is None:
                            raise ValueError("VERIFIED_SETTLEMENT_LINEAGE_REQUIRED")
                        markers.append(json.loads(marker[0]))
                    verified_paper_marker(
                        markers[1],
                        final_marker=markers[0],
                        paper_order={**item, "id": item["order_id"]},
                        paper_pnl=dict(pnl),
                        shadow_payload=payload,
                        shadow_order_id=item["order_id"],
                        shadow_evaluation=json.loads(item["evaluation_json"]),
                        now=datetime.now(UTC),
                    )
                    state = "PAPER_EVALUATED"
                    result["settled"] += 1
                    result["evaluated"] += int(state == "PAPER_EVALUATED")
                    realized += Decimal(pnl["realized_pnl"])
                else:
                    result["open_positions"] += 1
                    if payload.get("expected_settlement_at"):
                        settlements.append(payload["expected_settlement_at"])
                fills = db.execute(
                    "SELECT price,quantity,fee FROM paper_fills WHERE paper_order_id=? ORDER BY id",
                    (item["order_id"],),
                ).fetchall()
                fill_quantity = sum(fill["quantity"] for fill in fills)
                fill_price = (
                    sum(Decimal(fill["price"]) * fill["quantity"] for fill in fills) / fill_quantity
                    if fill_quantity
                    else None
                )
                item.update(
                    state=state,
                    lifecycle=payload,
                    category=payload.get("category", "UNVERIFIED"),
                    forecast=payload.get("forecast"),
                    net_ev=payload.get("net_ev"),
                    executed_simulated_price=str(fill_price)
                    if fill_price is not None
                    else "UNFILLED",
                    settlement_eta=payload.get("expected_settlement_at"),
                    simulated_fees=str(sum((Decimal(fill["fee"]) for fill in fills), Decimal(0))),
                )
                result["positions"].append(item)
            result["local_paper_settled_events"] = result["settled"]
            result["realized_pnl"] = str(realized)
            result["next_expected_settlement"] = min(settlements) if settlements else None
            if result["positions"]:
                # Orders alone do not prove the watcher is active after a restart.
                result["paper_mode"] = "POSITIONS_PRESENT_WATCHER_UNVERIFIED"
            result["blockers"] = ["NO_ACTIVATION_CERTIFICATE_OR_CURRENT_WATCHER_EVIDENCE"]
            for record in db.execute(
                "SELECT captured_at,payload FROM overnight_sprint_cycles ORDER BY captured_at DESC"
            ):
                evidence = json.loads(record["payload"])
                if evidence.get("kind") == "PAPER_RELEASE_QUALIFICATION":
                    if result["last_qualification_status"] is None:
                        inputs = evidence["decision_inputs"]
                        qualification = evidence["qualification"]
                        gates = qualification["gates"]
                        if (
                            qualification["decision_id"] != decision_fingerprint(inputs)
                            or [item[0] for item in gates] != list(GATE_NAMES)
                            or any(type(item[1]) is not bool for item in gates)
                        ):
                            raise ValueError("QUALIFICATION_JOURNAL_INVALID")
                        result.update(
                            last_decision_at=inputs.get("decision_at"),
                            last_qualification_status=qualification["status"],
                            qualification_gates=gates,
                            first_blocker=next(iter(qualification["blockers"]), None),
                            rule_version=inputs.get("rule_version"),
                            book_captured_at=evidence["shadow_payload"].get("snapshot_at"),
                        )
                        # A recorded result does not re-run source, rule, book,
                        # model or release verification and never enables entry.
                        result["blockers"].append("RECORDED_QUALIFICATION_REQUIRES_REVALIDATION")
                elif evidence.get("kind") == "WEATHER_DIAGNOSTIC":
                    if result["weather_provider_updated_at"] is None:
                        weather = evidence["result"]
                        result["weather_provider_updated_at"] = weather.get("forecast_updated_at")
                        states = {
                            classify_source(
                                generated_at=weather.get("forecast_generated_at"),
                                updated_at=weather.get("forecast_updated_at"),
                                valid_from=f["period"]["startTime"],
                                valid_to=f["period"]["endTime"],
                                target_start=f["period"]["startTime"],
                                target_end=f["period"]["endTime"],
                                now=datetime.now(UTC),
                                payload_hash=evidence["sha256"],
                                previous_hash=evidence["sha256"],
                                reused=True,
                            ).state
                            for f in weather.get("forecasts", [])
                        }
                        result["weather_source_state"] = ", ".join(sorted(states)) or "UNVERIFIED"
                        result["blockers"].append("WEATHER_METHODOLOGY_AND_CUTOVER_UNCERTIFIED")
                elif evidence.get("kind") == "CRYPTO_FINAL_DIAGNOSTIC":
                    result["reported_final_examples"] = len(evidence["examples"])
                    result["blockers"].append("INDEPENDENT_CF_SOURCE_REPRODUCTION_MISSING")
                elif evidence.get("mode") == "OBSERVATION_ONLY" and "result" in evidence:
                    if result["last_capture_at"] is None:
                        result["last_capture_at"] = record["captured_at"]
                        captured = datetime.fromisoformat(record["captured_at"])
                        age = (datetime.now(UTC) - captured).total_seconds()
                        candidates = evidence["result"].get("eligible_candidates", [])
                        # A stored capture is historical evidence once quotes have aged.
                        result["fast_candidates_available"] = (
                            len(candidates) if 0 <= age <= 30 else None
                        )
                        if not candidates:
                            result["blockers"].append("NO_CERTIFIED_POSITIVE_NET_EV_CANDIDATE")
                        if age > 30:
                            result["blockers"].append("CAPTURE_IS_HISTORICAL_REFRESH_REQUIRED")
    except (sqlite3.DatabaseError, ValueError, KeyError, TypeError, OSError, AttributeError):
        result = snapshot(None)
        result["paper_mode"] = "UNVERIFIED"
        for key in (
            "open_positions",
            "settled",
            "evaluated",
            "realized_pnl",
            "historical_evaluated_events",
            "shadow_settled_events",
            "local_paper_settled_events",
            "reported_final_examples",
            "independent_final_reproductions",
            "shadow_candidates",
        ):
            result[key] = None
        result["blockers"] = ["PAPER_DASHBOARD_EVIDENCE_INVALID"]
    return result


def render(payload: dict) -> str:
    def escape(value) -> str:
        return html.escape(str(value))

    metrics = "".join(
        f"<article><h2>{escape(key.replace('_', ' ').title())}</h2>"
        f"<p>{escape(payload[key])}</p></article>"
        for key in (
            "paper_mode",
            "runtime_state",
            "runtime_process_state",
            "runtime_last_reported_state",
            "runtime_last_cycle_at",
            "runtime_watcher_last_status",
            "live_exchange",
            "demo_exchange",
            "open_positions",
            "settled",
            "evaluated",
            "realized_pnl",
            "next_expected_settlement",
            "fast_candidates_available",
            "last_capture_at",
            "weather_source_state",
            "weather_provider_updated_at",
            "reported_final_examples",
            "independent_final_reproductions",
            "historical_evaluated_events",
            "shadow_settled_events",
            "local_paper_settled_events",
            "shadow_candidates",
            "last_decision_at",
            "last_qualification_status",
            "first_blocker",
            "rule_version",
            "book_captured_at",
        )
    )
    cards = "".join(
        f"<article><h2>{escape(row['ticker'])}</h2><p>{escape(row['state'])}</p>"
        f"<p>{escape(row['side'])} · quantity {escape(row['quantity'])} · "
        f"category {escape(row['category'])}</p>"
        f"<p>Forecast {escape(row['forecast'])} · net EV {escape(row['net_ev'])} · "
        f"executed simulated price {escape(row['executed_simulated_price'])}</p>"
        f"<p>Settlement ETA {escape(row['settlement_eta'])} · "
        f"simulated fees {escape(row['simulated_fees'])}</p>"
        f"<details><summary>Full Lifecycle</summary><pre>"
        f"{escape(json.dumps(row['lifecycle'], indent=2))}</pre></details></article>"
        for row in payload["positions"]
    )
    empty = (
        "Position evidence unavailable."
        if payload["paper_mode"] == "UNVERIFIED"
        else "No local paper positions created."
    )
    gates = "".join(
        f"<li>{escape(name)}: {escape('passed at decision time' if passed else 'blocked')}</li>"
        for name, passed in payload["qualification_gates"]
    )
    return (
        "<!doctype html><html lang='en'><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<title>Live market data + local paper</title><style>"
        "body{font:16px system-ui;background:#101827;color:#edf2f8;margin:2rem;max-width:1200px}"
        "h1{color:#7dd3fc}section{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));"
        "gap:1rem}article{padding:1rem;background:#1c293d;"
        "border:1px solid #49617b;border-radius:8px}"
        "h2{font-size:1rem}pre{white-space:pre-wrap;overflow-wrap:anywhere}a{color:#7dd3fc}"
        "</style><main><h1>LOCAL PAPER — NO REAL MONEY</h1>"
        "<p>LIVE MARKET DATA | LOCAL PAPER ONLY | NO REAL MONEY</p>"
        "<p>Process liveness and last reported health are shown separately. "
        "A saved health event does not establish current monitoring or entry eligibility.</p>"
        "<p><a href='/system/progress'>System progress</a></p>"
        f"<section>{metrics}</section><h2>Readiness blockers</h2>"
        f"<p>{escape(', '.join(payload['blockers']))}</p>"
        "<h2>Last recorded qualification</h2>"
        "<p>Historical decision evidence; current eligibility requires revalidation.</p>"
        f"<ul>{gates}</ul>"
        f"<h2>Paper positions</h2>{cards or '<p>' + empty + '</p>'}"
        "</main></html>"
    )


def create_router() -> APIRouter:
    router = APIRouter()

    def current() -> dict:
        raw = os.environ.get("OVERNIGHT_PAPER_DB")
        return snapshot(Path(raw) if raw else None)

    @router.get("/paper-live", response_class=HTMLResponse)
    def paper_live():
        return render(current())

    @router.get("/api/paper-live")
    def paper_live_api():
        return current()

    return router
