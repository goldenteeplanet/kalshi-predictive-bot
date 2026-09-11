import json
import math
from datetime import timedelta
from decimal import Decimal

import pytest
from test_settlement_target_research_router import NOW, target

from kalshi_predictor.crypto.cf_process_inputs import (
    decode_cf_original,
    digest,
    estimate_cf_process,
)
from kalshi_predictor.crypto.settlement_average_model import forecast_benchmark_average


def encode(value):
    return json.dumps(value, separators=(",", ":")).encode()


def fixture(profile="LATEST_1HZ"):
    cadence, count = (1000, 3600) if profile == "LATEST_1HZ" else (200, 18000)
    start = int((NOW - timedelta(hours=1)).timestamp() * 1000)
    body = {
        "data": {
            "serverTime": NOW.isoformat(),
            "payload": [
                {"time": start + i * cadence, "value": "100.00" if i % 2 == 0 else "101.00"}
                for i in range(count)
            ],
        }
    }
    url = "https://external-api.kalshi.com/trade-api/v2/cfbenchmarks/values?id=BRTI"
    if profile == "HOUR_5HZ":
        url = "https://external-api.kalshi.com/trade-api/v2/cfbenchmarks/history/values?id=BRTI&timespan=HOUR&timestamp=2026-09-11T09:00:00.000Z"
    receipt = dict(
        schema="cf-response-receipt-v1",
        method="GET",
        url=url,
        index_id="BRTI",
        profile=profile,
        http_status=200,
        source_sha256="",
        requested_at=(NOW - timedelta(seconds=2)).isoformat(),
        received_at=NOW.isoformat(),
        recorded_at=NOW.isoformat(),
    )
    return body, receipt


def estimate(body, receipt, as_of=NOW):
    raw = encode(body)
    receipt = dict(receipt, source_sha256=digest(raw))
    receipt_raw = encode(receipt)
    return estimate_cf_process(
        raw,
        receipt_raw,
        source_sha256=digest(raw),
        receipt_sha256=digest(receipt_raw),
        target=target(),
        as_of=as_of,
    )


@pytest.mark.parametrize(
    "profile,count,cadence", [("LATEST_1HZ", 3600, 1000), ("HOUR_5HZ", 18000, 200)]
)
def test_real_profile_coverage_price_and_independent_sample_variance(profile, count, cadence):
    body, receipt = fixture(profile)
    result = estimate(body, receipt)
    n = count - 1
    a = math.log(101) - math.log(100)
    # Alternating +a/-a returns, odd N: sum=a, sum of squares=N*a*a.
    expected = math.sqrt((n * a * a - a * a / n) / (n - 1) / (cadence / 60000))
    assert result.process.level == Decimal("101.00")
    assert result.process.volatility_per_sqrt_minute == pytest.approx(expected, rel=1e-12)
    assert result.evidence["prices"] == count
    assert result.evidence["cadence_ms"] == cadence
    assert result.evidence["elapsed_minutes"] == (count - 1) * cadence / 60000
    assert result.evidence["downsampling"] is False
    forecast = forecast_benchmark_average(target(), result.process, as_of=NOW)
    assert 0 <= forecast["probability"] <= 1
    assert forecast["paper_eligible"] is False
    assert forecast["settlement_alignment_certified"] is False


def test_constant_actual_levels_give_zero_variance_without_imputation():
    body, receipt = fixture()
    for row in body["data"]["payload"]:
        row["value"] = "100.00"
    result = estimate(body, receipt)
    assert result.process.volatility_per_sqrt_minute == 0
    assert forecast_benchmark_average(target(), result.process, as_of=NOW)["probability"] == 0


@pytest.mark.parametrize(
    "change",
    [
        lambda b, r: b["data"]["payload"].pop(),
        lambda b, r: b["data"]["payload"][4].update(time=b["data"]["payload"][3]["time"]),
        lambda b, r: b["data"]["payload"][4].update(time=b["data"]["payload"][4]["time"] + 200),
        lambda b, r: b["data"]["payload"][4].update(value=True),
        lambda b, r: b["data"]["payload"][4].update(value="NaN"),
        lambda b, r: b["data"]["payload"][4].update(value="1e999"),
        lambda b, r: b["data"]["payload"][4].update(value="0"),
        lambda b, r: b["data"]["payload"][4].update(amendTime=123),
        lambda b, r: b["data"].update(extra=1),
        lambda b, r: r.update(profile="HOUR_5HZ"),
        lambda b, r: r.update(index_id="ETHUSD_RTI"),
        lambda b, r: r.update(http_status=True),
        lambda b, r: r.update(received_at=(NOW + timedelta(seconds=1)).isoformat()),
        lambda b, r: r.update(recorded_at=(NOW + timedelta(seconds=1)).isoformat()),
        lambda b, r: r.update(requested_at=(NOW + timedelta(seconds=1)).isoformat()),
        lambda b, r: r.update(received_at="2026-09-11T10:00:00"),
        lambda b, r: r.pop("received_at"),
        lambda b, r: r.update(url=r["url"] + "&id=BRTI"),
        lambda b, r: r.update(
            url=r["url"].replace("external-api.kalshi.com", "external-api.kalshi.com:443")
        ),
        lambda b, r: r.update(url=r["url"] + "#ignored"),
    ],
)
def test_changed_original_or_receipt_rejected_even_with_recomputed_hash(change):
    body, receipt = fixture()
    change(body, receipt)
    with pytest.raises(ValueError):
        estimate(body, receipt)


