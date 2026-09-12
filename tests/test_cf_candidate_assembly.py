from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest
from test_cf_feature_provenance import complete_cf_inputs
from test_overnight_provenance import artifact

from kalshi_predictor.overnight_paper.cf_candidate_assembly import assemble_cf_research_candidate
from kalshi_predictor.overnight_paper.provenance import canonical_hash
from kalshi_predictor.overnight_paper.qualification import Readiness, qualify_candidate
from kalshi_predictor.paper.models import PaperDecision


def preparation():
    args = complete_cf_inputs()
    inputs = args["decision"]
    inputs.update(side="BUY_YES", executable_price="0.4")
    args["decision_id"] = canonical_hash(inputs)
    paper = PaperDecision(
        ticker=inputs["ticker"], forecast_id=inputs["forecast_id"],
        model_name=inputs["model_name"], side="BUY_YES",
        probability=Decimal(str(inputs["forecast_probability"])),
        market_price=Decimal("0.4"), limit_price=Decimal("0.4"), edge=Decimal("0.1"),
        quantity=1, reason="synthetic original-bound research fixture",
    )
    return paper, args


def test_original_bound_cf_candidate_uses_existing_qualification_without_cost_invention():
    paper, args = preparation()
    candidate = assemble_cf_research_candidate(paper_decision=paper, provenance_args=args)
    result = qualify_candidate(**candidate.qualification_args)
    assert dict(result.gates)["FRESH_ANALYTICAL_SOURCE"]
    assert result.status == Readiness.PAPER_NOT_READY
    assert result.net_ev is None
    assert "POSITIVE_NET_EV" in result.blockers
    assert candidate.decision is paper
    assert candidate.shadow_payload["full_net_ev"] is None
    assert candidate.shadow_payload["cf_context"]["target"]["symbol"] == "SOL"


@pytest.mark.parametrize("changes", [
    {"ticker": "KXSOLE-WRONG-T100"}, {"probability": Decimal("0.99")},
    {"forecast_id": -1}, {"limit_price": Decimal("0.01")}, {"side": "BUY_NO"},
])
def test_actual_paper_decision_must_match_forecast_and_inputs(changes):
    paper, args = preparation()
    with pytest.raises(ValueError, match="PAPER_DECISION_MISMATCH"):
        assemble_cf_research_candidate(
            paper_decision=replace(paper, **changes), provenance_args=args,
        )


def catalog_candidate(status, include_book=False, include_identity=False, wrong_series=False):
    paper, args = preparation()
    context = args["context"]
    at = args["now"]
    inputs = args["decision"]
    public = artifact(dict(
        url="https://external-api.kalshi.com/trade-api/v2/markets/" + inputs["ticker"],
        clock_basis="public_rest_receipt", provider_updated_at=None, provider_generated_at=None,
        received_at=at.isoformat(), available_at=at.isoformat(),
        body={"market": dict(
            ticker=inputs["ticker"], event_ticker=inputs["event_id"], status=status,
            close_time=(at+timedelta(hours=1)).isoformat(),
            volume_fp="20000", open_interest_fp="5000", liquidity_dollars="20000",
            price_ranges=[dict(start="0", end="1", step="0.01")],
            rules_primary="Declared CF benchmark settlement", rules_secondary=None,
        )},
    ))
    sources = context.source_artifacts + (public,)
    if include_identity:
        base = "https://external-api.kalshi.com/trade-api/v2"
        terms = context.cf_context.target.rules.rule_source
        event = public.decode() | dict(
            url=base + "/events/" + inputs["event_id"],
            body={"event": dict(
                event_ticker=inputs["event_id"],
                series_ticker="WRONG" if wrong_series else "KXSOLE",
            )},
        )
        series = public.decode() | dict(
            url=base + "/series/KXSOLE",
            body={"series": dict(ticker="KXSOLE", category="Crypto", contract_terms_url=terms)},
        )
        sources += (artifact(event), artifact(series))
        inputs["market_rules_hash"] = canonical_hash(dict(
            primary="Declared CF benchmark settlement", secondary=None, contract_terms_url=terms,
        ))
    if include_book:
        book = public.decode() | dict(
            url=public.decode()["url"] + "/orderbook",
            body={"orderbook_fp": {
                "yes_dollars": [["0.35", "1000"]], "no_dollars": [["0.6", "1000"]],
            }},
        )
        sources += (artifact(book),)
    hashes = [item.sha256 for item in sources]
    features = artifact(context.features_artifact.decode() | {"source_hashes": hashes})
    forecast = context.artifacts["forecast"].decode() | dict(
        source_hashes=hashes, features_artifact_sha256=features.sha256,
    )
    config = context.artifacts["config"].decode() | {"opportunity_max_spread": "0.2"}
    inputs.update(settings=config, config_hash=canonical_hash(config))
    forecast["config_hash"] = canonical_hash(config)
    artifacts = context.artifacts | {"forecast": artifact(forecast), "config": artifact(config)}
    inputs.update(
        source_hashes=hashes, features_artifact_sha256=features.sha256,
        forecast_artifact_sha256=artifacts["forecast"].sha256,
        config_artifact_sha256=artifacts["config"].sha256,
    )
    inputs["source_timestamps"] = [dict(sha256=item.sha256, **{
        key: item.decode()[key] for key in (
            "provider_updated_at", "provider_generated_at", "received_at",
            "available_at", "clock_basis",
        )
    }) for item in sources]
    args["context"] = replace(
        context, source_artifacts=sources, artifacts=artifacts, features_artifact=features,
    )
    args["decision_id"] = canonical_hash(inputs)
    candidate = assemble_cf_research_candidate(paper_decision=paper, provenance_args=args)
    result = qualify_candidate(**candidate.qualification_args)
    return result


@pytest.mark.parametrize("status,passes", [("active", True), ("closed", False)])
def test_public_market_gate_replays_actual_catalog_original(status, passes):
    result = catalog_candidate(status)
    gates = dict(result.gates)
    assert gates["CURRENT_PUBLIC_MARKET"] is passes
    assert gates["FRESH_ANALYTICAL_SOURCE"]
    assert not gates["VALID_EXECUTABLE_BOOK"]
    assert result.status == Readiness.PAPER_NOT_READY


def test_cf_assembly_passes_real_executable_book_gate_without_granting_admission():
    result = catalog_candidate("active", include_book=True)
    assert dict(result.gates)["VALID_EXECUTABLE_BOOK"]
    assert result.status == Readiness.PAPER_NOT_READY
    assert result.net_ev is None


@pytest.mark.parametrize("wrong_series", [False, True])
def test_cf_assembly_replays_event_series_identity_and_keeps_rule_uncertified(wrong_series):
    result = catalog_candidate(
        "active", include_book=True, include_identity=True, wrong_series=wrong_series,
    )
    gates = dict(result.gates)
    assert gates["EXACT_IDENTITY"] is (not wrong_series)
    assert gates["VALID_EXECUTABLE_BOOK"]
    assert not gates["CERTIFIED_SETTLEMENT_RULE"]
    assert not gates["POSITIVE_NET_EV"]
    assert result.status == Readiness.PAPER_NOT_READY
