"""Read-only journal to exclusive official-result evaluation artifacts; no requests."""

import argparse
import json
from pathlib import Path

from kalshi_predictor.crypto.research_shadow_evaluation import write_evaluation
from kalshi_predictor.crypto.settlement_target import _json


def read(path: Path) -> bytes:
    if not 0 < path.stat().st_size <= 48_000_000:
        raise ValueError("BOUNDED_ORIGINAL_REQUIRED")
    return path.read_bytes()


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--journal", type=Path, required=True)
    p.add_argument("--capture", type=Path, required=True)
    p.add_argument("--completion-sha256", required=True)
    p.add_argument(
        "--official-manifest",
        type=Path,
        required=True,
        help="JSON ticker -> {original_path, receipt_path}; exact existing files only",
    )
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    completion = read(args.capture / "completion.json")
    files = {"completion.json": completion}
    manifest = _json(completion)["files"]
    if len(manifest) > 100:
        raise ValueError("BOUNDED_CAPTURE_REQUIRED")
    for name in manifest:
        path = (args.capture / name).resolve()
        if not path.is_relative_to(args.capture.resolve()):
            raise ValueError("CAPTURE_PATH_ESCAPE")
        files[name] = read(path)
    if (args.capture / "failure.json").exists():
        files["failure.json"] = read(args.capture / "failure.json")
    official = {
        ticker: (read(Path(item["original_path"])), read(Path(item["receipt_path"])))
        for ticker, item in _json(read(args.official_manifest)).items()
    }
    result = write_evaluation(
        args.output,
        args.journal,
        files,
        completion_sha256=args.completion_sha256,
        official=official,
    )
    print(
        json.dumps(
            dict(
                status="COMPLETE",
                event_count=result["event_count"],
                rows=len(result["rows"]),
                output=str(args.output),
            ),
            allow_nan=False,
        )
    )


if __name__ == "__main__":
    main()
