"""Read-only research capture view. No provider credentials, transport, or ledger."""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import UTC, date, datetime
from html import escape
from itertools import islice
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

_PROVIDERS = ("unusual-whales", "synoptic", "oddpool", "fred", "bls")
_NAMES = {
    "unusual-whales": "Unusual Whales",
    "synoptic": "Synoptic",
    "oddpool": "Oddpool",
    "fred": "FRED",
    "bls": "BLS",
}
_FILES = re.compile(r"(\d{8}T\d{12}Z)-(unusual-whales|synoptic|oddpool|fred|bls)\.summary\.json")
_ENDPOINTS = {
    "unusual-whales": ("api.unusualwhales.com", "/api/predictions/unusual"),
    "synoptic": ("api.synopticdata.com", "/v2/stations/latest"),
    "oddpool": ("api.oddpool.com", "/search/markets"),
    "fred": ("api.stlouisfed.org", "/fred/series/observations"),
    "bls": ("api.bls.gov", "/publicAPI/v2/timeseries/data/"),
}
_SELECTIONS = {
    "unusual-whales": "Provider-ranked prediction markets; venue mapping unverified",
    "synoptic": "KNYC air temperature observations",
    "oddpool": "Kalshi KXTEMPNYCH market research",
    "fred": "Economic observations and vintage dates; publication time unverified",
    "bls": "CUUR0000SA0 CPI observations; publication time and historical availability unverified",
}


