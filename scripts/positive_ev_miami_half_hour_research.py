"""Bounded, committed-source Miami forecast capture; no accounts or database."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from kalshi_predictor.crypto.research_provenance import (
    freeze_code,
    freeze_prediction,
    verify_unchanged,
)
from kalshi_predictor.weather.miami_forecast import forecast_miami_prior_day
from kalshi_predictor.weather.miami_half_hour_forecast import forecast_miami_prior_day_grid30
from kalshi_predictor.weather.miami_index import decode_miami_index


def now():
    return datetime.now(UTC)


def run(
    output: Path, manifest: Path, calibration: Path, calibration_receipt: Path, origin: datetime,
    *, model_version: str = "grid30-v1",
):
    if model_version not in ("hourly-v1", "grid30-v1"):
        raise ValueError("UNSUPPORTED_MODEL_VERSION_NO_REQUESTS")
    allowed_minutes = (0,) if model_version == "hourly-v1" else (0, 30)
    if origin.minute not in allowed_minutes or origin.second or origin.microsecond:
        raise ValueError("UNSUPPORTED_ORIGIN_GRID_NO_REQUESTS")
    # There is no useful forecast acquisition outside the tested origin-age window.
    if origin.tzinfo is None or not origin <= now() <= origin + timedelta(minutes=10):
        raise ValueError("OUTSIDE_PROSPECTIVE_ORIGIN_WINDOW_NO_REQUESTS")
    repo = Path(__file__).resolve().parents[1]
    paths: tuple[str, ...] = (
        "scripts/positive_ev_miami_half_hour_research.py",
        "src/kalshi_predictor/weather/miami_forecast.py",
        "src/kalshi_predictor/weather/miami_index.py",
        "src/kalshi_predictor/crypto/research_provenance.py",
    )
    if model_version == "grid30-v1":
        paths += ("src/kalshi_predictor/weather/miami_half_hour_forecast.py",)
    for name in (
        "weather.miami_forecast", "weather.miami_index", "crypto.research_provenance",
        "weather.miami_half_hour_forecast",
    ):
        actual = Path(sys.modules["kalshi_predictor." + name].__file__ or "").resolve()
        if actual != (repo / "src/kalshi_predictor" / Path(*name.split("."))).with_suffix(".py"):
            raise ValueError("IMPORTED_SOURCE_MISMATCH")
    output.mkdir(parents=True, exist_ok=False)
    proof = freeze_code(repo, output / "code", paths)
    (output / "code_provenance.json").write_text(json.dumps(proof, indent=2))
    manifest_raw = manifest.read_bytes()
    (output / "history_manifest.json").write_bytes(manifest_raw)
    old_cal = calibration.read_bytes()
    old_receipts = json.loads(calibration_receipt.read_text())
    old_receipt = next(r for r in old_receipts if r["url"].endswith("/calibrations"))
    if hashlib.sha256(old_cal).hexdigest() != old_receipt["sha256"]:
        raise ValueError("CALIBRATION_HASH_MISMATCH")
    (output / "historical-calibrations.json").write_bytes(old_cal)
    (output / "historical-calibration-receipt.json").write_text(json.dumps(old_receipt, indent=2))
    captures = []
    for i, item in enumerate(json.loads(manifest_raw)["captures"]):
        raw = Path(item["path"]).read_bytes()
        receipt = json.loads(Path(item["receipt_path"]).read_text())
        if hashlib.sha256(raw).hexdigest() != item["sha256"] or receipt["sha256"] != item["sha256"]:
            raise ValueError("HISTORY_HASH_MISMATCH")
        (output / f"history-{i}.json").write_bytes(raw)
        (output / f"history-{i}-receipt.json").write_text(json.dumps(receipt, indent=2))
        captures.append(
            decode_miami_index(
                raw,
                old_cal,
                index_received_at=datetime.fromisoformat(receipt["received_at"]),
                calibrations_received_at=datetime.fromisoformat(old_receipt["received_at"]),
                index_units="fahrenheit",
            )
        )
    receipts: list[dict[str, Any]] = []

    def get(suffix, name):
        if len(receipts) >= 2:
            raise ValueError("REQUEST_BUDGET")
        url = "https://external-api.kalshi.com/trade-api/v2/live_data/weather/miami" + suffix
        with urllib.request.urlopen(
            urllib.request.Request(
                url, headers={"User-Agent": "Dejoia-public-readonly-research/1"}
            ),
            timeout=15,
        ) as response:
            raw = response.read(2_000_001)
            status = response.status
        if len(raw) > 2_000_000:
            raise ValueError("RESPONSE_SIZE_CAP")
        received = now()
        (output / name).write_bytes(raw)
        receipt = dict(
            url=url,
            status=status,
            sha256=hashlib.sha256(raw).hexdigest(),
            received_at=received.isoformat(),
            path=name,
        )
        receipts.append(receipt)
        (output / "receipts.json").write_text(json.dumps(receipts, indent=2))
        return raw, received

    raw, received = get("?last_sec=7200", "current-index.json")
    cal, cal_received = get("/calibrations", "current-calibrations.json")
    captures.append(
        decode_miami_index(
            raw,
            cal,
            index_received_at=received,
            calibrations_received_at=cal_received,
            index_units="fahrenheit",
        )
    )
    asof = now()
    model = (
        forecast_miami_prior_day
        if model_version == "hourly-v1" else forecast_miami_prior_day_grid30
    )
    forecasts = [
        model(
            captures, origin_at=origin, model_input_as_of=asof, horizon_minutes=h
        )
        for h in (30, 60)
    ]
    verify_unchanged(repo, proof)
    result = freeze_prediction(
        output / "frozen",
        {
            "forecasts": forecasts,
            "model_version": model_version,
            "code_proof": proof,
            "history_manifest_sha256": hashlib.sha256(manifest_raw).hexdigest(),
            "status": "PROSPECTIVE_INDEX_FORECAST_UNQUALIFIED",
            "contract_probabilities": None,
            "executable_net_ev": None,
            "exchange_orders": 0,
            "paper_orders": 0,
        },
        model_input_as_of=asof,
        input_received_at=max(c.available_at for c in captures),
        model_committed_at=datetime.fromisoformat(proof["commit_recorded_at"]),
        target_at=origin + timedelta(minutes=30),
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--calibrations", type=Path, required=True)
    parser.add_argument("--calibration-receipts", type=Path, required=True)
    parser.add_argument("--origin", required=True)
    parser.add_argument("--model-version", choices=("hourly-v1", "grid30-v1"), default="grid30-v1")
    args = parser.parse_args()
    run(
        args.output,
        args.manifest,
        args.calibrations,
        args.calibration_receipts,
        datetime.fromisoformat(args.origin),
        model_version=args.model_version,
    )
