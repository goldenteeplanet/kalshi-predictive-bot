"""Run a fixed six-public-GET isolated microstructure research capture."""

import argparse
from pathlib import Path

from kalshi_predictor.config import get_settings
from kalshi_predictor.microstructure.research_capture import run

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    print(
        run(Path(__file__).resolve().parents[1], args.output, args.ticker, settings=get_settings())
    )
