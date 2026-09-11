import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest

from kalshi_predictor.ingest import public_market_discovery as discovery

NOW = datetime(2026, 9, 11, 12, tzinfo=UTC)


def test_entry_cutoff_excludes_exact_boundary_and_uses_configured_value():
    rows = [market("KXTEMP-NEAR", hours=0.5), market("KXTEMP-NEXT", hours=1)]
    excluded = []
    settings = SimpleNamespace(opportunity_min_time_to_close_minutes=Decimal("30"))
    assert discovery.eligible_tickers(
        rows, "KXTEMP", as_of=NOW, settings=settings, excluded_windows=excluded
    ) == ["KXTEMP-NEXT"]
    assert excluded[0]["window_status"] == "MARKET_CLOSE_TOO_NEAR"
    assert excluded[0]["final_entry_cutoff_time"] == NOW.isoformat()
    assert discovery.eligible_tickers(
        rows, "KXTEMP", as_of=NOW - timedelta(microseconds=1), settings=settings
    ) == ["KXTEMP-NEAR", "KXTEMP-NEXT"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("result", "yes"),
        ("settlement_ts", NOW.isoformat()),
        ("expected_expiration_time", NOW.isoformat()),
        ("expiration_time", NOW.isoformat()),
    ],
)
def test_open_status_does_not_override_terminal_or_expired_evidence(field, value):
    row = market("KXTEMP-A")
    row[field] = value
    assert discovery.eligible_tickers([row], "KXTEMP", as_of=NOW) == []


def test_near_close_catalog_does_not_spend_book_requests(tmp_path):
    calls = []

    def get(url):
        calls.append(url)
        return json.dumps(dict(markets=[market("KXTEMP-A", hours=0.25)])).encode(), 200

    result = discovery.discover_and_stage(
        series=["KXTEMP"],
        staging_dir=tmp_path / "stage",
        evidence_dir=tmp_path / "out",
        get=get,
        clock=lambda: NOW,
        settings=SimpleNamespace(opportunity_min_time_to_close_minutes=Decimal("30")),
    )
    assert len(calls) == result["requests"] == 1
    assert result["selected"] == []
    assert result["catalogs"][0]["excluded_windows"][0]["window_status"] == "MARKET_CLOSE_TOO_NEAR"


def test_catalog_request_filters_entry_window_before_pagination(tmp_path, monkeypatch):
    def get(url):
        query = parse_qs(urlparse(url).query)
        assert "status" not in query
        assert query["min_close_ts"] == [str(int((NOW + timedelta(minutes=45)).timestamp()))]
        assert query["max_close_ts"] == [str(int((NOW + timedelta(hours=72)).timestamp()))]
        assert query["limit"] == ["100"]
        rows = [market("KXTEMP-NEXT", hours=2), market("KXTEMP-NEAR", hours=0.5)]
        rows.append(dict(market("KXTEMP-SETTLED", hours=2), status="settled"))
        return json.dumps(dict(markets=rows)).encode(), 200

    def stage(**kwargs):
        assert kwargs["tickers"] == ["KXTEMP-NEXT"]
        return dict(requests=2, staged=[], errors=[])

    monkeypatch.setattr(discovery, "stage_public_books", stage)
    result = discovery.discover_and_stage(
        series=["KXTEMP"],
        staging_dir=tmp_path / "stage",
        evidence_dir=tmp_path / "out",
        get=get,
        clock=lambda: NOW,
        settings=SimpleNamespace(opportunity_min_time_to_close_minutes=Decimal("45")),
    )
    assert result["selected"] == ["KXTEMP-NEXT"]
    assert result["requests"] == 3


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
        if family == "KXBTC":
            event = "KXBTC-EVENT"
            return json.dumps(
                dict(
                    events=[
                        dict(
                            series_ticker=family,
                            event_ticker=event,
                            markets=[
                                dict(market(f"{event}-{i}"), event_ticker=event) for i in range(8)
                            ],
                        )
                    ],
                    cursor="more",
                )
            ).encode(), 200
        return json.dumps(
            dict(markets=[market(f"{family}-{i}") for i in range(8)], cursor="more")
        ).encode(), 200

    def stage(**kwargs):
        assert len(kwargs["tickers"]) == 6
        assert kwargs["tickers"][:2] == ["KXTEMP-0", "KXBTC-EVENT-0"]
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


def test_crypto_event_discovery_covers_more_than_one_market_page(tmp_path, monkeypatch):
    event = "KXBTC-EVENT"

    def get(url):
        query = parse_qs(urlparse(url).query)
        assert urlparse(url).path.endswith("/events")
        assert query["status"] == ["open"] and query["limit"] == ["2"]
        assert query["with_nested_markets"] == ["true"]
        rows = [
            dict(market(f"{event}-{i:03}", bid="0.99", ask="1"), event_ticker=event)
            for i in range(188)
        ]
        rows[-1].update(yes_bid_dollars="0.45", yes_ask_dollars="0.55")
        return json.dumps(
            dict(events=[dict(event_ticker=event, series_ticker="KXBTC", markets=rows)], cursor="")
        ).encode(), 200

    def stage(**kwargs):
        assert kwargs["tickers"][0] == "KXBTC-EVENT-187"
        return dict(requests=12, staged=[], errors=[])

    monkeypatch.setattr(discovery, "stage_public_books", stage)
    r = discovery.discover_and_stage(
        series=["KXBTC"],
        staging_dir=tmp_path / "stage",
        evidence_dir=tmp_path / "out",
        get=get,
        clock=lambda: NOW,
    )
    assert r["requests"] == 13 and r["catalogs"][0]["rows"] == 188
    assert r["catalogs"][0]["partial"] is False


@pytest.mark.parametrize("mutation", ["wrong_series", "wrong_event", "oversize", "too_many_events"])
def test_event_membership_and_caps(mutation):
    event = dict(
        event_ticker="KXBTC-E",
        series_ticker="KXBTC",
        markets=[dict(market("KXBTC-E-1"), event_ticker="KXBTC-E")],
    )
    if mutation == "wrong_series":
        event["series_ticker"] = "KXETH"
    elif mutation == "wrong_event":
        event["markets"][0]["event_ticker"] = "KXBTC-OTHER"
    elif mutation == "oversize":
        event["markets"] *= 401
    with pytest.raises(ValueError):
        discovery.event_markets(
            dict(events=[event] * (3 if mutation == "too_many_events" else 1)), "KXBTC"
        )


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
        manifest=None,
        staging_dir=tmp_path / "stage",
        output=tmp_path / "watch",
        cycles=1,
        discovery_series=["KXTEMP"],
    )
    assert calls == [["KXTEMP"]]
    assert json.loads((tmp_path / "watch" / "terminal.json").read_text())["status"] == "TERMINAL"
