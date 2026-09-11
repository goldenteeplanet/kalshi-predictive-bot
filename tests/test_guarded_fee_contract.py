"""Synthetic policies exercise mechanics; no production fee certification."""

import hashlib
import json
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from kalshi_predictor.paper import fees

NOW = datetime(2026, 9, 9, 12, tzinfo=UTC)


def test_reviewed_0450_renewal_preserves_historical_policy_identity():
    old, renewed = fees.CERTIFIED_FEE_POLICIES
    assert old.version == "b5b74a7948384c1a6275a411eba52c37e6fb4608d10a1204b0f6f36def69cea9"
    assert renewed.version == "cf8a8559cc8598265bb7cec0d9c78ddf9945763a5f4ba324fbb18513fbeab8fb"
    assert renewed.policy_id == old.policy_id + "-0450"
    assert renewed.effective_from == "2026-09-11T04:50:00+00:00"
    assert renewed.effective_to == "2026-09-11T05:50:00+00:00"
    assert datetime.fromisoformat(old.effective_to) < datetime.fromisoformat(renewed.effective_from)
    assert replace(
        renewed,
        policy_id=old.policy_id,
        effective_from=old.effective_from,
        effective_to=old.effective_to,
    ) == old
    assert replace(renewed, effective_to=old.effective_to).version != renewed.version


@pytest.mark.parametrize(
    "index, at, window_valid",
    [
        (0, "2026-09-11T03:35:00+00:00", True),
        (0, "2026-09-11T04:23:28.844889+00:00", False),
        (0, "2026-09-11T04:50:00+00:00", False),
        (1, "2026-09-11T04:49:59.999999+00:00", False),
        (1, "2026-09-11T04:50:00+00:00", True),
        (1, "2026-09-11T05:49:59.999999+00:00", True),
        (1, "2026-09-11T05:50:00+00:00", False),
    ],
)
def test_registered_policy_windows_do_not_bypass_original_evidence(index, at, window_valid):
    # The actual registry resolves the version/window, then still requires originals.
    # No fabricated authoritative document bytes or successful fee quote are supplied.
    evidence = {"policy_version": fees.CERTIFIED_FEE_POLICIES[index].version, "documents": []}
    error = "FEE_ORIGINAL_DOCUMENTS_REQUIRED" if window_valid else "FEE_POLICY_NOT_EFFECTIVE"
    with pytest.raises(ValueError, match=error):
        fees.build_fee_quote(
            evidence=evidence,
            ticker="KXTEMPMIAH-RESEARCH",
            side="BUY_YES",
            price=Decimal(".30"),
            simulator_floor=Decimal("0"),
            now=datetime.fromisoformat(at),
        )


def synthetic_evidence(monkeypatch, now=NOW, ticker="M"):
    original = b"SYNTHETIC reviewed rate .07, no settlement fee; not official evidence"
    sha = hashlib.sha256(original).hexdigest()
    policy = fees.CertifiedFeePolicy(
        "synthetic-only",
        (now - timedelta(days=1)).isoformat(),
        (now + timedelta(days=1)).isoformat(),
        "0.07",
        ((fees.ROUNDING_URL, sha),),
        sha,
        sha,
        sha,
        "0",
    )
    monkeypatch.setattr(fees, "CERTIFIED_FEE_POLICIES", (policy,))
    bodies = {
        f"/markets/{ticker}": {
            "market": {
                "ticker": ticker,
                "event_ticker": "E",
                "status": "open",
                "close_time": (now + timedelta(hours=1)).isoformat(),
            }
        },
        "/events/E": {
            "event": {
                "event_ticker": "E",
                "series_ticker": "S",
                "fee_type_override": None,
                "fee_multiplier_override": None,
            }
        },
        "/series/S": {"series": {"ticker": "S", "fee_type": "quadratic", "fee_multiplier": "1"}},
    }
    captures = []
    for path, body in bodies.items():
        raw = json.dumps(
            {"url": fees.PUBLIC_BASE + path, "received_at": now.isoformat(), "body": body}
        ).encode()
        captures.append({"payload_hex": raw.hex(), "sha256": hashlib.sha256(raw).hexdigest()})
    return {
        "policy_version": policy.version,
        "documents": [
            {
                "url": fees.ROUNDING_URL,
                "received_at": now.isoformat(),
                "payload_hex": original.hex(),
            }
        ],
        "captures": captures,
    }


