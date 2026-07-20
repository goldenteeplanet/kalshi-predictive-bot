import json
from pathlib import Path

from kalshi_predictor.phase_nyc_w6 import write_runtime_integration_preview


def test_nyc_w6_requires_passed_w5_and_keeps_runtime_unchanged(tmp_path: Path) -> None:
    source = tmp_path / "w5.json"
    source.write_text(json.dumps({
        "summary": {"runtime_activation_ready": True, "certified_windows": 3,
                    "settled_windows": 3, "mean_absolute_divergence_f": "0.02",
                    "maximum_absolute_divergence_f": "0.02"},
        "windows": [{"alignment_passed": True}] * 3,
    }))
    path = write_runtime_integration_preview(w5_report=source, output_dir=tmp_path / "out")
    report = json.loads(path.read_text())
    assert report["activation_preview_ready"] is True
    assert report["runtime_weather_v2_changed"] is False
    assert report["database_writes"] == 0
