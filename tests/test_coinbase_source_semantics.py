"""Synthetic Coinbase clock/original bindings; no market/model certification."""

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from kalshi_predictor.overnight_paper import crypto_source as cs
from kalshi_predictor.overnight_paper.provenance import canonical_hash, validate_source_visibility
from kalshi_predictor.overnight_paper.qualification import EvidenceReference, GateEvidence

NOW = datetime(2026, 9, 8, 1, tzinfo=UTC)
IDENTITY = dict(
    ticker="KXBTCD-26SEP0802-T70000",
    event_id="KXBTCD-26SEP0802",
    series="KXBTCD",
    category="Crypto",
)


def capture(body, url, params):
    raw = json.dumps(body).encode()
    receipt = dict(
        url=url,
        params=params,
        received_at=NOW.isoformat(),
        method="GET",
        status=200,
        sha256=hashlib.sha256(raw).hexdigest(),
    )
    return raw, receipt


def source():
    ticker, tr = capture(dict(time=NOW.isoformat(), price="70000.01"), cs.TICKER_URL, {})
    rows = [[int(NOW.timestamp()) - 60 * i, 69999, 70001, 70000, 70000, 2] for i in (1, 2, 3)]
    candles, cr = capture(rows, cs.CANDLES_URL, {"granularity": 60})
    return cs.build_coinbase_source(
        ticker_payload=ticker,
        ticker_receipt=tr,
        candle_payload=candles,
        candle_receipt=cr,
        decision_at=NOW,
    )


def mutate_original(value, name, transform):
    original = value["body"][name]
    body = json.loads(bytes.fromhex(original["payload_hex"]))
    body = transform(body)
    raw = json.dumps(body).encode()
    original.update(payload_hex=raw.hex(), sha256=hashlib.sha256(raw).hexdigest())


def test_real_original_builder_and_provenance_clock_basis():
    value = source()
    validate_source_visibility(value, decision_at=NOW, now=NOW)
    checked = cs.verify_coinbase_source(value, decision_at=NOW, now=NOW)
    assert checked["inputs"]["spot"] == "70000.01"
    assert value["provider_generated_at"] is value["provider_updated_at"] is None
    assert value["settlement_truth"] is False


@pytest.mark.parametrize(
    "change,reason",
    [
        (lambda s: s.update(provider_generated_at=NOW.isoformat()), "INVENTED"),
        (lambda s: s.update(product_id="ETH-USD"), "SCOPE"),
        (lambda s: s["body"]["ticker"].update(url=cs.TICKER_URL + "?other=1"), "ENDPOINT"),
        (lambda s: s["body"]["candles"].update(params={"granularity": 300}), "ENDPOINT"),
        (lambda s: s["body"]["ticker"].update(sha256="0" * 64), "HASH"),
        (
            lambda s: s["body"]["ticker"].update(
                received_at=(NOW + timedelta(seconds=1)).isoformat()
            ),
            "RECEIPT",
        ),
        (lambda s: mutate_original(s, "ticker", lambda b: {"price": b["price"]}), None),
        (lambda s: mutate_original(s, "ticker", lambda b: b | {"price": "NaN"}), "NUMBER"),
        (lambda s: mutate_original(s, "candles", lambda b: b + [b[0]]), "DUPLICATE_CANDLE"),
        (lambda s: mutate_original(s, "candles", lambda b: b[:-1]), "CANDLE_COUNT"),
    ],
)
def test_invalid_originals_fail_closed(change, reason):
    value = source()
    change(value)
    with pytest.raises((ValueError, KeyError), match=reason):
        cs.verify_coinbase_source(value, decision_at=NOW, now=NOW)


def test_trade_age_rechecked_without_refreshing_receipt_or_quote():
    value = source()
    assert cs.verify_coinbase_source(value, decision_at=NOW, now=NOW + timedelta(seconds=60))
    with pytest.raises(ValueError, match="STALE"):
        cs.verify_coinbase_source(
            value, decision_at=NOW, now=NOW + timedelta(seconds=60, microseconds=1)
        )


def test_open_candle_excluded_and_history_never_filled():
    value = source()
    mutate_original(
        value,
        "candles",
        lambda rows: [[int(NOW.timestamp()), 69999, 70001, 70000, 70000, 1]] + rows,
    )
    view = cs.verify_coinbase_source(value, decision_at=NOW, now=NOW)["inputs"]
    assert len(view["closed_candles"]) == 3
    assert view["excluded_unclosed_row_indices"] == [0]
    mutate_original(
        value,
        "candles",
        lambda rows: (
            [rows[0]] + rows[1:3] + [[int(NOW.timestamp()) - 300, 69999, 70001, 70000, 70000, 1]]
        ),
    )
    assert (
        cs.verify_coinbase_source(value, decision_at=NOW, now=NOW)["inputs"]["missing_minutes"] == 2
    )


