from __future__ import annotations

import json
import sys
from datetime import UTC, datetime

from scripts import weather_fast_preflight_soak


def test_three_cycle_soak_is_healthy_and_cycle_idempotent(tmp_path, monkeypatch) -> None:
    preflight = tmp_path / "preflight.json"
    gate = tmp_path / "gate.json"
    history = tmp_path / "history.jsonl"
    output = tmp_path / "soak.json"
    preflight.write_text(
        json.dumps(
            {
                "generated_at": datetime.now(UTC).isoformat(),
                "paper_orders_before": 10,
                "paper_orders_after": 10,
                "results": [
                    {
                        "ticker": "RAIN",
                        "status": "RECORDED",
                        "forecast_snapshot_pair_key": "forecast:1:snapshot:2",
                        "phase3n_action": "ALLOW",
                        "phase3n_hard_blocks": [],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    gate.write_text(
        json.dumps({"weather_rows": [{"ticker": "RAIN", "paper_ready": True}]}),
        encoding="utf-8",
    )

    for cycle_id in ("1", "2", "2", "3"):
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "weather_fast_preflight_soak.py",
                "--cycle-id",
                cycle_id,
                "--preflight",
                str(preflight),
                "--gate",
                str(gate),
                "--history",
                str(history),
                "--output",
                str(output),
                "--required-cycles",
                "3",
            ],
        )
        weather_fast_preflight_soak.main()

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["soak_complete"] is True
    assert payload["consecutive_healthy_cycles"] == 3
    assert payload["paper_order_creation_enabled"] is False
    assert len(history.read_text(encoding="utf-8").splitlines()) == 3
