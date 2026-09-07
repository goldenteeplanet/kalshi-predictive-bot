from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from unittest.mock import ANY

import pytest


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _module():
    return _load(
        "phase4ag_tested",
        Path(__file__).parents[1] / "scripts/local/phase4ag_exchange_timestamp_collector.py",
    )


def _af_helper():
    return _load(
        "phase4af_fixture_for_4ag",
        Path(__file__).with_name("test_phase4af_settlement_timestamp_audit.py"),
    )


def _fixture(tmp_path: Path):
    helper = _af_helper()
    af_fixture = helper._fixture(tmp_path)
    af = helper._audit(af_fixture)
    af_path = tmp_path / "phase4af.json"
    af_path.write_text(json.dumps(af), encoding="utf-8")
    return (
        _module(),
        af_fixture[1],
        af_fixture[2],
        af_fixture[3],
        af_fixture[4],
        af_path,
        af_fixture[5],
    )


def _archive(module, directory: Path, response: dict, ticker: str = "KXTEST-1"):
    directory.mkdir(exist_ok=True)
    payload = {
        "schema": module.ARCHIVE_SCHEMA,
        "requested_ticker": ticker,
        "retrieved_at": "2026-08-25T19:05:00Z",
        "response": response,
    }
    payload["artifact_hash"] = module.artifact_hash(payload)
    (directory / f"{ticker}.json").write_text(json.dumps(payload), encoding="utf-8")


def _collect(fixture, **overrides):
    module, db, history, ad, ae, af, now = fixture
    return module.collect(
        db,
        af,
        ad,
        ae,
        history,
        now=now,
        mode=overrides.get("mode", "offline"),
        archive_dir=overrides.get("archive_dir"),
        api_base_url=overrides.get("api_base_url"),
        allowed_host=overrides.get("allowed_host"),
        maximum_retries=overrides.get("maximum_retries", 1),
        maximum_response_bytes=overrides.get("maximum_response_bytes", 1_000_000),
        transport=overrides.get("transport"),
    )


def test_valid_archive_produces_phase4af_evidence_and_preserves_database(tmp_path: Path):
    fixture = _fixture(tmp_path)
    module, database = fixture[0], fixture[1]
    archive_dir = tmp_path / "archives"
    _archive(
        module,
        archive_dir,
        {
            "market": {
                "ticker": "KXTEST-1",
                "result": "yes",
                "settlement_ts": "2026-08-25T19:00:00Z",
            }
        },
    )
    before = database.read_bytes()
    status, evidence = _collect(fixture, archive_dir=archive_dir)
    assert status["disposition_counts"] == {"ARCHIVED_EXCHANGE_EVIDENCE_FOUND": 1}
    assert status["request_count"] == 0
    assert evidence["rows"] == [
        {
            "ticker": "KXTEST-1",
            "settlement_timestamp": "2026-08-25T19:00:00+00:00",
            "source_record_identity": ANY,
        }
    ]
    assert database.read_bytes() == before


@pytest.mark.parametrize(
    ("market", "expected"),
    [
        (
            {"ticker": "OTHER", "result": "yes", "settlement_ts": "2026-08-25T19:00:00Z"},
            "MARKET_IDENTITY_MISMATCH",
        ),
        (
            {"ticker": "KXTEST-1", "result": "no", "settlement_ts": "2026-08-25T19:00:00Z"},
            "EXCHANGE_RESULT_CONFLICT",
        ),
        ({"ticker": "KXTEST-1", "result": "yes"}, "EXCHANGE_TIMESTAMP_MISSING"),
        (
            {"ticker": "KXTEST-1", "result": "yes", "close_time": "2026-08-25T19:00:00Z"},
            "EXCHANGE_TIMESTAMP_MISSING",
        ),
        (
            {"ticker": "KXTEST-1", "result": "yes", "settlement_ts": "2026-08-25 19:00:00"},
            "EXCHANGE_TIMESTAMP_TIMEZONE_AMBIGUOUS",
        ),
        (
            {"ticker": "KXTEST-1", "result": "yes", "settlement_ts": "bad"},
            "EXCHANGE_PAYLOAD_INVALID",
        ),
    ],
)
def test_payload_fail_closed_dispositions(market, expected):
    result = _module().inspect_exchange_payload(
        "KXTEST-1", "yes", [{"market": market}], source_kind="archive"
    )
    assert result["disposition"] == expected