def complete_coinbase_inputs():
    from test_overnight_provenance import artifact
    from test_paper_release_provenance import complete_inputs

    args = complete_inputs()
    context = args["context"]
    decision = args["decision"]
    value = source()
    source_artifact = artifact(value)
    market = dict(
        ticker=IDENTITY["ticker"],
        event_ticker=IDENTITY["event_id"],
        status="active",
        close_time="2026-09-08T02:00:00Z",
    )
    market_source = artifact(
        dict(
            url="https://external-api.kalshi.com/trade-api/v2/markets/" + IDENTITY["ticker"],
            body={"market": market},
            clock_basis="public_rest_receipt",
            provider_generated_at=None,
            provider_updated_at=None,
            received_at=NOW.isoformat(),
            available_at=NOW.isoformat(),
        )
    )
    sources = (source_artifact, market_source)
    hashes = [s.sha256 for s in sources]
    checked = cs.verify_coinbase_source(value, decision_at=NOW, now=NOW)
    feature = cs.coinbase_feature_record(value, decision_at=NOW, now=NOW)
    feature_payload = (
        context.features_artifact.decode()
        | IDENTITY
        | {"source_hashes": hashes, "records": [feature]}
    )
    features = artifact(feature_payload)
    rows = {key: item.decode() for key, item in context.artifacts.items()}
    for role in ("forecast", "snapshot"):
        rows[role].update(IDENTITY)
    rows["forecast"].update(
        source_hashes=hashes,
        coinbase_input_sha256=checked["input_sha256"],
        features_artifact_sha256=features.sha256,
    )
    artifacts = {key: artifact(row) for key, row in rows.items()}
    decision.update(
        IDENTITY,
        source_hashes=hashes,
        coinbase_input_sha256=checked["input_sha256"],
        features_artifact_sha256=features.sha256,
    )
    decision.update({key + "_artifact_sha256": item.sha256 for key, item in artifacts.items()})
    decision["source_timestamps"] = [
        {
            "sha256": item.sha256,
            **{
                key: item.decode()[key]
                for key in (
                    "provider_updated_at",
                    "provider_generated_at",
                    "available_at",
                    "received_at",
                    "clock_basis",
                )
            },
        }
        for item in sources
    ]
    decision["feature_timestamps"] = [
        {key: feature[key] for key in ("name", "source_sha256", "observed_at", "available_at")}
    ]
    args["context"] = replace(
        context, artifacts=artifacts, source_artifacts=sources, features_artifact=features
    )
    args["decision_id"] = canonical_hash(decision)
    return args


def gate(args, gate_number=4):
    sources = tuple(
        EvidenceReference("source", s.sha256, s.payload) for s in args["context"].source_artifacts
    )
    decision = args["decision"]
    report = dict(
        schema="overnight-paper-gate-v1",
        gate=gate_number,
        decision_id=canonical_hash(decision),
        ticker=decision["ticker"],
        category="Crypto",
        verifier=cs.VERIFIER,
        verdict="PASS",
        sources=[s.sha256 for s in sources],
        validated_at=NOW.isoformat(),
        valid_until=(NOW + timedelta(seconds=61)).isoformat(),
    )
    raw = json.dumps(report).encode()
    return GateEvidence(
        gate_number,
        canonical_hash(decision),
        "Crypto",
        decision["ticker"],
        cs.VERIFIER,
        EvidenceReference("report", hashlib.sha256(raw).hexdigest(), raw),
        sources=sources,
    )


def test_actual_gate4_and_full_forecast_feature_binding_path():
    from kalshi_predictor.overnight_paper.provenance_gate import verify_complete_provenance

    args = complete_coinbase_inputs()
    assert gate(args).verified(args["decision"])
    checked = verify_complete_provenance(**args)
    assert checked.passed, checked.blockers
    assert not checked.model_calibration_verified
    assert not gate(args, 3).verified(args["decision"])
    assert not gate(args, 9).verified(args["decision"])
    assert not gate(args).verified(args["decision"], as_of=NOW + timedelta(seconds=61))


