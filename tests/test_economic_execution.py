"""Actual existing economic arithmetic in an isolated in-memory caller Session."""

import hashlib
import json
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings
from kalshi_predictor.data.schema import EconomicFeature, EconomicMarketLink, Market, MarketSnapshot
from kalshi_predictor.data_sources import economic_execution as execution
from kalshi_predictor.overnight_paper.provenance import canonical_hash
from kalshi_predictor.research.fred import FREDOriginal, FREDResponse
from kalshi_predictor.utils.time import utc_now


@pytest.fixture(scope="module")
def code_bundle():
    return execution.economic_model_code_bundle(Path(__file__).resolve().parents[1])


@pytest.fixture
def example(monkeypatch, code_bundle):
    engine = create_engine("sqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def foreign_keys(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")

    for table in (Market, MarketSnapshot, EconomicFeature, EconomicMarketLink):
        table.__table__.create(engine)
    session = Session(engine)
    session.begin()
    now = utc_now()
    freeze = now - timedelta(minutes=2)
    capture = now - timedelta(seconds=1)
    target = now.date().replace(day=1)
    latest = (target - timedelta(days=1)).replace(day=1)
    prior = (latest - timedelta(days=1)).replace(day=1)
    event_id = "KXCPI-" + target.strftime("%y%b").upper()
    ticker = event_id + "-T0.3"
    config = Settings.model_construct(
        kalshi_api_key_id=None,
        kalshi_private_key_path=None,
        postgres_password="",
        execution_confirmation_token="",
    )
    settings = config.model_dump(mode="json")
    dependencies, code = code_bundle
    terms = b"Synthetic CPI terms for hash-binding tests only, not official settlement evidence."
    monkeypatch.setattr(execution, "TERMS_SHA256", hashlib.sha256(terms).hexdigest())
    artifact = execution._artifact
    model = artifact(
        dict(
            name="economic_v1",
            version="fixture-v1",
            model_kind="fixed_heuristic",
            created_at=freeze,
            frozen_at=freeze,
            available_at=freeze,
            training_cutoff=None,
            training_dataset_hashes=[],
            parameters=settings,
            parameters_sha256=canonical_hash(settings),
            code_sha256=hashlib.sha256(code).hexdigest(),
            code_dependencies=dependencies,
            model_entrypoint=execution.ENTRYPOINT,
        )
    )
    mapping = dict(
        series_ticker="KXCPI",
        event_ticker=event_id,
        source_series="CPIAUCSL",
        concept="headline_cpi_u",
        seasonal_adjustment="SA",
        outcome_units="published_one_decimal_monthly_percent_change",
        comparator="strictly_greater",
        revision_policy="exclude_revisions_after_expiration",
        contract_terms_sha256=execution.TERMS_SHA256,
        target_month=target.strftime("%Y-%m"),
        scheduled_release_at=now + timedelta(days=2),
        strike_percent="0.3",
    )
    procedure = artifact(
        dict(
            kind="lagged-cpi-procedure-v1",
            name="synthetic research",
            version="1",
            created_at=freeze,
            frozen_at=freeze,
            available_at=freeze,
            model_artifact_sha256=model.sha256,
            settings_sha256=canonical_hash(settings),
            variant_methods=execution.METHODS,
            contrast_type=execution.CONTRAST,
            ticker=ticker,
            contract_mapping=mapping,
            lagged_periods=[prior.isoformat(), latest.isoformat()],
        )
    )
    raw = json.dumps(
        dict(
            units="lin",
            output_type=1,
            observations=[
                dict(
                    date=stamp.isoformat(),
                    value=value,
                    realtime_start=now.date().isoformat(),
                    realtime_end=now.date().isoformat(),
                )
                for stamp, value in ((prior, "330.000"), (latest, "331.000"))
            ],
        )
    ).encode()
    url = (
        "https://api.stlouisfed.org/fred/series/observations?series_id=CPIAUCSL"
        f"&observation_start={prior}&observation_end={latest}&realtime_start={now.date()}"
        f"&realtime_end={now.date()}"
    )
    source = FREDResponse(
        "observations",
        "CPIAUCSL",
        FREDOriginal(url, capture, hashlib.sha256(raw).hexdigest(), raw),
        json.loads(raw),
    )

    def envelope(path, body):
        return artifact(dict(url=execution.BASE + path, received_at=capture, body=body))

    market = envelope(
        "/markets/" + ticker,
        dict(
            market=dict(
                ticker=ticker,
                event_ticker=event_id,
                series_ticker="KXCPI",
                title="Will CPI rise more than 0.3%?",
                status="active",
                strike_type="greater",
                floor_strike="0.3",
                close_time=now + timedelta(days=1),
                rules_primary=(
                    "If the Consumer Price Index (CPI) increases by more than 0.3% "
                    f"(single-decimal) in {target.strftime('%B %Y')}, "
                    "then the market resolves to Yes."
                ),
            )
        ),
    )
    series = envelope(
        "/series/KXCPI",
        dict(
            series=dict(
                ticker="KXCPI",
                contract_terms_url="https://assets.kalshi.com/contract_terms/CPI.pdf",
                fee_type="quadratic_with_maker_fees",
                fee_multiplier=1,
            )
        ),
    )
    event_original = envelope(
        "/events/" + event_id, dict(event=dict(event_ticker=event_id, series_ticker="KXCPI"))
    )
    book = envelope(
        "/markets/" + ticker + "/orderbook",
        dict(orderbook_fp=dict(yes_dollars=[["0.45", "100"]], no_dollars=[["0.50", "100"]])),
    )
    yield dict(
        session=session,
        frozen_model=model,
        frozen_procedure=procedure,
        model_code=code,
        repository=Path(__file__).resolve().parents[1],
        settings=config,
        source=source,
        market=market,
        event=event_original,
        series=series,
        book=book,
        contract_terms=terms,
    )
    session.rollback()
    session.close()
    engine.dispose()


