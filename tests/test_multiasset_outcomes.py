import json
from datetime import timedelta

import pytest
from test_multiasset_capture import NOW, TARGET, setup

from kalshi_predictor.crypto import multiasset_capture as C
from kalshi_predictor.crypto import multiasset_outcomes as O


def frozen(tmp_path):
    plan, transport, _ = setup()
    root = tmp_path / "capture"
    C.capture(root, plan, transport, clock=lambda: NOW)
    plan_sha = C.digest((root / "protocol.json").read_bytes())
    pin = C.encode(
        {
            "event": plan["event"],
            "target_at": plan["target_at"],
            "at": NOW.isoformat(),
            "protocol_sha256": plan_sha,
            "completion_sha256": C.digest((root / "completion.json").read_bytes()),
        }
    )
    receipt = C.encode(
        {"pin_sha256": C.digest(pin), "at": (NOW + timedelta(seconds=1)).isoformat()}
    )
    return root, pin, receipt, plan_sha


def test_official_outcomes_score_frozen_predictions_once(tmp_path):
    root, pin, receipt, sha = frozen(tmp_path)
    markets = json.loads((root / "catalog.original.json").read_bytes())["markets"]
    calls = []

    def transport(url, timeout):
        calls.append(url)
        market = next(m for m in markets if url.endswith("/" + m["ticker"]))
        return 200, C.encode(
            {
                "market": {
                    **market,
                    "status": "finalized",
                    "result": "yes",
                    "settlement_value_dollars": "1",
                    "settlement_ts": (TARGET + timedelta(minutes=2)).isoformat(),
                }
            }
        )

    output = tmp_path / "outcome"
    result = O.collect(
        output, root, pin, receipt, sha, transport, clock=lambda: TARGET + timedelta(minutes=10)
    )
    assert result == {"rows": 20, "requests": 2}
    evaluation = json.loads((output / "evaluation.json").read_bytes())
    assert evaluation["paper_pnl"] is None
    for row in evaluation["rows"]:
        assert row["hypothetical_after_cost_pnl"] is None
    with pytest.raises(FileExistsError):
        O.collect(
            output, root, pin, receipt, sha, transport, clock=lambda: TARGET + timedelta(minutes=10)
        )
    assert len(calls) == 2


def test_changed_input_rejected_before_collection(tmp_path):
    root, pin, receipt, sha = frozen(tmp_path)
    path = root / "cf.original.json"
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ValueError, match="HASH"):
        O.verify_capture(root, pin, receipt, sha)


def test_post_target_pin_does_not_establish_prospective_capture(tmp_path):
    root, pin, receipt, sha = frozen(tmp_path)
    value = json.loads(pin)
    value["at"] = (TARGET + timedelta(seconds=1)).isoformat()
    pin = C.encode(value)
    receipt = C.encode({"pin_sha256": C.digest(pin), "at": value["at"]})
    with pytest.raises(ValueError, match="PRETARGET"):
        O.verify_capture(root, pin, receipt, sha)


def test_unfinalized_market_never_scored(tmp_path):
    root, pin, receipt, sha = frozen(tmp_path)
    market = json.loads((root / "catalog.original.json").read_bytes())["markets"][0]

    def transport(url, timeout):
        return 200, C.encode({"market": {**market, "status": "determined", "result": "yes"}})

    with pytest.raises(ValueError):
        O.collect(
            tmp_path / "outcome",
            root,
            pin,
            receipt,
            sha,
            transport,
            clock=lambda: TARGET + timedelta(minutes=10),
        )
    assert (tmp_path / "outcome/failure.json").exists()
    assert not (tmp_path / "outcome/evaluation.json").exists()
