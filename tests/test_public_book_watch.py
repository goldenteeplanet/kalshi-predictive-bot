import json
from datetime import UTC, datetime, timedelta

import pytest

from kalshi_predictor.ingest.public_book_watch import manifest_tickers, run


def test_stale_or_future_manifest_never_selects(tmp_path):
    path = tmp_path / "manifest.json"
    now = datetime.now(UTC)
    for offset in (-1201, 1):
        path.write_text(
            json.dumps(
                {"generated_at": (now + timedelta(seconds=offset)).isoformat(), "tickers": ["BTC"]}
            )
        )
        with pytest.raises(ValueError):
            manifest_tickers(path, as_of=now)


def test_selection_bounded_and_deduplicated(tmp_path):
    path = tmp_path / "manifest.json"
    now = datetime.now(UTC)
    path.write_text(
        json.dumps({"generated_at": now.isoformat(), "tickers": ["A"] * 5 + list("BCDEFGHI")})
    )
    assert manifest_tickers(path, as_of=now) == list("ABCDEF")


def test_run_refuses_unbounded_and_reused_output(tmp_path):
    with pytest.raises(ValueError):
        run(manifest=tmp_path / "m", staging_dir=tmp_path / "s", output=tmp_path / "out", cycles=13)
    output = tmp_path / "existing"
    output.mkdir()
    with pytest.raises(FileExistsError):
        run(manifest=tmp_path / "m", staging_dir=tmp_path / "s", output=output, cycles=1)
