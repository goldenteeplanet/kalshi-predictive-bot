"""Bounded public Miami index outcome capture; no accounts, trades or database."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.request import HTTPRedirectHandler, Request, build_opener

from kalshi_predictor.weather import miami_evaluation, miami_index

BASE = "https://external-api.kalshi.com/trade-api/v2/live_data/weather/miami"


def now() -> datetime:
    return datetime.now(UTC)


def at(value: str | datetime) -> datetime:
    result = datetime.fromisoformat(value) if isinstance(value, str) else value
    if not isinstance(result, datetime) or result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("AWARE_CLOCK_REQUIRED")
    return result.astimezone(UTC)


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def load(raw: bytes) -> dict:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("DUPLICATE_JSON_KEY")
            result[key] = value
        return result

    def reject(value):
        raise ValueError("NONFINITE_JSON")

    result = json.loads(raw, object_pairs_hook=unique, parse_constant=reject)
    if not isinstance(result, dict):
        raise ValueError("OBJECT_REQUIRED")
    return result


def read(path: Path, limit: int) -> bytes:
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    if not raw or len(raw) > limit:
        raise ValueError("ORIGINAL_SIZE_CAP")
    return raw


def write_new(path: Path, raw: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(raw)


def json_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, indent=2, allow_nan=False).encode()


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def acquire(url: str) -> tuple[bytes, int]:
    opener = build_opener(NoRedirect())
    request = Request(url, headers={"User-Agent": "Dejoia-public-readonly-outcomes/1"})
    with opener.open(request, timeout=15) as response:
        return response.read(2_000_001), response.status


def run(
    frozen_dir: Path,
    output: Path,
    *,
    clock: Callable[[], datetime] = now,
    get: Callable[[str], tuple[bytes, int]] = acquire,
) -> dict:
    """One immutable attempt per 15-minute bucket; at most two public GETs.

    A failed/incomplete attempt remains evidence and is not retried in its bucket.
    Only the earliest due missing target can acquire a final receipt per invocation.
    """
    started = at(clock())
    if not (frozen_dir / "decision.json").is_file():
        return {"status": "NO_FROZEN_DECISION_NO_REQUESTS", "requests": 0}
    names = ("prediction.json", "recording-receipt.json", "decision.json")
    originals = {
        name: read(frozen_dir / name, 8_000_000 if name == names[0] else 32_000) for name in names
    }
    prediction, recording, decision = (load(originals[name]) for name in names)
    prediction_sha = digest(originals[names[0]])
    if (
        prediction.get("schema") != "frozen-research-prediction-v1"
        or recording.get("schema") != "prediction-recording-receipt-v1"
        or decision.get("schema") != "recorded-research-decision-v1"
        or recording.get("prediction_sha256") != prediction_sha
        or decision.get("prediction_sha256") != prediction_sha
        or decision.get("prediction_receipt_sha256") != digest(originals[names[1]])
        or decision.get("prediction_recorded_at") != recording.get("prediction_recorded_at")
    ):
        raise ValueError("FROZEN_HASH_OR_SCHEMA_MISMATCH")
    received, asof, recorded, decided, earliest = (
        at(prediction["input_received_at"]),
        at(prediction["model_input_as_of"]),
        at(recording["prediction_recorded_at"]),
        at(decision["decision_at"]),
        at(prediction["target_at"]),
    )
    committed = at(prediction["model_committed_at"])
    if not received <= asof <= recorded <= decided < earliest or decided > started:
        raise ValueError("INVALID_FROZEN_CHRONOLOGY")
    for key in ("input_received_at", "model_input_as_of"):
        if at(prediction[key]) != at(decision[key]):
            raise ValueError("DECISION_CLOCK_MISMATCH")
    if "model_committed_at" in decision and at(decision["model_committed_at"]) != committed:
        raise ValueError("DECISION_CLOCK_MISMATCH")
    proof = prediction["prediction"]["code_proof"]
    if (
        at(proof["commit_recorded_at"]) != committed
        or not committed <= at(proof["code_frozen_at"]) <= asof
        or len(proof.get("source_commit", "")) != 40
        or any(c not in "0123456789abcdef" for c in proof["source_commit"])
        or not proof.get("files")
        or any(
            not item.get("path")
            or len(item.get("sha256", "")) != 64
            or any(c not in "0123456789abcdef" for c in item["sha256"])
            for item in proof["files"]
        )
    ):
        raise ValueError("INVALID_CODE_PROOF")
    forecasts = prediction["prediction"]["forecasts"]
    if len(forecasts) != 2 or {f["horizon_minutes"] for f in forecasts} != {30, 60}:
        raise ValueError("EXPECTED_FIXED_HORIZONS")
    targets = [at(f["target_at"]) for f in forecasts]
    if min(targets) != earliest or len({at(f["origin_at"]) for f in forecasts}) != 1:
        raise ValueError("TARGET_ORIGIN_MISMATCH")
    for forecast, target in zip(forecasts, targets, strict=True):
        if (
            forecast["units"] != "fahrenheit"
            or target.second
            or target.microsecond
            or target != at(forecast["origin_at"]) + timedelta(minutes=forecast["horizon_minutes"])
            or at(forecast["model_input_as_of"]) != asof
            or at(forecast["input_received_at"]) != received
        ):
            raise ValueError("FORECAST_IDENTITY_MISMATCH")
    reference = output / "frozen-reference.json"
    binding = {name: digest(raw) for name, raw in originals.items()}
    if reference.exists() and load(read(reference, 4000)) != binding:
        raise ValueError("COHORT_CHANGED_SINCE_FIRST_OUTCOME")
    due = []
    for target in targets:
        final = output / (target.strftime("%Y%m%dT%H%MZ") + ".final.json")
        if final.exists():
            completed = load(read(final, 2_000_000))
            if completed.get("prediction_sha256") != prediction_sha:
                raise ValueError("FINAL_COHORT_MISMATCH")
            row = completed.get("row", {})
            metrics = row.get("metrics")
            actual = row.get("actual_f")
            attempt_name = completed.get("attempt", "")
            if (
                at(row.get("target_at")) != target
                or row.get("status") != "SCORED"
                or type(actual) not in (int, float)
                or not math.isfinite(actual)
                or not isinstance(metrics, dict)
                or set(metrics) != miami_evaluation.MODELS
                or not isinstance(attempt_name, str)
                or not attempt_name.startswith("attempt-")
                or Path(attempt_name).name != attempt_name
                or "\\" in attempt_name
            ):
                raise ValueError("FINAL_TARGET_OR_EVIDENCE_MISMATCH")
            for model in metrics.values():
                if not isinstance(model, dict) or any(
                    type(model.get(key)) not in (int, float)
                    or not math.isfinite(model[key])
                    or model[key] < 0
                    for key in ("absolute_error_f", "squared_error_f2", "crps_f")
                ):
                    raise ValueError("FINAL_METRICS_INVALID")
            prior = output / attempt_name
            prior_report = load(read(prior / "evaluation.json", 2_000_000))
            if (
                prior_report.get("prediction_sha256") != prediction_sha
                or prior_report.get("decision_sha256") != binding["decision.json"]
                or [
                    r
                    for r in prior_report.get("rows", [])
                    if r.get("target_at") == row["target_at"]
                ]
                != [row]
            ):
                raise ValueError("FINAL_ATTEMPT_EVALUATION_MISMATCH")
        elif started >= target + timedelta(minutes=5):
            due.append(target)
    if not due:
        return {"status": "NO_UNSCORED_TARGET_DUE_NO_REQUESTS", "requests": 0}
    target = min(due)
    output.mkdir(parents=True, exist_ok=True)
    if not reference.exists():
        write_new(reference, json_bytes(binding))
    bucket = started.replace(minute=started.minute // 15 * 15, second=0, microsecond=0)
    attempt = output / ("attempt-" + bucket.strftime("%Y%m%dT%H%MZ"))
    try:
        attempt.mkdir()
    except FileExistsError:
        return {"status": "ATTEMPT_ALREADY_EXISTS_NO_RETRY", "requests": 0}
    for name, raw in originals.items():
        write_new(attempt / name, raw)
    sources = {
        "collector.py": Path(__file__),
        "evaluator.py": Path(miami_evaluation.__file__),
        "decoder.py": Path(miami_index.__file__),
    }
    code_hashes = {}
    for name, path in sources.items():
        raw = read(path, 1_000_000)
        write_new(attempt / name, raw)
        code_hashes[name] = digest(raw)
    receipts: list[dict] = []

    def fetch(url: str, name: str) -> tuple[bytes, datetime]:
        if len(receipts) >= 2:
            raise ValueError("TWO_GET_BUDGET")
        raw, status = get(url)
        receipt_at = at(clock())
        if type(raw) is not bytes or len(raw) > 2_000_000 or status != 200:
            raise ValueError("RESPONSE_SIZE_OR_STATUS")
        if receipt_at < started or (receipts and receipt_at < at(receipts[-1]["received_at"])):
            raise ValueError("RECEIPT_CLOCK_REGRESSION")
        write_new(attempt / name, raw)
        receipt = {
            "url": url,
            "status": status,
            "sha256": digest(raw),
            "received_at": receipt_at.isoformat(),
            "path": name,
        }
        receipts.append(receipt)
        write_new(attempt / (name + ".receipt.json"), json_bytes(receipt))
        return raw, receipt_at

    start = int(target.timestamp() * 1000)
    raw, index_at = fetch(BASE + f"?from={start}&to={start + 59999}", "index.json")
    cal, calibration_at = fetch(BASE + "/calibrations", "calibrations.json")
    capture = miami_index.decode_miami_index(
        raw,
        cal,
        index_received_at=index_at,
        calibrations_received_at=calibration_at,
        index_units="fahrenheit",
    )
    report = miami_evaluation.evaluate_miami_forecast(
        *(originals[name] for name in names),
        outcome=capture,
        evaluated_at=at(clock()),
    )
    report.update(requests=len(receipts), receipts=receipts, evaluator_source_hashes=code_hashes)
    write_new(attempt / "evaluation.json", json_bytes(report))
    for row in report["rows"]:
        if at(row["target_at"]) == target and row["status"] == "SCORED":
            final = output / (target.strftime("%Y%m%dT%H%MZ") + ".final.json")
            write_new(
                final,
                json_bytes(
                    {"prediction_sha256": prediction_sha, "attempt": attempt.name, "row": row}
                ),
            )
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--frozen-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(run(args.frozen_dir, args.output), indent=2, allow_nan=False))
