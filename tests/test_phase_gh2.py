import json
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

from kalshi_predictor import phase_gh2
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.repositories import insert_market_snapshot
from kalshi_predictor.opportunities.repository import insert_market_ranking
from kalshi_predictor.phase_gh2 import select_actionable_ranked_markets
from kalshi_predictor.utils.time import utc_now
from kalshi_predictor.weather.features import WeatherFeatureBuildSummary


def test_candidate_alignment_prioritizes_fresh_executable_rankings(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    now = utc_now()
    with session_factory() as session:
        _seed_ranked_market(
            session,
            ticker="KXBTC-FRESH",
            captured_at=now,
            close_time=now + timedelta(hours=2),
            edge="0.04",
            score="70",
        )
        _seed_ranked_market(
            session,
            ticker="KXBTC-STALE",
            captured_at=now - timedelta(hours=2),
            close_time=now + timedelta(hours=2),
            edge="0.20",
            score="99",
        )
        _seed_ranked_market(
            session,
            ticker="KXBTC-CLOSED",
            captured_at=now,
            close_time=now - timedelta(minutes=1),
            edge="0.50",
            score="100",
            status="closed",
        )

        rows = select_actionable_ranked_markets(
            session,
            limit=10,
            max_per_series=10,
            now=now,
        )

    assert [row["ticker"] for row in rows] == ["KXBTC-FRESH", "KXBTC-STALE"]
    assert rows[0]["selection_tier"] == "FRESH_EXECUTABLE_POSITIVE_EDGE"
    assert rows[0]["fresh"] is True
    assert rows[1]["fresh"] is False


def test_snapshot_recovery_candidates_break_ranked_only_selection_loop() -> None:
    payload = {
        "generated_at": "2026-07-21T12:00:00+00:00",
        "blocked_active_pure_examples": [
            {
                "ticker": "KXXRP-RECOVERY",
                "series_ticker": "KXXRP",
                "blocked_reason": "BLOCKED_MISSING_ACTIVE_SNAPSHOT",
                "latest_snapshot_at": None,
            },
            {
                "ticker": "KXBTC-BOOK",
                "series_ticker": "KXBTC",
                "blocked_reason": "BLOCKED_NO_EXECUTABLE_BOOK",
            },
            {
                "ticker": "KXTEMPNYCH-OTHER",
                "series_ticker": "KXTEMPNYCH",
                "blocked_reason": "BLOCKED_MISSING_ACTIVE_SNAPSHOT",
            },
        ],
    }

    rows = phase_gh2._snapshot_recovery_candidates(payload, limit=10)

    assert [row["ticker"] for row in rows] == ["KXXRP-RECOVERY"]
    assert rows[0]["selection_tier"] == "MISSING_SNAPSHOT_RECOVERY"
    assert rows[0]["blocking_gates"] == ["snapshot_missing"]


def test_manifest_merge_reserves_capacity_for_snapshot_recovery() -> None:
    ranked = [{"ticker": f"KXBTC-RANKED-{index}"} for index in range(5)]
    recovery = [{"ticker": f"KXXRP-RECOVERY-{index}"} for index in range(2)]

    rows = phase_gh2._merge_manifest_candidates(ranked, recovery, limit=4)

    assert [row["ticker"] for row in rows] == [
        "KXBTC-RANKED-0",
        "KXBTC-RANKED-1",
        "KXXRP-RECOVERY-0",
        "KXXRP-RECOVERY-1",
    ]


def test_gh2_systemd_units_preserve_paper_only_single_writer_contract() -> None:
    root = Path(__file__).parents[1]
    implementation = (root / "src/kalshi_predictor/phase_gh2.py").read_text(encoding="utf-8")
    service = (root / "deploy/systemd/kalshi-gh2-decision-refresh.service").read_text(
        encoding="utf-8"
    )
    timer = (root / "deploy/systemd/kalshi-gh2-decision-refresh.timer").read_text(encoding="utf-8")
    script = (root / "scripts/cloud/kalshi-gh2-decision-refresh.sh").read_text(encoding="utf-8")

    assert "EXECUTION_ENABLED=false" in service
    assert "AUTOPILOT_ENABLED=false" in service
    assert "OnUnitActiveSec=15min" in timer
    assert "flock -n 9" in script
    assert "db-writer-monitor --json" in script
    assert "gh2-stage-crypto-quotes" in script
    assert "gh2-single-writer-decision-refresh" in script
    assert "--apply" in script
    assert "--active-link-limit 24" in script
    assert "--forecast-limit 24" in script
    assert "--opportunity-limit 20" in script
    assert "paper-order" not in script.lower()
    assert "latest_snapshots_for_model" not in implementation
    assert "_latest_snapshots(session, crypto_link_tickers)" in implementation
    assert "_latest_snapshots(session, weather_decision_tickers)" in implementation
    assert "parse_and_store_market_legs(" in implementation
    assert "tickers=_bounded_unique(" in implementation


def test_weather_feature_refresh_is_strictly_bounded(monkeypatch) -> None:
    calls = []

    class FakeSession:
        def scalars(self, statement):
            return iter(("new_york", "chicago", "miami"))

    def fake_build(session, *, location_key, settings, limit):
        calls.append((location_key, limit))
        return WeatherFeatureBuildSummary(
            location_key=location_key,
            forecasts_processed=limit,
            features_inserted=limit,
        )

    monkeypatch.setattr(phase_gh2, "build_weather_features", fake_build)

    summaries = phase_gh2._build_current_weather_features(
        FakeSession(),
        ["KXTEMPNYCH-TEST"],
        settings=SimpleNamespace(),
        max_locations=2,
        forecasts_per_location=4,
    )

    assert calls == [("new_york", 4), ("chicago", 4)]
    assert len(summaries) == 2


def test_soak_history_records_candidate_and_reset_evidence(tmp_path: Path) -> None:
    history_path = tmp_path / "soak.jsonl"
    history_path.write_text(
        "".join(
            json.dumps(
                {
                    "generated_at": utc_now().isoformat(),
                    "healthy": True,
                    "paper_ready_candidates": 0,
                    "rankings_inserted": 2,
                }
            )
            + "\n"
            for _ in range(23)
        ),
        encoding="utf-8",
    )

    result = phase_gh2._record_soak_cycle(
        history_path,
        healthy=True,
        paper_ready_candidates=1,
        positive_ev_rows=3,
        rankings_inserted=2,
        fresh_ranked_candidates=4,
        reset_reason=None,
        required_cycles=24,
    )

    latest = json.loads(history_path.read_text(encoding="utf-8").splitlines()[-1])
    assert result["soak_complete"] is True
    assert latest["positive_ev_rows"] == 3
    assert latest["fresh_ranked_candidates"] == 4
    assert latest["reset_reason"] is None


def _session_factory(tmp_path: Path):
    engine = init_db(f"sqlite:///{tmp_path / 'gh2.db'}")
    return get_session_factory(engine)


def _seed_ranked_market(
    session,
    *,
    ticker: str,
    captured_at,
    close_time,
    edge: str,
    score: str,
    status: str = "open",
) -> None:
    insert_market_snapshot(
        session,
        {
            "ticker": ticker,
            "status": status,
            "title": f"Will {ticker} resolve yes?",
            "series_ticker": "KXBTC",
            "close_time": close_time.isoformat(),
            "liquidity_dollars": "1000",
        },
        {
            "orderbook_fp": {
                "yes_dollars": [["0.40", "20"]],
                "no_dollars": [["0.50", "20"]],
            }
        },
        captured_at,
    )
    insert_market_ranking(
        session,
        {
            "ticker": ticker,
            "ranked_at": captured_at,
            "title": f"Will {ticker} resolve yes?",
            "status": status,
            "series_ticker": "KXBTC",
            "forecast_model": "crypto_v2",
            "forecast_probability": "0.60",
            "best_side": "BUY_YES",
            "best_price": "0.40",
            "estimated_edge": edge,
            "liquidity_score": "80",
            "spread_score": "80",
            "time_score": "80",
            "model_confidence_score": "80",
            "opportunity_score": score,
            "spread": "0.10",
            "liquidity": "1000",
            "reason": "GH-2 candidate fixture.",
        },
    )