def test_equivalent_timestamps_do_not_conflict():
    responses = [
        {
            "market": {
                "ticker": "KXTEST-1",
                "result": "yes",
                "settlement_ts": "2026-08-25T19:00:00Z",
            }
        },
        {
            "market": {
                "ticker": "KXTEST-1",
                "result": "yes",
                "settled_at": "2026-08-25T14:00:00-05:00",
            }
        },
    ]
    result = _module().inspect_exchange_payload("KXTEST-1", "yes", responses, source_kind="archive")
    assert result["disposition"] == "ARCHIVED_EXCHANGE_EVIDENCE_FOUND"


def test_conflicting_timestamps_block():
    responses = [
        {
            "market": {
                "ticker": "KXTEST-1",
                "result": "yes",
                "settlement_ts": "2026-08-25T19:00:00Z",
            }
        },
        {"market": {"ticker": "KXTEST-1", "result": "yes", "settled_at": "2026-08-25T19:01:00Z"}},
    ]
    result = _module().inspect_exchange_payload("KXTEST-1", "yes", responses, source_kind="archive")
    assert result["disposition"] == "EXCHANGE_TIMESTAMP_CONFLICT"


def test_offline_missing_archive_is_blocked(tmp_path: Path):
    status, evidence = _collect(_fixture(tmp_path))
    assert status["disposition_counts"] == {"SOURCE_ARCHIVE_MISSING": 1}
    assert status["safe_for_phase4af_reaudit"] is False
    assert evidence["rows"] == []


def test_tampered_archive_rejected(tmp_path: Path):
    fixture = _fixture(tmp_path)
    directory = tmp_path / "archives"
    _archive(fixture[0], directory, {"market": {"ticker": "KXTEST-1"}})
    path = next(directory.glob("*.json"))
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["retrieved_at"] = "changed"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="ARCHIVE_HASH_MISMATCH"):
        _collect(fixture, archive_dir=directory)


def test_fake_read_only_api_and_exact_endpoint(tmp_path: Path):
    fixture = _fixture(tmp_path)
    calls = []

    def transport(url, connect, response, maximum):
        calls.append((url, connect, response, maximum))
        body = json.dumps(
            {
                "market": {
                    "ticker": "KXTEST-1",
                    "result": "yes",
                    "settlement_ts": "2026-08-25T19:00:00Z",
                }
            }
        ).encode()
        return 200, url, body

    status, evidence = _collect(
        fixture,
        mode="bounded-read-only-api",
        api_base_url="https://api.elections.kalshi.com",
        allowed_host="api.elections.kalshi.com",
        transport=transport,
    )
    assert status["disposition_counts"] == {"DIRECT_EXCHANGE_EVIDENCE_FOUND": 1}
    assert status["request_count"] == 1
    assert calls[0][0].endswith("/trade-api/v2/markets/KXTEST-1")
    assert len(evidence["rows"]) == 1


@pytest.mark.parametrize(
    ("status_code", "expected", "requests"),
    [
        (401, "READ_ONLY_REQUEST_NOT_AUTHORIZED", 1),
        (403, "READ_ONLY_REQUEST_NOT_AUTHORIZED", 1),
        (500, "HTTP_SOURCE_UNAVAILABLE", 2),
    ],
)
def test_api_failures_are_bounded(tmp_path: Path, status_code, expected, requests):
    fixture = _fixture(tmp_path)

    def transport(url, connect, response, maximum):
        return status_code, url, b"{}"

    status, _ = _collect(
        fixture,
        mode="bounded-read-only-api",
        api_base_url="https://api.elections.kalshi.com",
        allowed_host="api.elections.kalshi.com",
        transport=transport,
    )
    assert status["disposition_counts"] == {expected: 1}
    assert status["request_count"] == requests