def _at(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError
    return result.astimezone(UTC)


def _unlinked(path: Path) -> None:
    if any(
        p.is_symlink() or getattr(p, "is_junction", lambda: False)() for p in (path, *path.parents)
    ):
        raise ValueError


def _read(path: Path, root: Path, limit: int) -> bytes:
    _unlinked(path)
    if path.resolve(strict=True).parent != root or not path.is_file():
        raise ValueError
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError
    return raw


def _endpoint(provider: str, value: Any) -> str:
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError
    url = urlsplit(value)
    if (
        url.scheme != "https"
        or (url.hostname, url.path) != _ENDPOINTS[provider]
        or url.username
        or url.password
        or url.port
        or url.fragment
    ):
        raise ValueError
    pairs = parse_qsl(url.query, keep_blank_values=True)
    params = dict(pairs)
    if len(pairs) != len(params):
        raise ValueError
    if provider == "bls":
        if params:
            raise ValueError
    elif provider == "fred":
        fields = {
            "series_id",
            "file_type",
            "limit",
            "sort_order",
            "observation_start",
            "observation_end",
            "realtime_start",
            "realtime_end",
        }
        if (
            set(params) != fields
            or params["series_id"] not in {"DFF", "CPIAUCSL", "UNRATE"}
            or params["file_type"] != "json"
            or params["sort_order"] != "asc"
            or not params["limit"].isdigit()
            or not 1 <= int(params["limit"]) <= 1000
        ):
            raise ValueError
        for key in ("observation_start", "observation_end", "realtime_start", "realtime_end"):
            if date.fromisoformat(params[key]).isoformat() != params[key]:
                raise ValueError
        if (
            params["observation_start"] > params["observation_end"]
            or params["realtime_start"] > params["realtime_end"]
        ):
            raise ValueError
    elif provider == "synoptic":
        fixed = dict(
            stid="KNYC",
            vars="air_temp",
            output="json",
            sensorvars="1",
            obtimezone="UTC",
            units="temp|C",
            qc="on",
            qc_flags="on",
            qc_remove_data="off",
        )
        if any(params.get(k) != v for k, v in fixed.items()) or set(params) != set(fixed) | {
            "within"
        }:
            raise ValueError
        if not params["within"].isdigit() or not 1 <= int(params["within"]) <= 120:
            raise ValueError
    else:
        fixed = (
            {}
            if provider == "unusual-whales"
            else dict(series_id="KXTEMPNYCH", exchange="kalshi", status="active")
        )
        if any(params.get(k) != v for k, v in fixed.items()) or set(params) - (
            set(fixed) | {"limit", "offset"}
        ):
            raise ValueError
        if (
            not params.get("limit", "").isdigit()
            or not 1 <= int(params["limit"]) <= 20
            or params.get("offset", "0") != "0"
        ):
            raise ValueError
    return f"https://{url.hostname}{url.path}"


def _text(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 1000:
        raise ValueError
    return value


def _samples(provider: str, summary: dict[str, Any], body: Any) -> list[dict[str, Any]]:
    supplied = summary["samples"]
    if (
        not isinstance(supplied, list)
        or len(supplied) > 20
        or type(summary["record_count"]) is not int
        or summary["record_count"] != len(supplied)
    ):
        raise ValueError
    output: list[dict[str, Any]] = []
    fields: tuple[str, ...]
    if provider in {"fred", "bls"}:
        if (
            summary.get("publication_time_verified") is not False
            or summary.get("historical_availability_verified", False) is not False
        ):
            raise ValueError
        if provider == "fred":
            originals = body["observations"]
            series_id = dict(parse_qsl(urlsplit(summary["source_url"]).query))["series_id"]
            fields = ("date", "value", "realtime_start", "realtime_end")
            if originals != supplied:
                raise ValueError
        else:
            results = body["Results"]
            if isinstance(results, list):
                if len(results) != 1:
                    raise ValueError
                results = results[0]
            series = results["series"]
            if (
                body["status"] != "REQUEST_SUCCEEDED"
                or len(series) != 1
                or series[0]["seriesID"] != "CUUR0000SA0"
            ):
                raise ValueError
            series_id = series[0]["seriesID"]
            originals = series[0]["data"]
            fields = ("year", "period", "periodName", "value")
            if originals[: len(supplied)] != supplied or summary.get("total_available") != len(
                originals
            ):
                raise ValueError
        for row in supplied:
            output_row: dict[str, Any] = {
                "series_id": series_id,
                **{key: _text(row.get(key)) for key in fields},
            }
            if provider == "bls":
                if row["year"] != str(_at(summary["started_at"]).year) or not re.fullmatch(
                    r"M(0[1-9]|1[0-3])", row["period"]
                ):
                    raise ValueError
                notes = row["footnotes"]
                if not isinstance(notes, list) or len(notes) > 20:
                    raise ValueError
                output_row["footnotes"] = [
                    {key: _text(note.get(key)) for key in ("code", "text") if key in note}
                    for note in notes
                ]
            output.append(output_row)
    elif provider == "synoptic":
        stations = body["STATION"]
        if (
            len(stations) != 1
            or stations[0]["STID"] != "KNYC"
            or body["UNITS"]["air_temp"] not in {"Celsius", "C"}
        ):
            raise ValueError
        observed = stations[0]["OBSERVATIONS"]
        for row in supplied:
            sensor = row["sensor_id"]
            if not isinstance(sensor, str) or not re.fullmatch(r"air_temp_value_\d+d?", sensor):
                raise ValueError
            original = observed[sensor]
            clock = _at(original["date_time"])
            if (
                clock != _at(row["observed_at"])
                or row["value_c"] != (None if original["value"] is None else str(original["value"]))
                or row["unit"] != body["UNITS"]["air_temp"]
            ):
                raise ValueError
            output.append(
                dict(
                    station="KNYC",
                    sensor_id=sensor,
                    observed_at=clock.isoformat(),
                    value_c=_text(row["value_c"]),
                    unit=row["unit"],
                    sensor_qc="PROVIDER_QC_REPORTED"
                    if original.get("qc") is not None
                    else "NOT_REPORTED",
                )
            )
    else:
        originals = body["data"] if provider == "unusual-whales" else body
        if provider == "unusual-whales" and isinstance(originals, dict):
            originals = originals["data"]
        if not isinstance(originals, list) or len(originals) != len(supplied):
            raise ValueError
        fields = (
            ("asset_id", "market", "category", "resolves")
            if provider == "unusual-whales"
            else ("market_id",)
        )
        for row, original in zip(supplied, originals, strict=True):
            if any(row.get(key) != original.get(key) for key in fields):
                raise ValueError
            output.append({key: _text(row.get(key)) for key in fields})
    return output


def snapshot(capture_directory: Path | None = None) -> dict[str, Any]:
    now = datetime.now(UTC)
    result: dict[str, Any] = dict(
        viewed_at=now.isoformat(),
        research_only=True,
        current_freshness="UNKNOWN",
        providers=[
            dict(
                provider=p, name=_NAMES[p], state="NO_CAPTURE", selection=_SELECTIONS[p], samples=[]
            )
            for p in _PROVIDERS
        ],
    )
    directory = (
        capture_directory
        if capture_directory is not None
        else Path(__file__).resolve().parents[3] / "reports" / "provider_trial"
    )
    try:
        _unlinked(directory)
        if not directory.exists():
            return result
        root = directory.resolve(strict=True)
        with os.scandir(root) as entries:
            names = [entry.name for entry in islice(entries, 129)]
        if len(names) > 128:
            raise ValueError
        newest: dict[str, str] = {}
        for name in names:
            matched = _FILES.fullmatch(name)
            if matched and name > newest.get(matched[2], ""):
                newest[matched[2]] = name
        for item in result["providers"]:
            provider = item["provider"]
            if provider not in newest:
                continue
            try:
                name = newest[provider]
                summary = json.loads(_read(root / name, root, 65_536))
                started, completed = _at(summary["started_at"]), _at(summary["completed_at"])
                if (
                    summary["provider"] != provider
                    or summary["research_only"] is not True
                    or summary["runtime_certified"] is not False
                    or not started <= completed <= now
                    or not name.startswith(started.strftime("%Y%m%dT%H%M%S%fZ"))
                ):
                    raise ValueError
                item.update(attempted_at=started.isoformat(), completed_at=completed.isoformat())
                if summary["state"] == "PROVIDER_ERROR":
                    error = summary["error_code"]
                    prefix = {
                        "synoptic": "SYNOPTIC",
                        "oddpool": "ODDPOOL",
                        "unusual-whales": "UW",
                        "fred": "FRED",
                        "bls": "BLS",
                    }[provider]
                    if not isinstance(error, str) or not re.fullmatch(
                        prefix + r"_[A-Z0-9_]{1,96}", error
                    ):
                        raise ValueError
                    item.update(state="PROVIDER_ERROR", error_code=error)
                    continue
                if summary["state"] != "CAPTURED":
                    raise ValueError
                received = _at(summary["received_at"])
                if not started <= received <= completed:
                    raise ValueError
                endpoint = _endpoint(provider, summary["source_url"])
                original = _read(
                    root / name.replace(".summary.json", ".original.json"), root, 1_048_576
                )
                if hashlib.sha256(original).hexdigest() != summary["source_sha256"]:
                    raise ValueError
                body = json.loads(original)
                samples = _samples(provider, summary, body)
                if provider in {"fred", "bls"}:
                    item.update(
                        publication_time_verified=False, historical_availability_verified=False
                    )
                provider_at = summary.get("provider_timestamp")
                if provider_at is not None:
                    clock_body = body
                    if provider == "unusual-whales" and isinstance(body.get("data"), dict):
                        clock_body = body["data"]
                    if _at(provider_at) > received or _at(clock_body["timestamp"]) != _at(
                        provider_at
                    ):
                        raise ValueError
                    provider_at = _at(provider_at).isoformat()
                if any(_at(s["observed_at"]) > received for s in samples if "observed_at" in s):
                    raise ValueError
                item.update(
                    state="RECORDED_RESEARCH",
                    received_at=received.isoformat(),
                    receipt_age_seconds=int((now - received).total_seconds()),
                    provider_timestamp=provider_at,
                    current_freshness="UNKNOWN",
                    source_endpoint=endpoint,
                    source_sha256=summary["source_sha256"],
                    record_count=len(samples),
                    samples=samples,
                )
            except (
                OSError,
                ValueError,
                TypeError,
                KeyError,
                AttributeError,
                IndexError,
                RecursionError,
            ):
                item.clear()
                item.update(
                    provider=provider,
                    name=_NAMES[provider],
                    state="CAPTURE_INVALID",
                    error_code="CAPTURE_VERIFICATION_FAILED",
                    samples=[],
                )
    except (OSError, ValueError):
        result["providers"] = [
            dict(provider=p, name=_NAMES[p], state="CAPTURE_DIRECTORY_UNAVAILABLE", samples=[])
            for p in _PROVIDERS
        ]
    return result


def render(payload: dict[str, Any]) -> str:
    cards = []
    for row in payload["providers"]:
        labels = {
            "state": "Capture status",
            "selection": "Selection",
            "attempted_at": "Attempt time",
            "received_at": "Receipt time",
            "receipt_age_seconds": "Receipt age (seconds)",
            "provider_timestamp": "Provider timestamp",
            "error_code": "Capture error",
        }
        details = "".join(
            f"<dt>{label}</dt>"
            f"<dd>{escape(str(row[key]) if row[key] is not None else 'Not reported')}</dd>"
            for key, label in labels.items()
            if key in row
        )
        samples = (
            "".join(
                "<dl>"
                + "".join(
                    f"<dt>{escape(key.replace('_', ' ').title())}</dt>"
                    f"<dd>{escape(str(value) if value is not None else 'Not reported')}</dd>"
                    for key, value in sample.items()
                )
                + "</dl>"
                for sample in row["samples"]
            )
            or "<p>No verified samples are available for this capture.</p>"
        )
        cards.append(
            f"<section><h2>{escape(row['name'])}</h2><dl>{details}</dl>"
            f"<h3>Captured samples</h3>{samples}</section>"
        )
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>Provider research</title><link rel='stylesheet' href='/static/styles.css'>"
        "</head><body><main><h1>Provider research captures</h1><p>Research only. "
        "These are saved captures, not live readings. Receipt time and provider "
        "observation time are separate. Current freshness is unknown.</p>"
        "<p>This page does not establish model readiness, settlement authority or "
        "trading eligibility. Unusual Whales venue mapping is unverified.</p>"
        "<p>FRED vintage dates and BLS observation periods do not establish publication "
        "times or prove what was available at an earlier decision time.</p>"
        + "".join(cards)
        + "</main></body></html>"
    )


def create_router(capture_directory: Path | None = None) -> APIRouter:
    router = APIRouter()

    @router.get("/api/research/providers")
    def providers_api() -> dict[str, Any]:
        return snapshot(capture_directory)

    @router.get("/research/providers", response_class=HTMLResponse)
    def providers_page() -> str:
        return render(snapshot(capture_directory))

    return router
