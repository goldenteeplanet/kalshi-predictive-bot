from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.utils.decimals import decimal_to_str, to_decimal
from kalshi_predictor.utils.time import utc_now


@dataclass(frozen=True)
class ForumConsensusAssessment:
    enabled: bool
    available: bool
    qualifies: bool
    label: str
    summary: str
    participant_count: int
    winner_count: int
    average_win_rate: str | None
    longshot_price: str | None
    consensus_score: str | None
    is_longshot: bool
    is_recent: bool
    badge: dict[str, str] | None


def assess_forum_consensus(
    signal: Any | None,
    *,
    settings: Settings | None = None,
    current_price: Any = None,
) -> ForumConsensusAssessment:
    resolved_settings = settings or get_settings()
    if not resolved_settings.forum_consensus_enabled:
        return _empty("Forum consensus disabled", enabled=False)
    if signal is None:
        return _empty("No forum consensus signal has been imported for this market.")

    observed_at = _datetime(_field(signal, "observed_at"))
    participant_count = _int(_field(signal, "participant_count"))
    winner_count = _int(_field(signal, "winner_count"))
    average_win_rate = to_decimal(_field(signal, "average_win_rate"))
    price = to_decimal(_field(signal, "longshot_price")) or to_decimal(current_price)
    stored_score = to_decimal(_field(signal, "consensus_score"))

    max_age = timedelta(hours=resolved_settings.forum_consensus_max_age_hours)
    is_recent = observed_at is not None and utc_now() - _aware(observed_at) <= max_age
    enough_winners = winner_count >= resolved_settings.forum_consensus_min_winners
    enough_win_rate = (
        average_win_rate is not None
        and average_win_rate >= resolved_settings.forum_consensus_min_win_rate
    )
    is_longshot = (
        price is not None and price <= resolved_settings.forum_consensus_longshot_max_price
    )
    qualifies = is_recent and enough_winners and enough_win_rate and is_longshot
    consensus_score = stored_score or score_forum_consensus(
        participant_count=participant_count,
        winner_count=winner_count,
        average_win_rate=average_win_rate,
        is_longshot=is_longshot,
        settings=resolved_settings,
    )

    if qualifies:
        label = "Longshot Watch"
        summary = (
            "Forum consensus is worth a closer look: "
            f"{winner_count} historically winning participants like this longshot, "
            f"with an average win rate of {_percent(average_win_rate)}."
        )
        badge = {"label": "Forum Consensus", "kind": "caution"}
    else:
        label = "Weak or stale consensus"
        summary = _non_qualifying_summary(
            is_recent=is_recent,
            enough_winners=enough_winners,
            enough_win_rate=enough_win_rate,
            is_longshot=is_longshot,
            winner_count=winner_count,
            average_win_rate=average_win_rate,
            price=price,
        )
        badge = {"label": "Consensus Watch", "kind": "neutral"}

    return ForumConsensusAssessment(
        enabled=True,
        available=True,
        qualifies=qualifies,
        label=label,
        summary=summary,
        participant_count=participant_count,
        winner_count=winner_count,
        average_win_rate=decimal_to_str(average_win_rate),
        longshot_price=decimal_to_str(price),
        consensus_score=decimal_to_str(consensus_score),
        is_longshot=is_longshot,
        is_recent=is_recent,
        badge=badge,
    )


def assessment_to_dict(assessment: ForumConsensusAssessment) -> dict[str, Any]:
    return asdict(assessment)


def score_forum_consensus(
    *,
    participant_count: int,
    winner_count: int,
    average_win_rate: Decimal | None,
    is_longshot: bool,
    settings: Settings | None = None,
) -> Decimal:
    resolved_settings = settings or get_settings()
    if participant_count <= 0 or winner_count <= 0 or average_win_rate is None:
        return Decimal("0")
    winner_ratio = min(
        Decimal(winner_count) / Decimal(max(resolved_settings.forum_consensus_min_winners, 1)),
        Decimal("2"),
    )
    participant_ratio = min(Decimal(winner_count) / Decimal(participant_count), Decimal("1"))
    win_rate_component = min(average_win_rate / Decimal("0.75"), Decimal("1.5"))
    longshot_bonus = Decimal("15") if is_longshot else Decimal("0")
    score = (
        Decimal("35") * min(winner_ratio / Decimal("2"), Decimal("1"))
        + Decimal("25") * participant_ratio
        + Decimal("25") * min(win_rate_component / Decimal("1.5"), Decimal("1"))
        + longshot_bonus
    )
    return min(score, Decimal("100")).quantize(Decimal("0.1"))


def _empty(summary: str, *, enabled: bool = True) -> ForumConsensusAssessment:
    return ForumConsensusAssessment(
        enabled=enabled,
        available=False,
        qualifies=False,
        label="No consensus",
        summary=summary,
        participant_count=0,
        winner_count=0,
        average_win_rate=None,
        longshot_price=None,
        consensus_score=None,
        is_longshot=False,
        is_recent=False,
        badge=None,
    )


def _non_qualifying_summary(
    *,
    is_recent: bool,
    enough_winners: bool,
    enough_win_rate: bool,
    is_longshot: bool,
    winner_count: int,
    average_win_rate: Decimal | None,
    price: Decimal | None,
) -> str:
    misses: list[str] = []
    if not is_recent:
        misses.append("the signal is stale")
    if not enough_winners:
        misses.append(f"only {winner_count} historically winning participants are included")
    if not enough_win_rate:
        misses.append(f"the average win rate is {_percent(average_win_rate)}")
    if not is_longshot:
        misses.append(f"the observed price is {_price(price)}, not a configured longshot")
    return "Forum consensus is present but not strong enough yet because " + ", ".join(misses) + "."


def _field(row: Any, name: str) -> Any:
    if isinstance(row, dict):
        return row.get(name)
    return getattr(row, name, None)


def _datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    return None


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _percent(value: Decimal | None) -> str:
    if value is None:
        return "n/a"
    return f"{(value * Decimal('100')).quantize(Decimal('0.1'))}%"


def _price(value: Decimal | None) -> str:
    if value is None:
        return "n/a"
    return f"{(value * Decimal('100')).quantize(Decimal('0.1'))} cents"

