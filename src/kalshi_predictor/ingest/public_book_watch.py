"""Finite public-data replacement for an unavailable production stream."""

import argparse
import json
import time
from datetime import UTC, datetime
from pathlib import Path

from kalshi_predictor.ingest.public_book_stage import stage_public_books


def manifest_tickers(path, *, as_of):
    raw = path.read_bytes()
    if len(raw) > 1_000_000:
        raise ValueError("MANIFEST_SIZE")
    value = json.loads(raw)
    generated = datetime.fromisoformat(value["generated_at"])
    if not 0 <= (as_of - generated).total_seconds() <= 1200:
        raise ValueError("MANIFEST_NOT_FRESH")
    tickers = value["tickers"]
    if not isinstance(tickers, list) or any(not isinstance(t, str) for t in tickers):
        raise ValueError("MANIFEST_TICKERS")
    return list(dict.fromkeys(tickers))[:6]


def run(*, manifest, staging_dir, output, cycles=12, interval=60):
    if not 1 <= cycles <= 12 or interval < 60:
        raise ValueError("BOUNDED_WATCH_REQUIRED")
    output.mkdir(parents=True, exist_ok=False)
    (output / "reservation.json").write_text(
        json.dumps(
            dict(
                started_at=datetime.now(UTC).isoformat(),
                max_cycles=cycles,
                interval_seconds=interval,
                max_gets=cycles * 12,
                manifest=str(manifest),
                execution_enabled=False,
            )
        )
    )
    for index in range(cycles):
        start = time.monotonic()
        try:
            tickers = manifest_tickers(manifest, as_of=datetime.now(UTC))
            result = (
                stage_public_books(
                    tickers=tickers,
                    staging_dir=staging_dir,
                    evidence_dir=output / f"cycle-{index:02}",
                )
                if tickers
                else {"status": "NO_TICKERS", "requests": 0}
            )
        except (OSError, ValueError, KeyError, TypeError) as exc:
            result = {"status": "REFUSED", "error": type(exc).__name__, "requests": None}
        print(json.dumps({"cycle": index, "result": result}), flush=True)
        (output / f"cycle-{index:02}.terminal.json").write_text(json.dumps(result))
        if index + 1 < cycles:
            time.sleep(max(0, interval - (time.monotonic() - start)))
    (output / "terminal.json").write_text(
        json.dumps(
            {"status": "TERMINAL", "cycles": cycles, "finished_at": datetime.now(UTC).isoformat()}
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--staging-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cycles", type=int, default=12)
    args = parser.parse_args()
    run(
        manifest=args.manifest, staging_dir=args.staging_dir, output=args.output, cycles=args.cycles
    )