def test_mutated_feature_cannot_hide_behind_rebound_hashes():
    from test_overnight_provenance import artifact

    from kalshi_predictor.overnight_paper.provenance_gate import verify_complete_provenance

    args = complete_coinbase_inputs()
    context = args["context"]
    changed = context.features_artifact.decode()
    changed["records"][0]["value"]["spot"] = "1"
    features = artifact(changed)
    forecast = context.artifacts["forecast"].decode()
    forecast["features_artifact_sha256"] = features.sha256
    artifacts = context.artifacts | {"forecast": artifact(forecast)}
    args["context"] = replace(context, features_artifact=features, artifacts=artifacts)
    args["decision"].update(
        features_artifact_sha256=features.sha256,
        forecast_artifact_sha256=artifacts["forecast"].sha256,
    )
    args["decision_id"] = canonical_hash(args["decision"])
    result = verify_complete_provenance(**args)
    assert not result.passed and "COINBASE_FEATURE_ORIGINAL_BINDING" in result.blockers


def test_source_pass_does_not_create_rule_fee_or_model_policy():
    from kalshi_predictor.overnight_paper.model_release import verify_model_release
    from kalshi_predictor.overnight_paper.rule_verifier import CERTIFIED_RULE_POLICIES
    from kalshi_predictor.paper.fees import decision_fee_quote

    assert CERTIFIED_RULE_POLICIES == ()
    with pytest.raises(ValueError, match="FEE_EVIDENCE_REQUIRED"):
        decision_fee_quote(
            {},
            ticker=IDENTITY["ticker"],
            side="BUY_YES",
            quantity=1,
            price=__import__("decimal").Decimal(".5"),
            simulator_floor=__import__("decimal").Decimal("0"),
            now=NOW,
            required=True,
        )

    class NoTransaction:
        def in_transaction(self):
            return False

    result = verify_model_release(NoTransaction(), {}, NOW)
    assert not result.passed and not result.model_calibration_verified


def test_weather_registered_path_unchanged():
    from test_overnight_qualification import structured_gate

    item, inputs = structured_gate(4)
    assert item.verified(inputs)


def test_actual_collector_probability_and_forecast_artifact_bridge(tmp_path, monkeypatch):
    from kalshi_predictor.overnight_paper import discovery

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    monkeypatch.setattr(discovery, "datetime", Clock)
    bundle = source()
    mutate_original(
        bundle,
        "candles",
        lambda rows: [
            row[:4] + [close, row[5]]
            for row, close in zip(rows, (69999, 70001, 70000), strict=True)
        ],
    )

    class ArchivedPublic:
        receipts = []

        def __init__(self):
            self.receipts = []

        def get(self, path, params=None):
            name = "candles" if path.endswith("/candles") else "ticker"
            item = bundle["body"][name]
            raw = bytes.fromhex(item["payload_hex"])
            file = tmp_path / (name + ".json")
            file.write_bytes(raw)
            self.receipts.append(
                {
                    key: item[key]
                    for key in ("url", "params", "method", "status", "received_at", "sha256")
                }
                | {"path": str(file)}
            )
            return json.loads(raw)

    collected = discovery.collect_crypto_source(ArchivedPublic(), "BTC")
    monkeypatch.setattr(discovery, "datetime", datetime)
    assert collected["closed_candle_count"] == 3
    assert collected["analytical_inputs"] == cs.verify_coinbase_source(
        collected["analytical_source"], decision_at=NOW, now=NOW
    )
    row = {
        "raw_market": {
            "close_time": "2026-09-08T01:05:00Z",
            "strike_type": "greater",
            "floor_strike": 70000,
        },
        "crypto_terms": {"status": "EXACT_LINK"},
        "semantic_conflicts": [],
        "first_blocker": "SETTLEMENT_RULE_UNCERTIFIED",
        "paper_readiness": "PAPER_NOT_READY",
    }
    discovery.add_crypto_research(row, collected, NOW)
    assert row["forecast"] is not None, row.get("forecast_diagnostic_blocker")
    forecast = row["research_forecast_artifact"]
    assert forecast["probability"] == str(row["forecast"])
    assert forecast["coinbase_input_sha256"] == collected["analytical_inputs"]["input_sha256"]
    assert forecast["generated_at"] == NOW.isoformat()
    assert forecast["source_hashes"] == [canonical_hash(collected["analytical_source"])]
    assert row["paper_readiness"] == "PAPER_NOT_READY"
    collected["features"]["return_1h"] = "1"
    discovery.add_crypto_research(row, collected, NOW)
    assert row["forecast"] is None
    assert "ORIGINAL_INPUT_MISMATCH" in row["forecast_diagnostic_blocker"]


