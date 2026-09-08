"""Read-only local-paper dashboard; never constructs a writer or execution client."""

from __future__ import annotations

import hashlib
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
from kalshi_predictor.overnight_paper.source_health import MAX_FORECAST_AGE_SECONDS, aware
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
        "SELECT id,captured_at,payload FROM overnight_sprint_cycles WHERE id LIKE ? ORDER BY id",
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
    if (
        baseline.get("kind") != "LOCAL_PAPER_AUTHORIZATION_BASELINE_V1"
        or Path(baseline["database_path"]).resolve() != path.resolve()
    ):
        raise ValueError("RUNTIME_HEALTH_BASELINE_IDENTITY_INVALID")
    info = path.stat()
    watcher_status = None
    previous_at = None
    blocker_event = None
    for sequence, row in enumerate(rows):
        item = json.loads(row["payload"])
        captured = aware(item["captured_at"])
        if (
            item["kind"] != "PAPER_RUNTIME_HEALTH_V1"
            or item["generation"] != generation
            or item["sequence"] != sequence
            or aware(row["captured_at"]) != captured
            or row["id"] != f"runtime-health:{generation}:{sequence:06d}"
            or Path(item["database_path"]).resolve() != path.resolve()
            or item["database_id"] != baseline["database_id"]
            or item["database_file_identity"] != [info.st_dev, info.st_ino]
            or (previous_at is not None and captured < previous_at)
        ):
            raise ValueError("RUNTIME_HEALTH_IDENTITY_INVALID")
        if item.get("watcher_status") is not None:
            watcher_status = item["watcher_status"]
        blockers = item.get("blockers", [])
        if not isinstance(blockers, list) or any(not isinstance(b, str) for b in blockers):
            raise ValueError("RUNTIME_HEALTH_BLOCKERS_INVALID")
        if item["state"] in {"BLOCKED", "DEGRADED", "ENTRY_DISABLED"} and not blockers:
            reason = item.get("reason")
            if reason is not None:
                if not isinstance(reason, str):
                    raise ValueError("RUNTIME_HEALTH_REASON_INVALID")
                blockers = [reason]
        if "blockers" in item or blockers:
            blocker_event = {
                "at": item["captured_at"],
                "state": item["state"],
                "blockers": blockers,
            }
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
        "runtime_blocker_event": blocker_event,
        "runtime_current_monitor_verified": False,
    }


