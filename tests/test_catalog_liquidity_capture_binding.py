import json
import sqlite3

import pytest
from test_multiasset_capture import NOW, setup

from kalshi_predictor.crypto import multiasset_capture as C


@pytest.mark.parametrize("complete", [True, False])
def test_catalog_score_binding_is_durable_without_paper_authority(tmp_path, complete):
    plan, transport, calls = setup()

    def with_catalog_inputs(url, timeout):
        status, raw = transport(url, timeout)
        data = json.loads(raw)
        if "markets" in data and complete:
            for market in data["markets"]:
                market.update(volume_fp="1000", open_interest_fp="500", liquidity_dollars="0")
        if "orderbook_fp" in data:
            data["orderbook_fp"]["no_dollars"] = [["0.59", "4"]]
        return status, C.encode(data)

    output = tmp_path / "capture"
    C.capture(output, plan, with_catalog_inputs, clock=lambda: NOW)
    assert len(calls) == 6
    catalog_hash = C.digest((output / "catalog.original.json").read_bytes())
    with sqlite3.connect(output / "research.db") as db:
        rows = db.execute("SELECT payload,sha256 FROM decisions").fetchall()
    assert len(rows) == 4
    for raw, digest in rows:
        assert C.digest(raw) == digest
        payload = json.loads(raw)
        binding = payload["catalog_liquidity"]
        assert binding["catalog_sha256"] == catalog_hash
        assert payload["input_manifest"]["catalog.original.json"] == catalog_hash
        assert binding["score"] == ("60.00" if complete else None)
        assert all(row["book"]["usable"] is complete for row in payload["rows"])
        assert not payload["paper_eligible"] and not payload["execution_authority"]
        assert all(
            row["costs"] is None or row["costs"]["full_net_ev"] is None for row in payload["rows"]
        )
