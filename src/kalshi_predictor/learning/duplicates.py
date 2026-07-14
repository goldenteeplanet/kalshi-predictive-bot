from __future__ import annotations

from collections.abc import Iterable
from datetime import timedelta

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from kalshi_predictor.data.schema import PaperOrder
from kalshi_predictor.utils.time import utc_now

CandidateKey = tuple[str, str, str]


def candidate_key(*, ticker: str, model_name: str, side: str) -> CandidateKey:
    return (ticker, model_name, side)


def recent_duplicate_order(
    session: Session,
    *,
    ticker: str,
    model_name: str,
    side: str,
    cooldown_hours: int,
) -> PaperOrder | None:
    cutoff = utc_now() - timedelta(hours=max(0, cooldown_hours))
    for item in session.new:
        if (
            isinstance(item, PaperOrder)
            and item.ticker == ticker
            and item.model_name == model_name
            and item.side == side
            and item.created_at >= cutoff
        ):
            return item
    return session.scalar(
        select(PaperOrder)
        .where(
            PaperOrder.ticker == ticker,
            PaperOrder.model_name == model_name,
            PaperOrder.side == side,
            PaperOrder.created_at >= cutoff,
        )
        .order_by(desc(PaperOrder.created_at), desc(PaperOrder.id))
        .limit(1)
    )


def is_duplicate_candidate(
    session: Session,
    *,
    ticker: str,
    model_name: str,
    side: str,
    cooldown_hours: int,
    pending_keys: Iterable[CandidateKey] = (),
) -> bool:
    key = candidate_key(ticker=ticker, model_name=model_name, side=side)
    if key in set(pending_keys):
        return True
    return (
        recent_duplicate_order(
            session,
            ticker=ticker,
            model_name=model_name,
            side=side,
            cooldown_hours=cooldown_hours,
        )
        is not None
    )