def test_actual_existing_model_receipt_and_caller_rollback(example):
    receipt = execution.execute_lagged_cpi_research(**example).decode()
    score = Decimal(1) / Decimal(330)
    expected = Decimal("0.475") + score * Decimal("0.10") * Decimal("0.70")
    assert Decimal(receipt["source_on_probability"]) == expected
    assert receipt["source_off_probability"] == "0.475"
    assert receipt["feature_original"]["forecast_value"] is None
    assert (
        receipt["source_features"]["momentum_status"]
        == "ACTUAL_VS_PREVIOUS_MOMENTUM_NOT_CONSENSUS_SURPRISE"
    )
    assert (
        receipt["forecast_original"]["feature_json"]["economic_feature_id"]
        == receipt["ephemeral_feature_id"]
    )
    assert receipt["link_original"]["id"] == receipt["ephemeral_link_id"]
    assert (
        receipt["forecast_generated_at"]
        > receipt["forecast_original"]["feature_json"]["snapshot_captured_at"]
    )
    assert receipt["dependencies_before"] == receipt["dependencies_after"]
    assert receipt["event_envelope_sha256"] == example["event"].sha256
    assert receipt["ephemeral_session"] and not receipt["runtime_certified"]
    assert not receipt["fee_execution_verified"] and not receipt["settlement_eligible"]
    session = example["session"]
    assert session.in_transaction()
    assert session.scalar(select(func.count()).select_from(EconomicFeature)) == 1
    session.rollback()
    assert session.scalar(select(func.count()).select_from(EconomicFeature)) == 0