@pytest.fixture
def evidence(monkeypatch):
    return synthetic_evidence(monkeypatch)


def quote(evidence, floor="0"):
    return fees.build_fee_quote(
        evidence=evidence,
        ticker="M",
        side="BUY_YES",
        price=Decimal(".30"),
        simulator_floor=Decimal(floor),
        now=NOW,
    )


def test_charge_floor_and_exact_cost(evidence):
    q = quote(evidence)
    assert q.charge == Decimal(".02")
    assert q.decode()["fee_decomposition"]["trade_fee"] == "0.014700"
    assert Decimal(1) - Decimal(".30") - q.charge == Decimal(".68")
    assert -Decimal(".30") - q.charge == Decimal("-.32")
    assert quote(evidence, ".03").charge == Decimal(".03")


def test_no_production_policy(evidence, monkeypatch):
    monkeypatch.setattr(fees, "CERTIFIED_FEE_POLICIES", ())
    with pytest.raises(ValueError, match="REVIEWED_FEE_POLICY_REQUIRED"):
        quote(evidence)


def test_series_scope_is_enforced_and_bound_to_policy(evidence, monkeypatch):
    legacy = fees.CERTIFIED_FEE_POLICIES[0]
    allowed = replace(legacy, permitted_series=("S",))
    denied = replace(legacy, permitted_series=("SOTHER",))
    assert len({legacy.version, allowed.version, denied.version}) == 3
    monkeypatch.setattr(fees, "CERTIFIED_FEE_POLICIES", (allowed,))
    # Existing evidence cannot silently gain a new policy's authority.
    with pytest.raises(ValueError, match="REVIEWED_FEE_POLICY_REQUIRED"):
        quote(evidence)
    evidence["policy_version"] = allowed.version
    assert quote(evidence).charge == Decimal(".02")
    monkeypatch.setattr(fees, "CERTIFIED_FEE_POLICIES", (denied,))
    evidence["policy_version"] = denied.version
    with pytest.raises(ValueError, match="FEE_SERIES_OUTSIDE_REVIEWED_SCOPE"):
        quote(evidence)


@pytest.mark.parametrize("scope", [(), [], ("S", "S"), ("Z", "S"), ("s",), ("S*",), (1,)])
def test_series_scope_rejects_ambiguous_or_mutable_values(evidence, scope):
    with pytest.raises(ValueError, match="FEE_EXACT_SERIES_SCOPE_REQUIRED"):
        replace(fees.CERTIFIED_FEE_POLICIES[0], permitted_series=scope)