def test_exact_original_and_receipt_hashes_required():
    body, receipt = fixture()
    raw = encode(body)
    receipt["source_sha256"] = digest(raw)
    rr = encode(receipt)
    for source_hash, receipt_hash in [("0" * 64, digest(rr)), (digest(raw), "0" * 64)]:
        with pytest.raises(ValueError):
            estimate_cf_process(
                raw,
                rr,
                source_sha256=source_hash,
                receipt_sha256=receipt_hash,
                target=target(),
                as_of=NOW,
            )


def test_duplicate_original_keys_rejected_with_matching_digest():
    body, receipt = fixture()
    raw = encode(body).replace(b'"value":"100.00"', b'"value":"200.00","value":"100.00"', 1)
    with pytest.raises(ValueError, match="DUPLICATE"):
        decode_cf_original(
            raw,
            sha256=digest(raw),
            request_url=receipt["url"],
            index_id="BRTI",
            profile="LATEST_1HZ",
        )


def test_stale_data_and_server_future_refused():
    body, receipt = fixture()
    with pytest.raises(ValueError, match="STALE"):
        estimate(body, receipt, NOW + timedelta(seconds=61))
    body["data"]["serverTime"] = (NOW + timedelta(seconds=1)).isoformat()
    with pytest.raises(ValueError, match="FUTURE"):
        estimate(body, receipt)


def test_history_exact_requested_hour_and_profile_cannot_be_relabelled():
    body, receipt = fixture("HOUR_5HZ")
    receipt["url"] = receipt["url"].replace("09:00", "08:00")
    with pytest.raises(ValueError, match="WINDOW"):
        estimate(body, receipt)
    body, receipt = fixture("HOUR_5HZ")
    receipt["profile"] = "LATEST_1HZ"
    receipt["url"] = fixture()[1]["url"]
    with pytest.raises(ValueError, match="COVERAGE"):
        estimate(body, receipt)


def coverage_receipt(body, receipt):
    from datetime import UTC, datetime

    rows = body["data"]["payload"]
    return dict(
        index=receipt["index_id"],
        classification="ACCESSIBLE",
        request_count=1,
        request_started_at=receipt["requested_at"],
        received_at=receipt["received_at"],
        http_status=200,
        url=receipt["url"],
        sha256=digest(encode(body)),
        observations=len(rows),
        duplicates=0,
        monotonic=True,
        step_counts_ms={"1000": len(rows) - 1},
        first_timestamp=datetime.fromtimestamp(rows[0]["time"] / 1000, UTC).isoformat(),
        last_timestamp=datetime.fromtimestamp(rows[-1]["time"] / 1000, UTC).isoformat(),
        parse_errors=0,
        terminal=True,
    )


@pytest.mark.parametrize(
    "symbol,index",
    [
        ("BTC", "BRTI"),
        ("ETH", "ETHUSD_RTI"),
        ("SOL", "SOLUSD_RTI"),
        ("XRP", "XRPUSD_RTI"),
        ("DOGE", "DOGEUSD_RTI"),
    ],
)
def test_original_coverage_receipt_and_explicit_index_map(symbol, index):
    from dataclasses import replace

    body, receipt = fixture()
    receipt.update(index_id=index, url=receipt["url"].replace("BRTI", index))
    raw, rr = encode(body), encode(coverage_receipt(body, receipt))
    t = target()
    t = replace(t, symbol=symbol, rules=replace(t.rules, index_id=index))
    result = estimate_cf_process(
        raw, rr, source_sha256=digest(raw), receipt_sha256=digest(rr), target=t, as_of=NOW
    )
    assert result.process.symbol == symbol
    assert result.process.index_id == index
    assert result.evidence["recorded_at"] is None
    assert result.evidence["received_at"] == NOW.isoformat()


@pytest.mark.parametrize(
    "key,value",
    [
        ("observations", 3599),
        ("duplicates", False),
        ("monotonic", 1),
        ("terminal", 1),
        ("step_counts_ms", {"1000": 3598}),
        ("first_timestamp", NOW.isoformat()),
    ],
)
def test_coverage_receipt_claims_replayed_not_trusted(key, value):
    body, receipt = fixture()
    r = coverage_receipt(body, receipt)
    r[key] = value
    raw, rr = encode(body), encode(r)
    with pytest.raises(ValueError, match="COVERAGE"):
        estimate_cf_process(
            raw,
            rr,
            source_sha256=digest(raw),
            receipt_sha256=digest(rr),
            target=target(),
            as_of=NOW,
        )


def test_fractional_or_overflow_json_metadata_refused_cleanly():
    body, receipt = fixture()
    raw = encode(body)
    receipt["source_sha256"] = digest(raw)
    for replacement in (b"200.0", b"1e999"):
        rr = encode(receipt).replace(b'"http_status":200', b'"http_status":' + replacement)
        with pytest.raises(ValueError, match="NONFINITE_JSON"):
            estimate_cf_process(
                raw,
                rr,
                source_sha256=digest(raw),
                receipt_sha256=digest(rr),
                target=target(),
                as_of=NOW,
            )
