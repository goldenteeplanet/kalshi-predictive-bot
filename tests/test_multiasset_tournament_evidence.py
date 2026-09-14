import json
from datetime import timedelta

import pytest
from test_multiasset_capture import TARGET
from test_multiasset_outcomes import frozen

from kalshi_predictor.crypto import multiasset_capture as C
from kalshi_predictor.crypto import multiasset_outcomes as O
from kalshi_predictor.crypto.multiasset_tournament_evidence import verify_outcomes as audit


def verify_outcomes(*args):
    return audit(*args, as_of=TARGET + timedelta(minutes=11))


def completed(tmp_path):
    root, pin, receipt, sha = frozen(tmp_path)
    markets = json.loads((root / "catalog.original.json").read_bytes())["markets"]

    def transport(url, timeout):
        m = next(m for m in markets if url.endswith("/" + m["ticker"]))
        return 200, C.encode(
            {
                "market": {
                    **m,
                    "status": "finalized",
                    "result": "yes",
                    "settlement_value_dollars": "1",
                    "settlement_ts": (TARGET + timedelta(minutes=2)).isoformat(),
                }
            }
        )

    out = tmp_path / "outcome"
    O.collect(out, root, pin, receipt, sha, transport, clock=lambda: TARGET + timedelta(minutes=10))
    return root, out, pin, receipt, sha


def test_authenticated_decisions_and_unknown_economics(tmp_path):
    result = verify_outcomes(*completed(tmp_path))
    assert len(result["rows"]) == 4
    assert result["verified_score_rows"] == 20
    assert result["economics"]
    assert all(
        r["full_net_ev"] is None and r["formal_full_net_near_miss"] is None
        for r in result["economics"]
    )


def test_altered_score_rejected_even_if_output_manifest_rehashed(tmp_path):
    args = completed(tmp_path)
    out = args[1]
    value = json.loads((out / "evaluation.json").read_bytes())
    value["rows"][0]["score"]["brier"] = "0.123456789"
    raw = C.encode(value)
    (out / "evaluation.json").write_bytes(raw)
    completion = json.loads((out / "completion.json").read_bytes())
    completion["files"]["evaluation.json"] = C.digest(raw)
    (out / "completion.json").write_bytes(C.encode(completion))
    with pytest.raises(ValueError, match="PROSPECTIVE_PREDICTIONS"):
        verify_outcomes(*args)


def test_duplicated_outcome_row_and_failure_marker_rejected(tmp_path):
    args = completed(tmp_path)
    (args[1] / "failure.json").write_text("{}")
    with pytest.raises(ValueError, match="TERMINAL_OUTCOME_FAILURE"):
        verify_outcomes(*args)


def test_future_completion_cannot_be_reported_as_current_evidence(tmp_path):
    with pytest.raises(ValueError, match="COMPLETED_OUTCOME"):
        audit(*completed(tmp_path), as_of=TARGET)
