"""Official outcome collection and scoring of frozen multi-asset forecasts only."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from kalshi_predictor.crypto.multiasset_capture import (
    BASE,
    at,
    digest,
    encode,
    persist,
    source_manifest,
)
from kalshi_predictor.crypto.research_shadow_evaluation import _official, scores


def verify_capture(root: Path, pin_raw: bytes, receipt_raw: bytes, expected_plan_sha: str):
    if any(path.is_symlink() for path in (root, *root.parents)):
        raise ValueError("LINKED_CAPTURE_REFUSED")
    pin, receipt = json.loads(pin_raw), json.loads(receipt_raw)
    plan_raw = (root / "protocol.json").read_bytes()
    plan = json.loads(plan_raw)
    target = at(plan["target_at"])
    complete_raw = (root / "completion.json").read_bytes()
    complete = json.loads(complete_raw)
    if (
        plan["source_manifest"] != source_manifest()
        or receipt["pin_sha256"] != digest(pin_raw)
        or pin["completion_sha256"] != digest(complete_raw)
        or pin["protocol_sha256"] != digest(plan_raw)
        or digest(plan_raw) != expected_plan_sha
        or pin["event"] != plan["event"]
        or at(pin["target_at"]) != target
        or not at(complete["at"]) <= at(pin["at"]) <= at(receipt["at"]) < target
        or (root / "failure.json").exists()
        or len(complete["files"]) > 100
    ):
        raise ValueError("INDEPENDENT_PRETARGET_PIN_REQUIRED")
    for name, expected in complete["files"].items():
        path = root / name
        if (
            path.is_symlink()
            or not path.resolve().is_relative_to(root.resolve())
            or not 0 < path.stat().st_size <= 16000000
            or digest(path.read_bytes()) != expected
        ):
            raise ValueError("CAPTURE_ORIGINAL_HASH_MISMATCH")
    with closing(sqlite3.connect((root / "research.db").as_uri() + "?mode=ro", uri=True)) as db:
        rows = db.execute("SELECT id,payload,sha256,stored_at FROM decisions").fetchall()
    if len(rows) != 4 or {r[0] for r in rows} != set(complete["decisions"]):
        raise ValueError("DURABLE_DECISIONS_REQUIRED")
    decisions = []
    for ident, raw, sha, stored in rows:
        value = json.loads(raw)
        if (
            digest(raw) != sha
            or raw != (root / ("decision-" + ident + ".json")).read_bytes()
            or value["decision_id"] != ident
            or value["event"] != plan["event"]
            or not at(value["decision_at"]) <= at(stored) <= at(complete["at"]) < target
            or value["protocol_sha256"] != expected_plan_sha
        ):
            raise ValueError("DECISION_STORAGE_OR_CLOCK_MISMATCH")
        decisions.append(value)
    return plan, decisions, digest(complete_raw)


def collect(
    output: Path,
    capture_root: Path,
    pin_raw: bytes,
    pin_receipt_raw: bytes,
    expected_plan_sha: str,
    transport,
    *,
    clock=lambda: datetime.now(UTC),
):
    output.mkdir(exist_ok=False)
    files = {}
    requests = 0

    def save(name, raw):
        persist(output / name, raw)
        files[name] = digest(raw)

    try:
        save("reservation.json", encode({"at": clock().isoformat(), "max_gets": 2, "retries": 0}))
        plan, decisions, completion_sha = verify_capture(
            capture_root, pin_raw, pin_receipt_raw, expected_plan_sha
        )
        target = at(plan["target_at"])
        if not target + timedelta(minutes=10) <= clock() < target + timedelta(minutes=11):
            raise ValueError("EXACT_OUTCOME_WINDOW_REQUIRED")
        save("capture-pin.json", pin_raw)
        save("capture-pin-receipt.json", pin_receipt_raw)
        catalog = json.loads((capture_root / "catalog.original.json").read_bytes())
        originals = {r["ticker"]: r for r in catalog["markets"]}
        official = {}
        for index, ticker in enumerate(sorted({d["ticker"] for d in decisions})):
            requested = clock()
            if requested >= target + timedelta(minutes=11) or requests >= 2:
                raise ValueError("OUTCOME_DEADLINE_OR_BUDGET")
            url = BASE + "/markets/" + ticker
            save(f"{index}.reservation.json", encode({"url": url, "at": requested.isoformat()}))
            requests += 1
            status, raw = transport(
                url, min(12, (target + timedelta(minutes=11) - requested).total_seconds())
            )
            received = clock()
            if type(raw) is not bytes or not 0 < len(raw) <= 3000000:
                raise ValueError("BOUNDED_OFFICIAL_ORIGINAL_REQUIRED")
            save(f"{index}.original.json", raw)
            receipt = encode(
                {
                    "method": "GET",
                    "url": url,
                    "http_status": status,
                    "original_complete": True,
                    "source_sha256": digest(raw),
                    "requested_at": requested.isoformat(),
                    "received_at": received.isoformat(),
                }
            )
            save(f"{index}.receipt.json", receipt)
            captured = min(at(d["decision_at"]) for d in decisions)
            official[ticker] = _official(
                (raw, receipt),
                ticker,
                target,
                encode({"market": originals[ticker]}),
                captured,
                clock(),
            )
        results = []
        for d in decisions:
            outcome, evidence = official[d["ticker"]]
            for model, value in d["models"]["models"].items():
                probability = value["probability"]
                if probability is None:
                    continue
                results.append(
                    {
                        "decision_id": d["decision_id"],
                        "event": d["event"],
                        "ticker": d["ticker"],
                        "asset": d["symbol"],
                        "rule_version": d["rule_version"],
                        "model": model,
                        "score": scores(Decimal(str(probability)), outcome),
                        "official": evidence,
                        "hypothetical_after_cost_pnl": None,
                        "full_net_ev": None,
                        "cost_status": "UNKNOWN_ORIGINAL_COSTS",
                        "independent_n": None,
                    }
                )
        _, _, after_sha = verify_capture(capture_root, pin_raw, pin_receipt_raw, expected_plan_sha)
        if after_sha != completion_sha or clock() >= target + timedelta(minutes=11):
            raise ValueError("CAPTURE_CHANGED_OR_EVALUATION_DEADLINE")
        save(
            "evaluation.json",
            encode(
                {
                    "at": clock().isoformat(),
                    "rows": results,
                    "capture_completion_sha256": completion_sha,
                    "paper_pnl": None,
                    "execution_authority": False,
                }
            ),
        )
        save(
            "completion.json",
            encode(
                {
                    "at": clock().isoformat(),
                    "files": dict(files),
                    "status": "OFFICIAL_RESEARCH_EVALUATED",
                    "requests": requests,
                }
            ),
        )
        if clock() >= target + timedelta(minutes=11):
            raise ValueError("POST_PUBLICATION_DEADLINE")
        return {"rows": len(results), "requests": requests}
    except Exception as exc:
        save(
            "failure.json",
            encode(
                {
                    "at": clock().isoformat(),
                    "error_class": type(exc).__name__,
                    "requests": requests,
                    "status": "TERMINAL_NO_RETRY",
                }
            ),
        )
        raise
