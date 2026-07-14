from datetime import UTC
from decimal import Decimal
from typing import Any

from kalshi_predictor.utils.decimals import to_decimal
from kalshi_predictor.utils.time import utc_now


def explain_risks(
    ranking: Any | None,
    snapshot: Any | None = None,
    *,
    fresh_data_minutes: int = 15,
) -> dict[str, Any]:
    if ranking is None:
        return {
            "level": "No Trade",
            "top_risk": "No ranked opportunity is available yet.",
            "risks": ["Run an opportunity scan before reviewing this market."],
            "badges": [{"label": "No Trade", "kind": "neutral"}],
        }

    risks: list[str] = []
    badges: list[dict[str, str]] = []
    spread = to_decimal(_field(ranking, "spread"))
    edge = to_decimal(_field(ranking, "estimated_edge")) or Decimal("0")
    liquidity_score = to_decimal(_field(ranking, "liquidity_score")) or Decimal("0")
    score = to_decimal(_field(ranking, "opportunity_score")) or Decimal("0")
    freshness = data_freshness(snapshot, fresh_data_minutes=fresh_data_minutes)

    if spread is not None and spread >= Decimal("0.10"):
        risks.append(
            "The spread is wide, so the paper edge may disappear if the order does not "
            "fill near the expected price."
        )
        badges.append({"label": "High Spread", "kind": "caution"})

    if edge < Decimal("0.03"):
        risks.append("The estimated edge is thin, leaving little room for fees or slippage.")
        badges.append({"label": "Low Edge", "kind": "caution"})

    if liquidity_score < Decimal("30"):
        risks.append("Liquidity looks weak, so an order may be harder to fill cleanly.")
        badges.append({"label": "Low Liquidity", "kind": "caution"})

    if freshness["badge"] == "Stale Data":
        risks.append(freshness["text"])
        badges.append({"label": "Stale Data", "kind": "risk"})

    if score < Decimal("40"):
        badges.append({"label": "Risky", "kind": "risk"})
    elif risks:
        badges.append({"label": "Caution", "kind": "caution"})
    else:
        badges.append({"label": "Good", "kind": "good"})
        risks.append("No major local guardrail concern stands out from the stored data.")

    return {
        "level": badges[-1]["label"],
        "top_risk": risks[0],
        "risks": risks,
        "badges": _dedupe_badges(badges),
    }


def data_freshness(
    snapshot: Any | None,
    *,
    fresh_data_minutes: int = 15,
) -> dict[str, str]:
    if snapshot is None:
        return {
            "text": "No market snapshot is available, so the data freshness is unknown.",
            "badge": "Stale Data",
            "kind": "risk",
        }

    captured_at = _field(snapshot, "captured_at")
    if captured_at is None:
        return {
            "text": "The latest snapshot has no timestamp.",
            "badge": "Stale Data",
            "kind": "risk",
        }
    if captured_at.tzinfo is None:
        captured_at = captured_at.replace(tzinfo=UTC)
    age_minutes = (utc_now() - captured_at).total_seconds() / 60
    if age_minutes > fresh_data_minutes:
        return {
            "text": (
                f"Latest data is about {age_minutes:.0f} minutes old; "
                f"freshness limit is {fresh_data_minutes} minutes."
            ),
            "badge": "Stale Data",
            "kind": "risk",
        }
    return {
        "text": f"Latest data is fresh at about {max(age_minutes, 0):.0f} minutes old.",
        "badge": "Good",
        "kind": "good",
    }


def _field(row: Any, name: str) -> Any:
    if isinstance(row, dict):
        return row.get(name)
    return getattr(row, name, None)


def _dedupe_badges(badges: list[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[str] = set()
    unique: list[dict[str, str]] = []
    for badge in badges:
        if badge["label"] in seen:
            continue
        seen.add(badge["label"])
        unique.append(badge)
    return unique
