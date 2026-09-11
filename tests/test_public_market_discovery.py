import json
from datetime import UTC, datetime, timedelta

import pytest

from kalshi_predictor.ingest import public_market_discovery as discovery

NOW = datetime(2026, 9, 11, 12, tzinfo=UTC)


def market(ticker, *, bid="0.45", ask="0.55", hours=1):
    return dict(
        ticker=ticker,
        status="active",
        close_time=(NOW + timedelta(hours=hours)).isoformat(),
        yes_bid_dollars=bid,
        yes_ask_dollars=ask,
    )


def test_prioritizes_next_window_and_near_midpoint_without_expired_or_foreign():
    rows = [
        market("KXTEMP-A", bid="0.95", ask="1"),
        market("KXTEMP-B"),
        market("KXTEMP-C", hours=-1),
        market("KXOTHER-D"),
        market("KXTEMP-E", hours=73),
        market("KXTEMP-../X"),
        market("KXTEMP-F", hours=2),
    ]
    assert discovery.eligible_tickers(rows, "KXTEMP", as_of=NOW) == [
        "KXTEMP-B",
        "KXTEMP-A",
        "KXTEMP-F",
    ]


def test_discovery_breaks_empty_manifest_dependency_and_preserves_partial_catalog(
    tmp_path, monkeypatch
):
    calls = []

    def get(url):
        calls.append(url)
        family = "KXTEMP" if "KXTEMP" in url else "KXBTC"
        return json.dumps(
            dict(markets=[market(f"{family}-{i}") for i in range(8)], cursor="more")
        ).encode(), 200

    def stage(**kwargs):
        assert len(kwargs["tickers"]) == 6
        assert kwargs["tickers"][:2] == ["KXTEMP-0", "KXBTC-0"]
        return dict(status="COMPLETE", requests=12, staged=[], errors=[])

    monkeypatch.setattr(discovery, "stage_public_books", stage)
    output = tmp_path / "out"
    result = discovery.discover_and_stage(
        series=["KXTEMP", "KXBTC"],
        staging_dir=tmp_path / "stage",
        evidence_dir=output,
        get=get,
        clock=lambda: NOW,
    )
    assert result["requests"] == 14 and len(calls) == 2
    assert all(c["partial"] for c in result["catalogs"])
    assert (output / "catalog-0.original").exists()
    with pytest.raises(FileExistsError):
        discovery.discover_and_stage(
            series=["KXTEMP"],
            staging_dir=tmp_path / "stage",
            evidence_dir=output,
            get=get,
            clock=lambda: NOW,
        )
    assert len(calls) == 2


def test_invalid_scope_refused_before_network_or_output(tmp_path):
    for series in [[], ["../escape"], [f"KX{i}" for i in range(7)]]:
        with pytest.raises(ValueError):
            discovery.discover_and_stage(
                series=series,
                staging_dir=tmp_path / "stage",
                evidence_dir=tmp_path / "out",
                get=lambda _: pytest.fail("network"),
            )
    assert not (tmp_path / "out").exists()


def test_watch_discovery_does_not_read_absent_ranking_manifest(tmp_path, monkeypatch):
    from kalshi_predictor.ingest import public_book_watch

    calls = []

    def discover(**kwargs):
        calls.append(kwargs["series"])
        return dict(status="COMPLETE", requests=1)

    monkeypatch.setattr(public_book_watch, "discover_and_stage", discover)
    public_book_watch.run(
        manifest=None, staging_dir=tmp_path / "stage", output=tmp_path / "watch",
        cycles=1, discovery_series=["KXTEMP"],
    )
    assert calls == [["KXTEMP"]]
    assert json.loads((tmp_path / "watch" / "terminal.json").read_text())["status"] == "TERMINAL"