@pytest.mark.parametrize(
    "raw",
    [
        b'{"time":"2026-09-08T01:00:00Z","price":"1","price":"2"}',
        b'{"time":"2026-09-08T01:00:00Z","price":NaN}',
    ],
)
def test_duplicate_json_and_nonfinite_tokens_rejected(raw):
    value = source()
    value["body"]["ticker"].update(payload_hex=raw.hex(), sha256=hashlib.sha256(raw).hexdigest())
    with pytest.raises(ValueError, match="DUPLICATE_JSON_KEY|NONFINITE_JSON"):
        cs.verify_coinbase_source(value, decision_at=NOW, now=NOW)


@pytest.mark.parametrize("clock", ["2026-09-08T01:00:00", "not-a-clock", "2026-09-08T01:00:01Z"])
def test_unsupported_trade_clocks(clock):
    value = source()
    mutate_original(value, "ticker", lambda row: row | {"time": clock})
    with pytest.raises(ValueError):
        cs.verify_coinbase_source(value, decision_at=NOW, now=NOW)


def test_original_size_and_row_count_limits():
    value = source()
    value["body"]["ticker"]["payload_hex"] = "00" * (cs.MAX_ORIGINAL_BYTES + 1)
    with pytest.raises(ValueError, match="SIZE"):
        cs.verify_coinbase_source(value, decision_at=NOW, now=NOW)
    value = source()
    mutate_original(value, "candles", lambda rows: rows * 101)
    with pytest.raises(ValueError, match="COUNT"):
        cs.verify_coinbase_source(value, decision_at=NOW, now=NOW)


def test_current_market_expiry_and_forecast_hash_mismatch():
    from test_overnight_provenance import artifact

    from kalshi_predictor.overnight_paper.provenance_gate import verify_complete_provenance

    args = complete_coinbase_inputs()
    context = args["context"]
    forecast = context.artifacts["forecast"].decode()
    forecast["coinbase_input_sha256"] = "0" * 64
    artifacts = context.artifacts | {"forecast": artifact(forecast)}
    args["context"] = replace(context, artifacts=artifacts)
    args["decision"]["forecast_artifact_sha256"] = artifacts["forecast"].sha256
    args["decision_id"] = canonical_hash(args["decision"])
    result = verify_complete_provenance(**args)
    assert not result.passed and "COINBASE_FORECAST_INPUT_BINDING" in result.blockers
    args = complete_coinbase_inputs()
    sources = list(args["context"].source_artifacts)
    market = sources[1].decode()
    market["body"]["market"]["close_time"] = NOW.isoformat()
    sources[1] = artifact(market)
    hashes = [s.sha256 for s in sources]
    args["context"] = replace(args["context"], source_artifacts=tuple(sources))
    args["decision"]["source_hashes"] = hashes
    assert not gate(args).verified(args["decision"])


@pytest.mark.parametrize("duplicate", [False, True])
def test_each_coinbase_source_requires_exactly_one_feature_record(duplicate):
    from test_overnight_provenance import artifact

    from kalshi_predictor.overnight_paper.provenance_gate import verify_complete_provenance

    args = complete_coinbase_inputs()
    context = args["context"]
    changed = context.features_artifact.decode()
    record = changed["records"][0]
    changed["records"] = (
        [record, dict(record)]
        if duplicate
        else [
            dict(
                name="fixture",
                value=0,
                source_sha256=context.source_artifacts[1].sha256,
                observed_at=NOW.isoformat(),
                available_at=NOW.isoformat(),
            )
        ]
    )
    features = artifact(changed)
    forecast = context.artifacts["forecast"].decode()
    forecast["features_artifact_sha256"] = features.sha256
    artifacts = context.artifacts | {"forecast": artifact(forecast)}
    args["context"] = replace(context, features_artifact=features, artifacts=artifacts)
    args["decision"].update(
        features_artifact_sha256=features.sha256,
        forecast_artifact_sha256=artifacts["forecast"].sha256,
    )
    args["decision"]["feature_timestamps"] = [
        {key: r[key] for key in ("name", "source_sha256", "observed_at", "available_at")}
        for r in changed["records"]
    ]
    args["decision_id"] = canonical_hash(args["decision"])
    result = verify_complete_provenance(**args)
    assert not result.passed and "COINBASE_FEATURE_RECORD_REQUIRED" in result.blockers


def test_nonfinite_unused_ticker_field_rejected():
    raw = b'{"time":"2026-09-08T01:00:00Z","price":"70000.01","unused":Infinity}'
    value = source()
    value["body"]["ticker"].update(payload_hex=raw.hex(), sha256=hashlib.sha256(raw).hexdigest())
    with pytest.raises(ValueError, match="NONFINITE_JSON"):
        cs.verify_coinbase_source(value, decision_at=NOW, now=NOW)
