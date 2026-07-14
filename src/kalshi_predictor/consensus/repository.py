from collections.abc import Mapping
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.consensus.scoring import assess_forum_consensus, score_forum_consensus
from kalshi_predictor.data.repositories import encode_json
from kalshi_predictor.data.schema import ForumConsensusSignal
from kalshi_predictor.utils.decimals import decimal_to_str, to_decimal
from kalshi_predictor.utils.time import parse_datetime, utc_now


def insert_forum_consensus_signal(
    session: Session,
    payload: Mapping[str, Any],
    *,
    settings: Settings | None = None,
) -> ForumConsensusSignal:
    resolved_settings = settings or get_settings()
    observed_at = parse_datetime(payload.get("observed_at")) or utc_now()
    participant_count = _int(payload.get("participant_count"))
    winner_count = _int(payload.get("winner_count"))
    average_win_rate = to_decimal(payload.get("average_win_rate"))
    longshot_price = to_decimal(
        payload.get("longshot_price") or payload.get("price") or payload.get("market_price")
    )
    score = to_decimal(payload.get("consensus_score"))
    if score is None:
        score = score_forum_consensus(
            participant_count=participant_count,
            winner_count=winner_count,
            average_win_rate=average_win_rate,
            is_longshot=(
                longshot_price is not None
                and longshot_price <= resolved_settings.forum_consensus_longshot_max_price
            ),
            settings=resolved_settings,
        )
    signal = ForumConsensusSignal(
        ticker=str(payload["ticker"]),
        observed_at=observed_at,
        source=str(payload.get("source") or "manual"),
        side=_side(payload.get("side")),
        participant_count=participant_count,
        winner_count=winner_count,
        average_win_rate=decimal_to_str(average_win_rate),
        longshot_price=decimal_to_str(longshot_price),
        consensus_score=decimal_to_str(score),
        notes=str(payload.get("notes") or "") or None,
        raw_json=encode_json(dict(payload)),
        created_at=utc_now(),
    )
    session.add(signal)
    session.flush()
    return signal


def ingest_forum_consensus_payload(
    session: Session,
    payload: Mapping[str, Any],
    *,
    settings: Settings | None = None,
) -> list[ForumConsensusSignal]:
    records = payload.get("signals")
    if isinstance(records, list):
        return [
            insert_forum_consensus_signal(session, item, settings=settings)
            for item in records
            if isinstance(item, Mapping)
        ]
    return [insert_forum_consensus_signal(session, payload, settings=settings)]


def latest_consensus_for_ticker(
    session: Session,
    ticker: str,
) -> ForumConsensusSignal | None:
    return session.scalar(
        select(ForumConsensusSignal)
        .where(ForumConsensusSignal.ticker == ticker)
        .order_by(desc(ForumConsensusSignal.observed_at), desc(ForumConsensusSignal.id))
        .limit(1)
    )


def recent_consensus_signals(
    session: Session,
    *,
    limit: int = 20,
) -> list[ForumConsensusSignal]:
    return list(
        session.scalars(
            select(ForumConsensusSignal)
            .order_by(desc(ForumConsensusSignal.observed_at), desc(ForumConsensusSignal.id))
            .limit(limit)
        )
    )


def consensus_signal_row(signal: ForumConsensusSignal, *, settings: Settings | None = None) -> dict:
    assessment = assess_forum_consensus(signal, settings=settings)
    return {
        "id": signal.id,
        "ticker": signal.ticker,
        "observed_at": signal.observed_at.isoformat(),
        "source": signal.source,
        "side": signal.side,
        "participant_count": signal.participant_count,
        "winner_count": signal.winner_count,
        "average_win_rate": signal.average_win_rate,
        "longshot_price": signal.longshot_price,
        "consensus_score": signal.consensus_score,
        "notes": signal.notes,
        "assessment": assessment.summary,
        "qualifies": assessment.qualifies,
    }


def _side(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip().upper()
    if text in {"YES", "BUY_YES"}:
        return "BUY_YES"
    if text in {"NO", "BUY_NO"}:
        return "BUY_NO"
    return text or None


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