def test_absent_scope_preserves_both_previous_policy_hash_profiles(evidence):
    legacy = fees.CERTIFIED_FEE_POLICIES[0]
    optional = replace(
        legacy,
        event_override_interpretation=fees.OPTIONAL_EVENT_OVERRIDE_PROFILE,
        event_schema_document_sha256=fees.EVENT_DATA_SCHEMA_SHA256,
    )
    for policy in (legacy, optional):
        old_fields = asdict(policy)
        old_fields.pop("permitted_series")
        if policy is legacy:
            old_fields.pop("event_override_interpretation")
            old_fields.pop("event_schema_document_sha256")
        expected = hashlib.sha256(
            json.dumps(old_fields, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        ).hexdigest()
        assert policy.version == expected


def test_missing_override_is_unknown(evidence):
    capture = evidence["captures"][1]
    row = json.loads(bytes.fromhex(capture["payload_hex"]))
    del row["body"]["event"]["fee_multiplier_override"]
    raw = json.dumps(row).encode()
    capture.update(payload_hex=raw.hex(), sha256=hashlib.sha256(raw).hexdigest())
    with pytest.raises(ValueError, match="OVERRIDE_INVALID"):
        quote(evidence)


@pytest.mark.parametrize("claimed_series", ["S", "SOTHER", None])
def test_explicit_market_series_must_agree_with_event(evidence, claimed_series):
    capture = evidence["captures"][0]
    row = json.loads(bytes.fromhex(capture["payload_hex"]))
    row["body"]["market"]["series_ticker"] = claimed_series
    raw = json.dumps(row).encode()
    capture.update(payload_hex=raw.hex(), sha256=hashlib.sha256(raw).hexdigest())
    if claimed_series == "S":
        assert quote(evidence).charge == Decimal(".02")
    else:
        with pytest.raises(ValueError, match="FEE_CATALOG_OR_OVERRIDE_INVALID"):
            quote(evidence)


def test_activation_clock_and_historical_lineage(evidence):
    q = quote(evidence)
    args = dict(ticker="M", side="BUY_YES", quantity=1, price=Decimal(".30"))
    assert fees.historical_fee_quote(q.decode(), **args) == q
    with pytest.raises(ValueError, match="STALE"):
        fees.verify_fee_quote(
            q.decode(), **args, simulator_floor=Decimal(0), now=NOW + timedelta(seconds=61)
        )
    payload = q.decode()
    payload["simulated_charge"] = "0"
    with pytest.raises(ValueError, match="RECOMPUTATION"):
        fees.historical_fee_quote(payload, **args)


def test_fresh_quote_does_not_extend_original_clock(evidence):
    late = NOW + timedelta(seconds=40)
    q = fees.build_fee_quote(
        evidence=evidence,
        ticker="M",
        side="BUY_YES",
        price=Decimal(".30"),
        simulator_floor=Decimal(0),
        now=late,
    )
    with pytest.raises(ValueError, match="CAPTURE_AMBIGUOUS_OR_STALE"):
        fees.verify_fee_quote(
            q.decode(),
            ticker="M",
            side="BUY_YES",
            quantity=1,
            price=Decimal(".30"),
            simulator_floor=Decimal(0),
            now=NOW + timedelta(seconds=70),
        )


@pytest.mark.parametrize(
    "field,value",
    [("ticker", "OTHER"), ("side", "SELL_YES"), ("quantity", 2), ("price", Decimal(".31"))],
)
def test_quote_cannot_move_to_other_order(evidence, field, value):
    args = dict(ticker="M", side="BUY_YES", quantity=1, price=Decimal(".30"))
    args[field] = value
    with pytest.raises(ValueError, match="MISMATCH"):
        fees.historical_fee_quote(quote(evidence).decode(), **args)


def test_new_guarded_missing_contract_rejected():
    with pytest.raises(ValueError, match="EVIDENCE_REQUIRED"):
        fees.decision_fee_quote(
            {},
            ticker="M",
            side="BUY_YES",
            quantity=1,
            price=Decimal(".30"),
            simulator_floor=Decimal(0),
            now=NOW,
            required=True,
        )


@pytest.mark.parametrize("index", [0, 1, 2])
@pytest.mark.parametrize("bad", ["-0.01", "NaN", "Infinity", "-Infinity"])
def test_nonfinite_and_negative_math_inputs_rejected(index, bad):
    values = [Decimal(".30"), Decimal("1"), Decimal(".07")]
    values[index] = Decimal(bad)
    with pytest.raises(ValueError, match="FINITE_NONNEGATIVE"):
        fees.single_buy_fees(*values)


@pytest.mark.parametrize(
    "price,rate,trade,debit",
    [
        ("0", ".07", "0.000000", "0.00"),
        ("1", ".07", "0.000000", "1.00"),
        (".5", ".04", "0.010000", "0.51"),
        (".5", ".0400001", "0.010001", "0.52"),
        (".3001", "0", "0.000000", "0.31"),
    ],
)
def test_rounding_discontinuities_are_explicit(price, rate, trade, debit):
    result = fees.single_buy_fees(Decimal(price), Decimal(1), Decimal(rate))
    assert result["trade_fee"] == Decimal(trade)
    assert result["total_debit"] == Decimal(debit)
    assert result["estimated_fee"] == Decimal(debit) - Decimal(price)


def test_out_of_range_price_rejected():
    with pytest.raises(ValueError, match="PRICE_OUT_OF_RANGE"):
        fees.single_buy_fees(Decimal("1.01"), Decimal(1), Decimal(".07"))


def test_tampered_document_original_rejected(evidence):
    evidence["documents"][0]["payload_hex"] = b"changed original".hex()
    with pytest.raises(ValueError, match="ORIGINAL_DOCUMENT_MISMATCH"):
        quote(evidence)


def test_tampered_catalog_original_rejected(evidence):
    evidence["captures"][0]["payload_hex"] = b"changed original".hex()
    with pytest.raises(ValueError, match="CAPTURE_HASH_MISMATCH"):
        quote(evidence)


def test_unregistered_rate_change_cannot_reuse_policy_identity(evidence, monkeypatch):
    from dataclasses import replace

    changed = replace(fees.CERTIFIED_FEE_POLICIES[0], taker_rate="0")
    monkeypatch.setattr(fees, "CERTIFIED_FEE_POLICIES", (changed,))
    with pytest.raises(ValueError, match="REVIEWED_FEE_POLICY_REQUIRED"):
        quote(evidence)


def test_later_validation_cannot_admit_original_received_after_quote(evidence):
    q = quote(evidence).decode()
    capture = q["evidence"]["captures"][0]
    row = json.loads(bytes.fromhex(capture["payload_hex"]))
    row["received_at"] = (NOW + timedelta(seconds=10)).isoformat()
    raw = json.dumps(row).encode()
    capture.update(payload_hex=raw.hex(), sha256=hashlib.sha256(raw).hexdigest())
    with pytest.raises(ValueError, match="CAPTURE_AMBIGUOUS_OR_STALE"):
        fees.verify_fee_quote(
            q,
            ticker="M",
            side="BUY_YES",
            quantity=1,
            price=Decimal(".30"),
            simulator_floor=Decimal(0),
            now=NOW + timedelta(seconds=20),
        )


@pytest.fixture
def optional_evidence(evidence, monkeypatch):
    """Synthetic schema binding tests mechanics, never a real registered policy."""
    from dataclasses import replace

    schema = b"SYNTHETIC EventData optional paired fields, not a certification"
    sha = hashlib.sha256(schema).hexdigest()
    monkeypatch.setattr(fees, "EVENT_DATA_SCHEMA_SHA256", sha)
    old = fees.CERTIFIED_FEE_POLICIES[0]
    policy = replace(
        old,
        documents=old.documents + ((fees.EVENT_SCHEMA_URL, sha),),
        event_override_interpretation=fees.OPTIONAL_EVENT_OVERRIDE_PROFILE,
        event_schema_document_sha256=sha,
    )
    monkeypatch.setattr(fees, "CERTIFIED_FEE_POLICIES", (policy,))
    evidence["policy_version"] = policy.version
    evidence["documents"].append(
        {"url": fees.EVENT_SCHEMA_URL, "received_at": NOW.isoformat(), "payload_hex": schema.hex()}
    )
    body = {
        "event": {
            "event_ticker": "E",
            "series_ticker": "S",
            "sub_title": "",
            "title": "Synthetic fixture",
            "collateral_return_type": "binary",
            "mutually_exclusive": True,
            "settlement_sources": [],
        },
        "markets": [],
    }
    _replace_optional_event(evidence, body)
    return evidence


def _replace_optional_event(evidence, body):
    wrapper = {"url": fees.PUBLIC_BASE + "/events/E", "received_at": NOW.isoformat(), "body": body}
    raw = json.dumps(wrapper).encode()
    evidence["captures"][1] = {"payload_hex": raw.hex(), "sha256": hashlib.sha256(raw).hexdigest()}
    original = json.dumps(body).encode()
    evidence["event_original"] = {
        "url": wrapper["url"],
        "status": 200,
        "received_at": NOW.isoformat(),
        "payload_hex": original.hex(),
        "sha256": hashlib.sha256(original).hexdigest(),
    }


def _optional_body(evidence):
    return json.loads(bytes.fromhex(evidence["event_original"]["payload_hex"]))


def test_legacy_policy_hash_and_quote_shape_unchanged(evidence):
    from dataclasses import asdict

    policy = fees.CERTIFIED_FEE_POLICIES[0]
    legacy = asdict(policy)
    legacy.pop("permitted_series")
    legacy.pop("event_override_interpretation")
    legacy.pop("event_schema_document_sha256")
    assert policy.version == hashlib.sha256(fees._bytes(legacy)).hexdigest()
    result = quote(evidence)
    assert "event_override_state" not in result.decode()
    assert (
        fees.historical_fee_quote(
            result.decode(), ticker="M", side="BUY_YES", quantity=1, price=Decimal(".30")
        )
        == result
    )


def test_schema_bound_absent_and_null_pairs_inherit_identical_fees(optional_evidence):
    absent = quote(optional_evidence)
    body = _optional_body(optional_evidence)
    body["event"].update(fee_type_override=None, fee_multiplier_override=None)
    _replace_optional_event(optional_evidence, body)
    null = quote(optional_evidence)
    assert absent.charge == null.charge == Decimal(".02")
    assert absent.decode()["event_override_state"] == "INHERIT_SERIES_OMITTED_PAIR"
    assert null.decode()["event_override_state"] == "INHERIT_SERIES_EXPLICIT_NULL_PAIR"
    assert absent.decode()["fee_decomposition"] == null.decode()["fee_decomposition"]
    assert (
        fees.historical_fee_quote(
            absent.decode(), ticker="M", side="BUY_YES", quantity=1, price=Decimal(".30")
        )
        == absent
    )


@pytest.mark.parametrize(
    "fields",
    [
        {"fee_type_override": None},
        {"fee_multiplier_override": None},
        {"fee_type_override": "quadratic", "fee_multiplier_override": 1},
        {"fee_type_override": None, "fee_multiplier_override": 0},
        {"fee_type_override": None, "fee_multiplier_override": False},
        {"fee_type_override": "", "fee_multiplier_override": None},
        {"fee_type_override": "quadratic", "fee_multiplier_override": None},
        {"fee_type_override": None, "fee_multiplier_override": "1"},
        {"fee_type_override": {}, "fee_multiplier_override": None},
    ],
)
def test_optional_partial_and_nonnull_pairs_rejected(optional_evidence, fields):
    body = _optional_body(optional_evidence)
    body["event"].update(fields)
    _replace_optional_event(optional_evidence, body)
    with pytest.raises(ValueError, match="PARTIAL_EVENT_OVERRIDE|EVENT_OVERRIDE_UNSUPPORTED"):
        quote(optional_evidence)


@pytest.mark.parametrize(
    "change",
    [
        {"event_ticker": None},
        {"series_ticker": False},
        {"title": None},
        {"sub_title": []},
        {"collateral_return_type": 0},
        {"mutually_exclusive": 1},
        {"settlement_sources": [{"url": 123}]},
    ],
)
def test_optional_required_event_types_rejected(optional_evidence, change):
    body = _optional_body(optional_evidence)
    body["event"].update(change)
    _replace_optional_event(optional_evidence, body)
    with pytest.raises(ValueError, match="SCHEMA"):
        quote(optional_evidence)


@pytest.mark.parametrize("change", [{"markets": None}, {"markets": [0]}, {"event": None}])
def test_optional_full_envelope_required(optional_evidence, change):
    body = _optional_body(optional_evidence)
    body.update(change)
    _replace_optional_event(optional_evidence, body)
    with pytest.raises(ValueError, match="FULL_EVENT_RESPONSE"):
        quote(optional_evidence)


@pytest.mark.parametrize("placement", ["event", "envelope", "nested"])
def test_optional_unknown_fee_schema_drift_rejected(optional_evidence, placement):
    body = _optional_body(optional_evidence)
    if placement == "event":
        body["event"]["fee_schedule_override"] = {}
    elif placement == "envelope":
        body["fee_override"] = {}
    else:
        body["event"]["product_metadata"] = {"fee_override": {}}
    _replace_optional_event(optional_evidence, body)
    with pytest.raises(ValueError, match="UNKNOWN_EVENT_FEE_FIELD"):
        quote(optional_evidence)


def test_optional_duplicate_raw_json_cannot_be_normalized_away(optional_evidence):
    raw = bytes.fromhex(optional_evidence["event_original"]["payload_hex"])
    raw = raw.replace(b'"event_ticker": "E"', b'"event_ticker": "WRONG", "event_ticker": "E"')
    optional_evidence["event_original"].update(
        payload_hex=raw.hex(), sha256=hashlib.sha256(raw).hexdigest()
    )
    with pytest.raises(ValueError, match="DUPLICATE_JSON"):
        quote(optional_evidence)


def test_optional_nonfinite_original_rejected(optional_evidence):
    raw = bytes.fromhex(optional_evidence["event_original"]["payload_hex"]).replace(
        b'"markets": []', b'"markets": [NaN]'
    )
    optional_evidence["event_original"].update(
        payload_hex=raw.hex(), sha256=hashlib.sha256(raw).hexdigest()
    )
    with pytest.raises(ValueError, match="NONFINITE_JSON"):
        quote(optional_evidence)


@pytest.mark.parametrize(
    "change",
    [
        {"url": fees.PUBLIC_BASE + "/events/OTHER"},
        {"status": 201},
        {"status": True},
        {"received_at": (NOW + timedelta(seconds=1)).isoformat()},
        {"sha256": "0" * 64},
    ],
)
def test_optional_original_receipt_binding_rejected(optional_evidence, change):
    optional_evidence["event_original"].update(change)
    with pytest.raises(ValueError, match="RAW_EVENT_ORIGINAL_MISMATCH"):
        quote(optional_evidence)


def test_optional_original_body_projection_rejected(optional_evidence):
    body = _optional_body(optional_evidence)
    body["event"]["fee_type_override"] = "quadratic"
    raw = json.dumps(body).encode()
    optional_evidence["event_original"].update(
        payload_hex=raw.hex(), sha256=hashlib.sha256(raw).hexdigest()
    )
    with pytest.raises(ValueError, match="BODY_MAPPING"):
        quote(optional_evidence)


def test_optional_original_required(optional_evidence):
    del optional_evidence["event_original"]
    with pytest.raises(ValueError, match="RAW_EVENT_ORIGINAL_REQUIRED"):
        quote(optional_evidence)


@pytest.mark.parametrize("mode", ["wrong_hash", "missing_schema_pair", "unknown_profile"])
def test_optional_reviewed_profile_required(optional_evidence, monkeypatch, mode):
    from dataclasses import replace

    policy = fees.CERTIFIED_FEE_POLICIES[0]
    if mode == "wrong_hash":
        policy = replace(policy, event_schema_document_sha256="0" * 64)
    elif mode == "missing_schema_pair":
        policy = replace(policy, documents=policy.documents[:1])
        optional_evidence["documents"] = optional_evidence["documents"][:1]
    else:
        policy = replace(policy, event_override_interpretation="unreviewed")
    monkeypatch.setattr(fees, "CERTIFIED_FEE_POLICIES", (policy,))
    optional_evidence["policy_version"] = policy.version
    with pytest.raises(ValueError, match="REVIEWED_EVENT_SCHEMA|PROFILE_UNSUPPORTED"):
        quote(optional_evidence)


def test_optional_future_schema_doc_receipt_rejected(optional_evidence):
    optional_evidence["documents"][1]["received_at"] = (NOW + timedelta(seconds=1)).isoformat()
    with pytest.raises(ValueError, match="DOCUMENT_AUTHORITY_OR_VISIBILITY"):
        quote(optional_evidence)


def test_optional_does_not_register_policy(optional_evidence, monkeypatch):
    monkeypatch.setattr(fees, "CERTIFIED_FEE_POLICIES", ())
    with pytest.raises(ValueError, match="REVIEWED_FEE_POLICY_REQUIRED"):
        quote(optional_evidence)


def test_optional_explicit_null_settlement_sources_is_valid_shape(optional_evidence):
    body = _optional_body(optional_evidence)
    body["event"]["settlement_sources"] = None
    _replace_optional_event(optional_evidence, body)
    assert quote(optional_evidence).charge == Decimal(".02")
    del body["event"]["settlement_sources"]
    _replace_optional_event(optional_evidence, body)
    with pytest.raises(ValueError, match="REQUIRED_FIELDS"):
        quote(optional_evidence)


@pytest.mark.parametrize("placement", ["captured", "top_level", "nested"])
@pytest.mark.parametrize(
    "fields,valid",
    [
        (
            {
                "fee_type_override": None,
                "fee_multiplier_override": None,
                "fee_waiver_expiration_time": None,
            },
            True,
        ),
        ({"fee_type_override": None}, False),
        ({"fee_type_override": "quadratic", "fee_multiplier_override": 1}, False),
        ({"fee_waiver_expiration_time": "2026-09-09T13:00:00Z"}, False),
        ({"fee_schedule_override": None}, False),
    ],
)
def test_optional_market_fee_fields_fail_closed(optional_evidence, placement, fields, valid):
    if placement == "captured":
        capture = optional_evidence["captures"][0]
        wrapper = json.loads(bytes.fromhex(capture["payload_hex"]))
        wrapper["body"]["market"].update(fields)
        raw = json.dumps(wrapper).encode()
        capture.update(payload_hex=raw.hex(), sha256=hashlib.sha256(raw).hexdigest())
    else:
        body = _optional_body(optional_evidence)
        markets = [{"ticker": "M", "event_ticker": "E", **fields}]
        if placement == "top_level":
            body["markets"] = markets
        else:
            body["event"]["markets"] = markets
        _replace_optional_event(optional_evidence, body)
    if valid:
        assert quote(optional_evidence).charge == Decimal(".02")
    else:
        with pytest.raises(ValueError, match="FEE_.*UNSUPPORTED|UNKNOWN_EVENT_FEE_FIELD"):
            quote(optional_evidence)


def test_actual_public_get_event_original_schema_smoke():
    """Exact historical public bytes: parser smoke only, not fee admission/certification."""
    import base64
    import zlib

    compressed = (
        "c%1Fp-EP}96ae7+Jq4j_$Cao*%Qr*OW?RsuD;fjcIurse(J>K9)F>*gR|MEY>_PereUv@P4kbIWYe}8ZB~BXxxkx"
        "0E;?FtfgY3Mw=q1gSHGOX-MA360SJu>e!dV7?A(E%)6(wp(r8Tk=!8u%&gvXMqO6IXz6%?93J$ZTj<oWRo+SH@Pi"
        "X|7&WokY9b#{FE;`HS4Pe(X7JAUEV&P2C*mn=zsPGgp*^qn=eM;0fgimM`p2c)qop|5S*&Qbj{v+Zg9^BVeQm8uB"
        "ms~861Rmm=41`EPVI<g8W(kfALMiqg9NVBSx=82x=QdE+!tgDe#Qpsr9HcLz2REpC~FQbGW35B`*{@%(-"
        "rUyA&<)VO&=&2rmWWkVnXr+pB`sh))zN;TTPDJ*|x_Sd6D#<Qrtd|CDv5kWxJ34ag8OAruS~n_SY9fVZ`cgHAIl^"
        "S%&tGzMMhoQFBcumFM4_O_(D75f>6=C~nn8a8M?(?I(J3PtL!z_i_j)ffA}^?F)|ikyPPiy(Oo`sAsgf0JYja7lt"
        "Y`X(q|M3gx3Z_6KgG^Casv-LufHTn=(~a@`Z4eegM~7Ycg)LQ&#R|lxU+z*hL?m_v^jzW7edC(*;vzXGLE8ks1}k"
        "XaHcvXWF4(zh*#UvcNK;E|M|h3{ZX-"
        "$sa4xx&FO6NY|e55`8l$35tH&_w*#&_z@0fu+lGQW+W{3uA3q==5t$3fAbnfU6;iui{nh}^W&n{Sm6SS}>QL_AcT"
        "vzB4i2h?-"
        "i$@Dp2_A;OK0b1OA1LZnW)O{O}=iif;`^aik3DVo6<ahLqKwU%1}HYcc1}hIix_G=+d}pC3P@1i9+*qduK{Tq}m>"
        "~RCK*!2maPI0JfpxstGDtaayk}BXzYsSs<veNNr`Wq=}FzO?9@`X*@zd{EQZGUVFKWUYzP=Mx=yZg^-"
        "XQXud)_g=&OY*_<@0WBB8Tk$z-"
        "eKevf7=v)eZNlT;z`i++3>xq^ymppBzI)nCNv0w=!94#OmIjZBOo|;<1Q`V!wZz+Y-"
        "&tM}{q!TDne5EJ0ztql2Cxj=J&ZktDGm_{%ArOD)EmO;Spl49{muzDcI?<H^ol><FaM<E}HAas)%p!+Ggtf2h`9F"
        "XCT~^6b@7VKKXQ;tWH)rrb^s=5$sdM8;A<jA7j1Jv0vO<6d3{~&;g4XF!j?lbPh`?eXix8>c@sg?LnVQhIjPv@|o"
        "T93P)#%;Y-u6o8-gp-uog#9*`4Dw(VB2-1sY*vMN%ZJ9ZmcWBd0mEOgU2S-"
        "kDMme=ah@LH5WaF_h?o}$d)Rv^z1jMcH;`HWhbI~p#dm2w0C7b9=pElOgvoxE@4YD8e?y{Q##g{w=eDI|8-"
        "VwZk6oM{XU=8gJ`mI>!y!`Z#s3?&#RHq^Rs$J-"
        ";SOW+vxcq^^A<3M$cY7qk(!x4^hwE`f2q1yq**L8`9G;dOk!w>likA8a+GptRrNAp6iHuXnHzE&w+Y6eR@u!uS!p"
        "R*LwPf&wIhAWB4?D_VVcr<kPuzKK&Ly{euIF4WI)7#eINA;XwiQI|0S5fVy`LsE6&b>l#51E+{sF8bNyn#RCP!w="
        "SsX`Qym{gitRG#&*zZ=#GcGhWcR-qJ9UWxKmNrC^}G4w@*>;pdaG;VV9!5)2gU<*NTR|18HIuJ-DK-QPe2ftEf9r"
        "QTNsrb+9)X<4@m29UOS@_R`^~7j-y_vD*!)=XODA+Z~X42GW6$dcEI<;zL3jMO~0aek-"
        "K@T|??b5CegM^x%+s22ulQFQnc;NWI&KbW=Qb9Nk=OtpR=)qjt}Sy0+87sN2b?Zx|iOsNc(|dpJg~>v<caZY!g~T"
        "{G&sq4{3)J}~MVMh&CAjQRr^_3z+))ZhPN)bk(#aTlXu*QdIDirO8Db{QHNLkAif^cw2iQ$xd6L&H0N6*Yq12SEc"
        "Ps1dYR&|sjT!R-"
        "sW@2AuCe?`|XGc7Nn+h1>fF`uE~b0DALSIOt3mCuRc^Fi_%8a@r5y?llP`3&zHpSXk1|5eYO`g!%{>brjd6U{kb"
    )
    raw = zlib.decompress(base64.b85decode(compressed))
    sha = "fc7a7f22b0004ca807619636b58c180c45c9c99e294ca69b5be1a1fc15c89fda"
    assert hashlib.sha256(raw).hexdigest() == sha
    body = json.loads(raw)
    received = datetime.fromisoformat("2026-09-10T21:36:27.911185+00:00")
    url = fees.PUBLIC_BASE + "/events/KXTEMPMIAH-26SEP1018"
    policy = fees.CertifiedFeePolicy(
        "UNREGISTERED-schema-smoke-only",
        received.isoformat(),
        received.isoformat(),
        "0.07",
        ((fees.EVENT_SCHEMA_URL, fees.EVENT_DATA_SCHEMA_SHA256),),
        "",
        "",
        "",
        "0",
        event_override_interpretation=fees.OPTIONAL_EVENT_OVERRIDE_PROFILE,
        event_schema_document_sha256=fees.EVENT_DATA_SCHEMA_SHA256,
    )
    state = fees._optional_event_override_state(
        body["event"],
        evidence={
            "event_original": {
                "url": url,
                "status": 200,
                "received_at": received.isoformat(),
                "payload_hex": raw.hex(),
                "sha256": sha,
            }
        },
        event_url=url,
        captured_body=body,
        market=body["markets"][0],
        received_at=received,
        quoted_at=received,
        current=received,
        policy=policy,
        documents=list(policy.documents),
    )
    assert state == "INHERIT_SERIES_OMITTED_PAIR"
    assert len(body["markets"]) == 10
