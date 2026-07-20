import json
from pathlib import Path

import httpx

from kalshi_predictor.phase_nyc_w5 import write_nyc_w5_report


def _write_window(reports: Path, index: int, target: str, observed: str) -> None:
    cert = reports / f"phase_nyc_w3b_{index}"
    cert.mkdir()
    cert.joinpath("nyc_w3_live_alignment_preview.json").write_text(json.dumps({
        "generated_at": target,
        "rows": [{
            "ticker": f"KXTEMPNYCH-26JUL15{index:02d}-T80.99",
            "target_utc_time": target, "metadata_passed": True,
            "alignment_passed": True, "observation_temperature_f": observed,
            "observation_at": target, "offset_seconds": 0,
        }],
    }))
    preview = reports / f"phase_nyc_w4_{index}"
    preview.mkdir()
    preview.joinpath("nyc_w4_observation_feature_integration_preview.json").write_text(
        json.dumps({"rows": [{"target_utc_time": target, "preview_passed": True,
                              "probability_change": "0.01"}]})
    )


def test_nyc_w5_requires_multiple_settled_consistent_windows(tmp_path: Path) -> None:
    reports = tmp_path / "reports"
    reports.mkdir()
    for index, observed in enumerate(("79", "80", "81"), start=1):
        _write_window(reports, index, f"2026-07-16T0{index}:00:00+00:00", observed)

    def handler(request: httpx.Request) -> httpx.Response:
        hour = int(request.url.path.split("JUL15", 1)[1][:2])
        return httpx.Response(200, json={"market": {
            "status": "settled", "result": "yes", "expiration_value": str(78 + hour),
        }})

    with httpx.Client(
        transport=httpx.MockTransport(handler), base_url="https://kalshi.test"
    ) as client:
        path = write_nyc_w5_report(
            reports_dir=reports, output_dir=tmp_path / "out", kalshi_client=client,
        )
    report = json.loads(path.read_text())
    assert report["summary"]["certified_windows"] == 3
    assert report["summary"]["settled_windows"] == 3
    assert report["summary"]["mean_absolute_divergence_f"] == "0"
    assert report["summary"]["runtime_activation_ready"] is True


def test_nyc_w5_holds_after_only_one_unsettled_window(tmp_path: Path) -> None:
    reports = tmp_path / "reports"
    reports.mkdir()
    _write_window(reports, 1, "2026-07-16T01:00:00+00:00", "79")

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"market": {
            "status": "closed", "result": "", "expiration_value": "",
        }})

    with httpx.Client(
        transport=httpx.MockTransport(handler), base_url="https://kalshi.test"
    ) as client:
        path = write_nyc_w5_report(
            reports_dir=reports, output_dir=tmp_path / "out", kalshi_client=client,
        )
    report = json.loads(path.read_text())
    assert report["summary"]["status"] == "COLLECTING_WINDOWS"
    assert report["summary"]["runtime_activation_ready"] is False
    assert report["database_writes"] == 0