def test_redirect_host_and_response_size_rejected(tmp_path: Path):
    fixture = _fixture(tmp_path)

    def redirect(url, connect, response, maximum):
        return 200, "https://evil.example/market", b"{}"

    status, _ = _collect(
        fixture,
        mode="bounded-read-only-api",
        api_base_url="https://api.elections.kalshi.com",
        allowed_host="api.elections.kalshi.com",
        transport=redirect,
    )
    assert status["disposition_counts"] == {"HTTP_SOURCE_UNAVAILABLE": 1}


def test_unsafe_api_configuration_refused(tmp_path: Path):
    fixture = _fixture(tmp_path)
    with pytest.raises(ValueError, match="BASE_URL_UNSAFE"):
        _collect(
            fixture,
            mode="bounded-read-only-api",
            api_base_url="http://api.elections.kalshi.com",
            allowed_host="api.elections.kalshi.com",
            transport=lambda *args: None,
        )


def test_archive_precedence_makes_no_api_request(tmp_path: Path):
    fixture = _fixture(tmp_path)
    archive_dir = tmp_path / "archives"
    _archive(
        fixture[0],
        archive_dir,
        {
            "market": {
                "ticker": "KXTEST-1",
                "result": "yes",
                "settlement_ts": "2026-08-25T19:00:00Z",
            }
        },
    )

    def forbidden(*args):
        raise AssertionError("transport must not be called")

    status, _ = _collect(
        fixture,
        mode="bounded-read-only-api",
        archive_dir=archive_dir,
        api_base_url="https://api.elections.kalshi.com",
        allowed_host="api.elections.kalshi.com",
        transport=forbidden,
    )
    assert status["request_count"] == 0


def test_tampered_phase4af_and_history_rejected(tmp_path: Path):
    fixture = _fixture(tmp_path)
    af_path = fixture[5]
    payload = json.loads(af_path.read_text(encoding="utf-8"))
    payload["ready_count"] = 99
    af_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="PHASE4AF_HASH_MISMATCH"):
        _collect(fixture)


def test_atomic_pair_publication_refusal_and_replace(tmp_path: Path):
    fixture = _fixture(tmp_path)
    module = fixture[0]
    status, evidence = _collect(fixture)
    status_path, evidence_path = tmp_path / "status.json", tmp_path / "evidence.json"
    module.publish_pair(status_path, evidence_path, status, evidence)
    with pytest.raises(FileExistsError):
        module.publish_pair(status_path, evidence_path, status, evidence)
    module.publish_pair(status_path, evidence_path, status, evidence, replace=True)
    assert not list(tmp_path.glob(".*.tmp"))
    assert json.loads(status_path.read_text(encoding="utf-8"))["pair_id"] == evidence["pair_id"]


def test_empty_eligible_cohort_is_deterministic_and_fail_closed(tmp_path: Path):
    fixture = _fixture(tmp_path)
    module, af_path = fixture[0], fixture[5]
    af = json.loads(af_path.read_text(encoding="utf-8"))
    for row in af["rows"]:
        row["classification"] = "AUDIT_BLOCKED"
        row["row_hash"] = module.canonical_hash(
            {key: value for key, value in row.items() if key != "row_hash"}
        )
    af["rows_hash"] = module.canonical_hash(af["rows"])
    af["artifact_hash"] = module.artifact_hash(af)
    af_path.write_text(json.dumps(af), encoding="utf-8")
    first = _collect(fixture)
    second = _collect(fixture)
    assert first == second
    assert first[0]["eligible_ticker_count"] == 0
    assert first[0]["safe_for_phase4af_reaudit"] is False
