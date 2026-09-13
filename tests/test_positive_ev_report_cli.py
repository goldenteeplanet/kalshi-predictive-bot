import json

import pytest
import typer
from typer.testing import CliRunner

from kalshi_predictor.overnight_paper.cli import register_commands
from kalshi_predictor.overnight_paper.positive_ev_report import MAX_BYTES, read_positive_ev_report


def app():
    result = typer.Typer()
    register_commands(result)
    return result


def test_registered_commands_recompute_and_refuse_archived_certification(tmp_path):
    path = tmp_path / "qualified_scan.json"
    inputs = dict(
        ticker="TEST",
        category="Weather",
        side="BUY_YES",
        forecast_probability="0.7",
        executable_price="0.6",
        estimated_fee="0.02",
        slippage="0.01",
        uncertainty="0.03",
        settings={"paper_min_edge": "0.05"},
    )
    path.write_text(
        json.dumps(
            {
                "rows": [
                    dict(
                        frozen_decision_inputs=inputs,
                        net_ev="999",
                        preparation_present=True,
                        rule_status="CERTIFIED",
                        source_status="FRESH_ORIGINALS_VERIFIED",
                        model_evaluation_verified=True,
                        selected_for_book_refresh=True,
                        book_status="EXECUTABLE",
                    ),
                    dict(ticker="MISSING", net_ev="888", probability="1", rule_status="CERTIFIED"),
                ]
            }
        )
    )
    before = path.read_bytes()
    runner = CliRunner()
    result = runner.invoke(app(), ["positive-ev-report", "--artifact", str(path), "--json"])
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["top_frozen_candidates"][0]["net_ev"] == "0.04"
    assert report["rows"][1]["net_ev"] is None
    assert report["current_net_ev"] is None
    assert [stage["count"] for stage in report["funnel"]["stages"]] == [2, 0, 0, 0, 0, 0, 0, 0]
    result = runner.invoke(app(), ["positive-ev-funnel", "--artifact", str(path)])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["authority"] == "NO_EXECUTION_AUTHORITY"
    result = runner.invoke(app(), ["positive-ev-report", "--artifact", str(path)])
    assert result.exit_code == 0
    assert "Current net EV: unknown" in result.output
    assert path.read_bytes() == before


@pytest.mark.parametrize("payload", [{"rows": [{}] * 1001}, {"rows": [True]}, {"ready": True}])
def test_invalid_rows_refused(tmp_path, payload):
    path = tmp_path / "artifact.json"
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError):
        read_positive_ev_report(path)


def test_size_bound_and_cli_error(tmp_path):
    path = tmp_path / "artifact.json"
    path.write_bytes(b" " * (MAX_BYTES + 1))
    with pytest.raises(ValueError, match="4_MIB"):
        read_positive_ev_report(path)
    result = CliRunner().invoke(app(), ["positive-ev-report", "--artifact", str(path)])
    assert result.exit_code != 0