@pytest.mark.parametrize(
    "change",
    [
        "wrong_series",
        "before_freeze",
        "future_receipt",
        "nonadjacent",
        "missing_value",
        "actual_target",
        "wrong_rule",
        "closed",
        "event_override",
        "wrong_event",
        "stale_book",
        "wrong_code",
        "wrong_settings",
        "future_freeze",
        "bad_terms",
    ],
)
def test_rejects_invalid_originals_before_ephemeral_writes(example, change):
    artifact = execution._artifact
    if change in {"wrong_series", "before_freeze", "future_receipt"}:
        source = example["source"]
        if change == "wrong_series":
            source = replace(source, series_id="DFF")
        else:
            source = replace(
                source,
                original=replace(
                    source.original,
                    received_at=utc_now() + timedelta(days=-1 if change == "before_freeze" else 1),
                ),
            )
        example["source"] = source
    elif change in {"nonadjacent", "missing_value", "actual_target"}:
        source = example["source"]
        data = json.loads(source.original.payload)
        if change == "missing_value":
            data["observations"][0]["value"] = "."
        else:
            data["observations"][0]["date"] = (
                "2020-01-01"
                if change == "nonadjacent"
                else utc_now().date().replace(day=1).isoformat()
            )
        raw = json.dumps(data).encode()
        example["source"] = replace(
            source,
            data=data,
            original=replace(source.original, payload=raw, sha256=hashlib.sha256(raw).hexdigest()),
        )
    elif change in {"wrong_rule", "closed"}:
        row = example["market"].decode()
        row["body"]["market"]["rules_primary" if change == "wrong_rule" else "status"] = (
            "other" if change == "wrong_rule" else "closed"
        )
        example["market"] = artifact(row)
    elif change in {"event_override", "wrong_event"}:
        row = example["event"].decode()
        row["body"]["event"][
            "fee_multiplier_override" if change == "event_override" else "event_ticker"
        ] = 2
        example["event"] = artifact(row)
    elif change == "stale_book":
        row = example["book"].decode()
        row["received_at"] = (utc_now() - timedelta(seconds=61)).isoformat()
        example["book"] = artifact(row)
    elif change == "wrong_code":
        example["model_code"] = b"unrelated"
    elif change == "wrong_settings":
        example["settings"].paper_min_edge = Decimal("0.99")
    elif change == "bad_terms":
        example["contract_terms"] = b"not pinned"
    else:
        row = example["frozen_procedure"].decode()
        row["frozen_at"] = (utc_now() + timedelta(days=1)).isoformat()
        example["frozen_procedure"] = artifact(row)
    with pytest.raises((ValueError, KeyError)):
        execution.execute_lagged_cpi_research(**example)
    assert example["session"].scalar(select(func.count()).select_from(EconomicFeature)) == 0


def test_post_execution_mutation_rolls_back_savepoint(example, monkeypatch):
    actual = execution.EconomicV1Forecaster.forecast

    def altered(self, session, snapshot):
        result = actual(self, session, snapshot)
        example["settings"].paper_min_edge = Decimal("0.99")
        return result

    monkeypatch.setattr(execution.EconomicV1Forecaster, "forecast", altered)
    with pytest.raises(ValueError, match="FROZEN_MANIFEST"):
        execution.execute_lagged_cpi_research(**example)
    session = example["session"]
    assert session.in_transaction()
    for table in (EconomicFeature, EconomicMarketLink, MarketSnapshot, Market):
        assert session.scalar(select(func.count()).select_from(table)) == 0


def test_persistent_database_is_refused(example, tmp_path):
    engine = create_engine("sqlite:///" + str(tmp_path / "research.db"))
    with Session(engine) as session:
        session.begin()
        example["session"] = session
        with pytest.raises(ValueError, match="EPHEMERAL"):
            execution.execute_lagged_cpi_research(**example)
    assert not (tmp_path / "research.db").exists()
    engine.dispose()


@pytest.mark.parametrize("table", [Market, MarketSnapshot, EconomicFeature, EconomicMarketLink])
@pytest.mark.parametrize("binding_kind", ["mapper", "table"])
def test_alternate_bind_rejected_before_any_connection(example, tmp_path, table, binding_kind):
    memory = create_engine("sqlite:///:memory:")
    path = tmp_path / "must-not-open.db"
    persistent = create_engine("sqlite:///" + str(path))
    connections = []

    @event.listens_for(memory, "connect")
    @event.listens_for(persistent, "connect")
    def connected(*_):
        connections.append(True)
        raise AssertionError("No engine connection may be opened for alternate binds")

    target = table if binding_kind == "mapper" else table.__table__
    with Session(bind=memory, binds={target: persistent}) as session:
        session.begin()
        arguments = example | {"session": session}
        with pytest.raises(ValueError, match="ALTERNATE_SESSION_BINDS_REFUSED"):
            execution.execute_lagged_cpi_research(**arguments)
    assert connections == []
    assert not path.exists()
    persistent.dispose()
    memory.dispose()