def _weather_snapshot(db: sqlite3.Connection, path: Path, now: datetime) -> dict[str, Any]:
    """Inspect the newest attempt, never promote an older diagnostic over a refusal."""
    records = []
    for row in db.execute("SELECT id,captured_at,payload FROM overnight_sprint_cycles"):
        record = json.loads(row["payload"])
        if str(row["id"]).startswith(("weather-preparation:", "weather-driver:")) and record.get(
            "kind"
        ) not in {"PAPER_RELEASE_PREPARATION", "PAPER_WEATHER_DRIVER_V1"}:
            raise ValueError("WEATHER_JOURNAL_KIND_INVALID")
        if record.get("kind") in {"PAPER_RELEASE_PREPARATION", "PAPER_WEATHER_DRIVER_V1"}:
            records.append((aware(row["captured_at"]), row["id"], record))
    if not records:
        return {}
    captured, key, record = max(records, key=lambda item: (item[0], item[1]))
    if captured > now:
        raise ValueError("WEATHER_JOURNAL_FUTURE_CLOCK")

    def preparation_record(value: dict[str, Any], identity: str) -> str:
        request = value["request"]
        if (
            value["kind"] != "PAPER_RELEASE_PREPARATION"
            or not identity.startswith("weather-preparation:")
            or value["request_id"] != decision_fingerprint(request)
            or request["source_hashes"] != [s["sha256"] for s in value["original_sources"]]
            or aware(value["started_at"]) > aware(value["finished_at"])
            or aware(value["finished_at"]) > captured
        ):
            raise ValueError("WEATHER_PREPARATION_JOURNAL_INVALID")
        return str(request["ticker"])

    if record["kind"] == "PAPER_RELEASE_PREPARATION":
        ticker = preparation_record(record, key)
        if aware(record["finished_at"]) != captured:
            raise ValueError("WEATHER_PREPARATION_CAPTURE_MISMATCH")
        blockers = record["blockers"]
    else:
        ticker = record["ticker"]
        if key != "weather-driver:" + record["generation"]:
            raise ValueError("WEATHER_DRIVER_GENERATION_MISMATCH")
        baselines = db.execute(
            "SELECT id,payload FROM overnight_sprint_cycles "
            "WHERE id LIKE 'authorization-baseline:%'"
        ).fetchall()
        if len(baselines) != 1:
            raise ValueError("WEATHER_DRIVER_BASELINE_REQUIRED")
        baseline = json.loads(baselines[0]["payload"])
        if (
            baseline.get("kind") != "LOCAL_PAPER_AUTHORIZATION_BASELINE_V1"
            or baselines[0]["id"] != "authorization-baseline:" + record["database_id"]
            or baseline["database_id"] != record["database_id"]
            or Path(baseline["database_path"]).resolve() != path.resolve()
            or Path(record["database_path"]).resolve() != path.resolve()
        ):
            raise ValueError("WEATHER_DRIVER_DATABASE_IDENTITY_MISMATCH")
        linked = record["preparation_checkpoint"]
        if linked is not None:
            prior = db.execute(
                "SELECT payload FROM overnight_sprint_cycles WHERE id=?", (linked,)
            ).fetchone()
            if prior is None or linked != "weather-preparation:" + record["generation"]:
                raise ValueError("WEATHER_DRIVER_PREPARATION_REQUIRED")
            prepared = json.loads(prior[0])
            if (
                preparation_record(prepared, linked) != ticker
                or prepared["original_sources"] != record["original_sources"]
                or prepared["state"] != record["preparation_state"]
            ):
                raise ValueError("WEATHER_DRIVER_PREPARATION_MISMATCH")
        blockers = record["assembly_blockers"]
    if not isinstance(blockers, list) or any(not isinstance(b, str) for b in blockers):
        raise ValueError("WEATHER_JOURNAL_BLOCKERS_INVALID")
    originals = record["original_sources"]
    if not isinstance(originals, list) or len(originals) > 12:
        raise ValueError("WEATHER_JOURNAL_ORIGINALS_INVALID")
    envelopes: dict[str, dict[str, Any]] = {}
    receipts = []
    for source in originals:
        raw = source["original_utf8"].encode("utf-8")
        if len(raw) > 20_000_000 or hashlib.sha256(raw).hexdigest() != source["sha256"]:
            raise ValueError("WEATHER_JOURNAL_ORIGINAL_HASH_MISMATCH")
        envelope = json.loads(raw)
        url = envelope["url"]
        if not isinstance(url, str) or url in envelopes or not isinstance(envelope["body"], dict):
            raise ValueError("WEATHER_JOURNAL_SOURCE_IDENTITY_INVALID")
        receipt = aware(envelope["received_at"])
        if receipt > captured:
            raise ValueError("WEATHER_JOURNAL_RECEIPT_IN_FUTURE")
        envelopes[url] = envelope["body"]
        receipts.append(receipt)
    base = "https://external-api.kalshi.com/trade-api/v2"
    market = envelopes.get(base + "/markets/" + ticker, {}).get("market")
    if originals and (not isinstance(market, dict) or market.get("ticker") != ticker):
        raise ValueError("WEATHER_JOURNAL_MARKET_IDENTITY_MISMATCH")
    hourly = [(url, body) for url, body in envelopes.items() if url.endswith("/forecast/hourly")]
    if len(hourly) > 1:
        raise ValueError("WEATHER_JOURNAL_FORECAST_AMBIGUOUS")
    values: dict[str, Any] = dict(
        weather_evidence_at=captured.isoformat(),
        weather_evidence_kind=record["kind"],
        weather_evidence_ticker=ticker,
        weather_last_attempt_blockers=blockers,
        weather_evidence_state="RECENT_ATTEMPT"
        if (now - captured).total_seconds() <= 60
        else "HISTORICAL",
        last_capture_at=max(receipts).isoformat() if receipts else None,
        capture_state="CURRENT_CAPTURE"
        if receipts and (now - max(receipts)).total_seconds() <= 60
        else "HISTORICAL"
        if receipts
        else "UNVERIFIED",
        weather_source_state="UNVERIFIED",
        weather_provider_updated_at=None,
        weather_provider_generated_at=None,
    )
    if hourly:
        url, body = hourly[0]
        points = [
            body
            for address, body in envelopes.items()
            if address.startswith("https://api.weather.gov/points/")
        ]
        station = envelopes.get("https://api.weather.gov/stations/KNYC", {})
        if (
            not url.startswith("https://api.weather.gov/gridpoints/OKX/")
            or len(points) != 1
            or points[0].get("properties", {}).get("forecastHourly") != url
            or station.get("properties", {}).get("stationIdentifier") != "KNYC"
        ):
            raise ValueError("WEATHER_JOURNAL_FORECAST_IDENTITY_MISMATCH")
        props = body["properties"]
        values.update(
            weather_provider_updated_at=props.get("updateTime"),
            weather_provider_generated_at=props.get("generatedAt"),
        )
        if props.get("updateTime") and props.get("generatedAt"):
            ages = [(now - aware(props[k])).total_seconds() for k in ("updateTime", "generatedAt")]
            values["weather_source_state"] = (
                "SOURCE_ERROR"
                if min(ages) < 0
                else "STALE"
                if max(ages) > MAX_FORECAST_AGE_SECONDS
                else "PROVIDER_CLOCKS_FRESH_REVALIDATION_REQUIRED"
            )
    return values


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
        "weather_provider_generated_at": None,
        "weather_evidence_at": None,
        "weather_evidence_kind": None,
        "weather_evidence_ticker": None,
        "weather_evidence_state": "UNVERIFIED",
        "weather_last_attempt_blockers": [],
        "capture_state": "UNVERIFIED",
        "historical_diagnostics": [],
        "reported_final_examples": None,
        "independent_final_reproductions": None,
        "next_expected_settlement": None,
        "positions": [],
        "shadow_candidates": 0,
        "last_decision_at": None,
        "last_qualification_status": None,
        "last_qualification_recorded_at": None,
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
        "runtime_blocker_event": None,
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
                            last_qualification_recorded_at=record["captured_at"],
                            qualification_gates=gates,
                            first_blocker=next(iter(qualification["blockers"]), None),
                            rule_version=inputs.get("rule_version"),
                            book_captured_at=evidence["shadow_payload"].get("snapshot_at"),
                        )
                        # A recorded result does not re-run source, rule, book,
                        # model or release verification and never enables entry.
                        result["blockers"].append("RECORDED_QUALIFICATION_REQUIRES_REVALIDATION")
                elif evidence.get("kind") in {"WEATHER_DIAGNOSTIC", "CRYPTO_FINAL_DIAGNOSTIC"}:
                    result["historical_diagnostics"].append(
                        {
                            "kind": evidence["kind"],
                            "captured_at": record["captured_at"],
                            "status": "HISTORICAL_DIAGNOSTIC_NOT_RUNTIME_CERTIFICATION",
                            "original_diagnostic": evidence,
                            "reported_final_examples": len(evidence.get("examples", [])),
                            "weather_provider_updated_at": evidence.get("result", {}).get(
                                "forecast_updated_at"
                            ),
                        }
                    )
                elif evidence.get("mode") == "OBSERVATION_ONLY" and "result" in evidence:
                    if result["last_capture_at"] is None:
                        result["last_capture_at"] = record["captured_at"]
                        result["capture_state"] = "HISTORICAL_DIAGNOSTIC"
            weather = _weather_snapshot(db, path, datetime.now(UTC))
            if weather:
                result.update(weather)
                qualified_at = result["last_qualification_recorded_at"]
                if qualified_at is None or aware(weather["weather_evidence_at"]) >= aware(
                    qualified_at
                ):
                    result["first_blocker"] = next(
                        iter(weather["weather_last_attempt_blockers"]), None
                    )
                result["blockers"].extend(weather["weather_last_attempt_blockers"])
                if weather["capture_state"] != "CURRENT_CAPTURE":
                    result["blockers"].append("CAPTURE_IS_HISTORICAL_REFRESH_REQUIRED")
            runtime_blocker = result["runtime_blocker_event"]
            if runtime_blocker:
                other_times = [
                    result["last_qualification_recorded_at"],
                    result["weather_evidence_at"],
                ]
                if all(t is None or aware(runtime_blocker["at"]) >= aware(t) for t in other_times):
                    result["first_blocker"] = next(iter(runtime_blocker["blockers"]), None)
                result["blockers"].extend(runtime_blocker["blockers"])
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
        result["first_blocker"] = "PAPER_DASHBOARD_EVIDENCE_INVALID"
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
            "weather_provider_generated_at",
            "weather_evidence_at",
            "weather_evidence_state",
            "capture_state",
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
        "<h2>Historical diagnostics</h2><p>These archived records do not establish current "
        "source freshness or the presence or absence of externally archived evidence.</p>"
        f"<pre>{escape(json.dumps(payload['historical_diagnostics'], indent=2))}</pre>"
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
