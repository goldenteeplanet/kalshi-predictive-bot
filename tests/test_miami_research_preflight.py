"""A stopped/out-of-window research capture must not touch the network or files."""

import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest


@pytest.mark.parametrize("offset", [-601, 1])
def test_outside_origin_window_is_read_free(tmp_path, monkeypatch, offset):
    path = Path(__file__).parents[1] / "scripts/positive_ev_miami_research.py"
    spec = importlib.util.spec_from_file_location("miami_runner_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    clock = datetime(2026, 9, 10, 21, 0, tzinfo=UTC)
    monkeypatch.setattr(module, "now", lambda: clock)

    def forbidden(*args, **kwargs):
        pytest.fail("Out-of-window capture attempted a network request")

    monkeypatch.setattr(module.urllib.request, "urlopen", forbidden)
    output = tmp_path / "capture"
    missing = tmp_path / "does-not-exist"
    with pytest.raises(ValueError, match="OUTSIDE_PROSPECTIVE_ORIGIN_WINDOW"):
        module.run(output, missing, missing, missing, clock + timedelta(seconds=offset))
    assert not output.exists()
