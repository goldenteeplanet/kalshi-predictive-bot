import json
from decimal import Decimal
from pathlib import Path

import httpx

from kalshi_predictor.phase_nyc_w4 import write_nyc_w4_report


def test_nyc_w4_previews_observation_without_runtime_changes(tmp_path: Path) -> None:
    certification = tmp_path / "certification.json"
    certification.write_text(json.dumps({
        "exact_ticker_certification": True,
        "rows": [{
            "ticker": "KXTEMPNYCH-26JUL1523-T80.99",
            "metadata_passed": True, "alignment_passed": True,
            "target_utc_time": "2026-07-16T03:00:00+00:00",
            "observation_at": "2026-07-16T02:51:00+00:00",
            "offset_seconds": 540, "observation_temperature_f": "78.98",
            "evidence_source": "noaa_nws_observation_non_settlement_evidence",
            "settlement_source": "the_weather_company",
        }],
    }))

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/markets/KXTEMPNYCH-26JUL1523-T80.99")
        return httpx.Response(200, json={"market": {
            "yes_bid_dollars": "0.40", "yes_ask_dollars": "0.50",
        }})

    with httpx.Client(
        transport=httpx.MockTransport(handler), base_url="https://kalshi.test"
    ) as client:
        path = write_nyc_w4_report(
            certification_path=certification, output_dir=tmp_path / "report",
            max_adjustment=Decimal("0.20"), kalshi_client=client,
        )
    report = json.loads(path.read_text())
    row = report["rows"][0]
    assert row["baseline_probability_without_observation"] == "0.45"
    assert row["weather_v2_temperature_signal_preview"] == "-0.1005"
    assert row["weather_v2_adjustment_preview"] == "-0.020100"
    assert row["probability_with_observation_preview"] == "0.429900"
    assert row["evidence_source"] == "noaa_nws_observation_non_settlement_evidence"
    assert report["database_writes"] == 0
    assert report["runtime_weather_v2_changed"] is False
    assert report["thresholds_changed"] is False


def test_nyc_w4_requires_exact_passing_certification(tmp_path: Path) -> None:
    certification = tmp_path / "certification.json"
    certification.write_text(json.dumps({"exact_ticker_certification": False, "rows": []}))
    try:
        write_nyc_w4_report(
            certification_path=certification, output_dir=tmp_path,
            max_adjustment=Decimal("0.20"),
        )
    except ValueError as exc:
        assert "exact-ticker" in str(exc)
    else:
        raise AssertionError("Expected exact certification requirement")
