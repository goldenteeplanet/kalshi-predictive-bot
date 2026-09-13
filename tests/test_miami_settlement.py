import hashlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from kalshi_predictor.weather import miami_settlement as module
from kalshi_predictor.weather.miami_index import IndexPoint, MiamiIndexCapture

TARGET = datetime(2026, 9, 10, 22, tzinfo=UTC)
TERMS = b"Synthetic test terms, not an official methodology document"


@pytest.fixture(autouse=True)
def synthetic_terms(monkeypatch):
    monkeypatch.setattr(module, "TERMS_SHA256", hashlib.sha256(TERMS).hexdigest())


def capture(selected_age=0):
    points = tuple(
        IndexPoint(
            TARGET - timedelta(minutes=i),
            Decimal("83.84") if i == selected_age else None,
            "normal" if i == selected_age else "unavailable",
            5 if i == selected_age else 3,
            "config1",
            False,
            True,
        )
        for i in range(60, -1, -1)
    )
    received = TARGET + timedelta(minutes=10)
    return MiamiIndexCapture(
        points, "a" * 64, "b" * 64, received, received, received, "config1", (), "fahrenheit", True
    )


def publications(c):
    return tuple(
        module.FirstPublicationEvidence(
            p.event_at,
            p.event_at + timedelta(minutes=5),
            module.point_fingerprint(p),
            c.index_sha256,
            "INITIAL_PUBLICATION",
            b"synthetic administrator audit fixture",
            hashlib.sha256(b"synthetic administrator audit fixture").hexdigest(),
        )
        for p in c.points
    )


def select(c=None, pubs=None, **kwargs):
    c = c or capture()
    args = dict(
        publications=publications(c) if pubs is None else pubs,
        target_at=TARGET,
        publication_cutoff=TARGET + timedelta(minutes=10),
        evaluated_at=TARGET + timedelta(minutes=11),
        contract_kind="AT",
        terms_raw=TERMS,
    )
    args.update(kwargs)
    return module.select_miami_at_value(c, **args)


@pytest.mark.parametrize("age", [0, 3, 60])
def test_inclusive_selection_and_no_rerounding(age):
    result = select(capture(age))
    assert result.age_seconds == age * 60
    assert result.value_f == Decimal("83.84")
    assert result.selected_event_at == TARGET - timedelta(minutes=age)
    assert result.status == "CONDITIONAL_SELECTION_NOT_CERTIFIED"
    assert not result.publication_adapter_verified and not result.settlement_certified


def test_old_point_beyond_sixty_minutes_cannot_be_carried_forward():
    c = capture(None)
    old = replace(
        c.points[0],
        event_at=TARGET - timedelta(minutes=61),
        value_f=Decimal("90.00"),
        status="normal",
        contributors=5,
    )
    result = select(replace(c, points=(old,) + c.points), publications(c))
    assert result.status == "NO_CANONICAL_POINT_RULE_7_1_REQUIRED"
    assert result.value_f is None


def test_greatest_event_not_latest_publication_governs():
    c = capture()
    c = replace(
        c,
        points=c.points[:-2]
        + (
            replace(c.points[-2], value_f=Decimal("99.00"), status="degraded", contributors=4),
            c.points[-1],
        ),
    )
    pubs = list(publications(c))
    pubs[-2] = replace(pubs[-2], published_at=TARGET + timedelta(minutes=9))
    result = select(c, tuple(pubs))
    assert result.value_f == Decimal("83.84")
    assert result.selected_event_at == TARGET


def test_delayed_publication_is_valid_when_known_by_cutoff():
    c = capture()
    pubs = list(publications(c))
    pubs[-1] = replace(pubs[-1], published_at=TARGET + timedelta(minutes=8))
    assert select(c, tuple(pubs)).value_f == Decimal("83.84")
    with pytest.raises(ValueError, match="PUBLICATION_OUTSIDE"):
        select(c, tuple(pubs), publication_cutoff=TARGET + timedelta(minutes=7))


def test_missing_publication_is_not_replaced_by_event_or_receipt_time():
    with pytest.raises(ValueError, match="PUBLICATION_EVIDENCE_REQUIRED"):
        select(pubs=())


@pytest.mark.parametrize(
    "failure", ["missing", "out_of_order", "duplicate", "incomplete", "quorum", "config"]
)
def test_unknown_window_cannot_be_inferred_from_sparse_values(failure):
    c = capture()
    if failure == "missing":
        c = replace(c, points=c.points[:-1])
    elif failure == "out_of_order":
        c = replace(c, points=tuple(reversed(c.points)))
    elif failure == "duplicate":
        c = replace(c, points=c.points + (c.points[-1],))
    else:
        point = c.points[-1]
        if failure == "incomplete":
            point = replace(point, status="incomplete", value_f=None)
        elif failure == "quorum":
            point = replace(point, contributors=3)
        else:
            point = replace(point, configuration_published_by_event=False)
        c = replace(c, points=c.points[:-1] + (point,))
    with pytest.raises(ValueError):
        select(
            c,
            publications(capture())
            if failure in {"missing", "out_of_order", "duplicate"}
            else publications(c),
        )


def test_restatement_and_wrong_evidence_hash_fail():
    c = capture()
    for changes in (
        {"kind": "RESTATEMENT"},
        {"evidence_sha256": "0" * 64},
        {"point_sha256": "0" * 64},
    ):
        pubs = list(publications(c))
        pubs[-1] = replace(pubs[-1], **changes)
        with pytest.raises(ValueError, match="INITIAL_PUBLICATION_BINDING"):
            select(c, tuple(pubs))


def test_before_determination_and_unsupported_in_contract_fail():
    with pytest.raises(ValueError, match="TARGET_OR_CUTOFF"):
        select(publication_cutoff=TARGET + timedelta(seconds=299))
    with pytest.raises(ValueError, match="SUPPORTED_TERMS"):
        select(contract_kind="IN")


def test_exact_target_only_requires_its_own_evidence():
    c = capture()
    c = replace(c, points=(c.points[-1],))
    result = select(c)
    assert result.value_f == Decimal("83.84")
    assert result.coverage_minutes == 1
    assert len(result.publication_evidence_hashes) == 1


def test_irrelevant_older_incomplete_point_needs_no_publication():
    c = capture()
    old = replace(c.points[-2], status="incomplete", configuration_published_by_event=False)
    c = replace(c, points=(old, c.points[-1]))
    assert select(c, publications(c)[-1:]).coverage_minutes == 1


def test_fallback_requires_each_newer_minute_and_publication():
    c = capture(3)
    suffix = replace(c, points=c.points[-4:])
    result = select(suffix)
    assert result.age_seconds == 180 and result.coverage_minutes == 4
    with pytest.raises(ValueError, match="COMPLETE_MINUTE_COVERAGE"):
        select(replace(suffix, points=suffix.points[:2] + suffix.points[3:]), publications(suffix))
    with pytest.raises(ValueError, match="PUBLICATION_EVIDENCE_REQUIRED"):
        select(suffix, publications(suffix)[:-1])


def test_no_eligible_result_requires_all_sixty_one_minutes():
    c = capture(None)
    assert select(c).coverage_minutes == 61
    with pytest.raises(ValueError, match="COMPLETE_MINUTE_COVERAGE"):
        select(replace(c, points=c.points[1:]))
