"""The full loaded closure is retained; both resource ceilings remain enforced."""
import sys
from pathlib import Path

import pytest

from kalshi_predictor.microstructure import research_capture as capture


def test_all_loaded_sources_preserved_and_each_limit_enforced(monkeypatch):
    repo = Path(__file__).resolve().parents[1]
    paths = capture.runtime_sources(repo)
    expected = set(capture.SOURCE_PATHS)
    for name, module in list(sys.modules.items()):
        filename = getattr(module, "__file__", None)
        if name.startswith("kalshi_predictor") and filename:
            expected.add(Path(filename).resolve().relative_to(repo).as_posix())
    assert set(paths) == expected
    assert len(paths) == len(set(paths))
    size = sum((repo / path).stat().st_size for path in paths)
    with monkeypatch.context() as patch:
        patch.setattr(capture, "MAX_SOURCE_FILES", len(paths))
        patch.setattr(capture, "MAX_SOURCE_BYTES", size)
        assert capture.runtime_sources(repo) == paths
        patch.setattr(capture, "MAX_SOURCE_FILES", len(paths) - 1)
        with pytest.raises(ValueError, match="SOURCE_CLOSURE_SIZE_CAP"):
            capture.runtime_sources(repo)
    with monkeypatch.context() as patch:
        patch.setattr(capture, "MAX_SOURCE_BYTES", size - 1)
        with pytest.raises(ValueError, match="SOURCE_CLOSURE_SIZE_CAP"):
            capture.runtime_sources(repo)


def test_imported_source_outside_repository_still_rejected(monkeypatch, tmp_path):
    from types import ModuleType

    foreign = ModuleType("kalshi_predictor.foreign_source_probe")
    foreign.__file__ = str(tmp_path / "foreign_source_probe.py")
    monkeypatch.setitem(sys.modules, foreign.__name__, foreign)
    with pytest.raises(ValueError, match="IMPORTED_SOURCE_OUTSIDE_REPOSITORY"):
        capture.runtime_sources(Path(__file__).resolve().parents[1])
