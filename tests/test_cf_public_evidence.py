"""Credential-free parser fixtures and original archived CF numerical regression."""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from kalshi_predictor.overnight_paper.cf_public_evidence import extract_public_brti

END = datetime(2026, 9, 8, 7, 45, tzinfo=UTC)


def html_for(points=None, *, identity="BRTI", extra=None):
    if points is None:
        points = [
            {"time": int((END - timedelta(seconds=60 - i)).timestamp() * 1000), "value": "78424.80"}
            for i in range(60)
        ]
    body = {
        "props": {
            "pageProps": {
                "externalIndexId": identity,
                "indexConfig": {"rtis": points},
                **(extra or {}),
            }
        }
    }
    return (
        '<html><script id="__NEXT_DATA__" type="application/json">'
        + json.dumps(body)
        + "</script></html>"
    ).encode()


def extract(raw=None, **changes):
    raw = raw or html_for()
    return extract_public_brti(
        **(
            {
                "html": raw,
                "expected_sha256": hashlib.sha256(raw).hexdigest(),
                "received_at": END,
                "target_end": END,
                "now": END,
            }
            | changes
        )
    )


def test_extract_exact_window_without_source_certification():
    evidence = extract()
    assert len(evidence.points) == 60
    assert evidence.points[0][0] == "2026-09-08T07:44:00+00:00"
    assert evidence.points[-1][0] == "2026-09-08T07:44:59+00:00"
    assert evidence.freshness_passed and evidence.provider_age_seconds == 1
    assert evidence.research_only and not evidence.runtime_certified


@pytest.mark.parametrize(
    "field,value",
    [
        ("value", "NaN"),
        ("value", "Infinity"),
        ("value", "-1.00"),
        ("value", "0.00"),
        ("value", "1.001"),
        ("value", 1.0),
        ("time", 1.5),
        ("time", True),
    ],
)
def test_invalid_values_and_timestamps_fail_closed(field, value):
    raw = html_for()
    body = json.loads(raw.split(b'">', 1)[1].split(b"</script>")[0])
    points = body["props"]["pageProps"]["indexConfig"]["rtis"]
    points[5][field] = value
    with pytest.raises(ValueError):
        extract(html_for(points))


@pytest.mark.parametrize("mutation", ["duplicate", "out_of_order", "subsecond", "gap"])
def test_only_complete_ordered_one_hz_data_is_accepted(mutation):
    raw = html_for()
    points = json.loads(raw.split(b'">', 1)[1].split(b"</script>")[0])["props"]["pageProps"][
        "indexConfig"
    ]["rtis"]
    if mutation == "duplicate":
        points[20] = points[19]
    elif mutation == "out_of_order":
        points[20], points[21] = points[21], points[20]
    elif mutation == "subsecond":
        points[20]["time"] += 200
    else:
        points.pop(20)
    with pytest.raises(ValueError):
        extract(html_for(points))


def test_missing_window_and_wrong_index_fail():
    with pytest.raises(ValueError, match="CF_TARGET_MINUTE_INCOMPLETE"):
        extract(target_end=END - timedelta(seconds=1))
    with pytest.raises(ValueError, match="CF_EXACT_BRTI_IDENTITY_REQUIRED"):
        extract(html_for(identity="ETHUSD_RTI"))


def test_original_payload_hash_is_checked():
    with pytest.raises(ValueError, match="CF_ORIGINAL_RESPONSE_HASH_MISMATCH"):
        extract(expected_sha256="0" * 64)


@pytest.mark.parametrize(
    "raw",
    [
        b"<html></html>",
        b'<script id="__NEXT_DATA__" type="application/json">{}',
        b'<script id="__NEXT_DATA__" type="application/json">{}</script>',
        b'<script id="__NEXT_DATA__" type="application/json">{"props":{},"props":{}}</script>',
    ],
)
def test_malformed_or_ambiguous_html_fails_without_echoing_payload(raw):
    with pytest.raises(ValueError, match="CF_"):
        extract(raw)


def test_unrelated_connection_fields_never_leave_parser():
    marker = "SYNTHETIC_SECRET_MARKER_NOT_A_REAL_CREDENTIAL"
    result = extract(html_for(extra={"wsApiKeyPassword": marker, "socketUrl": marker}))
    assert marker not in repr(result)
    assert "wsApiKey" not in repr(result)


def test_retrospective_window_remains_extractable_but_explicitly_stale():
    result = extract(received_at=END + timedelta(hours=2), now=END + timedelta(hours=2))
    assert not result.freshness_passed
    assert result.freshness_blockers == ("CF_PROVIDER_OBSERVATIONS_STALE", "CF_TARGET_WINDOW_STALE")
    assert not result.runtime_certified


def test_receipt_clock_and_unrelaxed_age_policy_are_enforced():
    with pytest.raises(ValueError, match="CF_RECEIPT_OR_TARGET_CLOCK_INVALID"):
        extract(now=END - timedelta(seconds=1))
    with pytest.raises(ValueError, match="CF_FRESHNESS_LIMIT_REFUSED"):
        extract(max_source_age_seconds=1801)
    assert extract(now=END + timedelta(seconds=61)).freshness_blockers == ("CF_RECEIPT_STALE",)


def test_original_archived_cf_points_reproduce_three_finalized_prices():
    from kalshi_predictor.overnight_paper.rule_verifier import evaluate_cf_minute

    fixture = json.loads(
        (Path(__file__).parent / "fixtures/paper_release_cf_examples.json").read_text(
            encoding="utf-8"
        )
    )
    assert (
        fixture["source_response_sha256"]
        == "14b47e1ad2df6dcc606c9e2efa5c1ae76df11272600679573f6d988fda7fbc33"
    )
    for window in [window for example in fixture["examples"] for window in example["windows"]]:
        boundary = datetime.fromisoformat(window["boundary"])
        points = [
            {
                "time": int(datetime.fromisoformat(point["timestamp"]).timestamp() * 1000),
                "value": point["value"],
            }
            for point in window["points"]
        ]
        evidence = extract(html_for(points), target_end=boundary)
        mean = sum((Decimal(value) for _, value in evidence.points), Decimal(0)) / 60
        assert mean.quantize(Decimal("0.01")) == Decimal(window["expected_value"])
        evaluated = evaluate_cf_minute(evidence.points, boundary=boundary, rounding="ROUND_HALF_UP")
        assert evaluated.rounded_value == Decimal(window["expected_value"])
