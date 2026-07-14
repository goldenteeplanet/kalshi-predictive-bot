import re
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from kalshi_predictor.utils.decimals import to_decimal
from kalshi_predictor.utils.time import utc_now

_CRYPTO_ASSET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("Bitcoin", re.compile(r"(?<![a-z0-9])(?:bitcoin|btc)(?![a-z0-9])")),
    ("Ethereum", re.compile(r"(?<![a-z0-9])(?:ethereum|ether|eth)(?![a-z0-9])")),
    ("Solana", re.compile(r"(?<![a-z0-9])(?:solana|sol)(?![a-z0-9])")),
    ("XRP", re.compile(r"(?<![a-z0-9])xrp(?![a-z0-9])")),
    ("Dogecoin", re.compile(r"(?<![a-z0-9])(?:dogecoin|doge)(?![a-z0-9])")),
)
_CRYPTO_SERIES_TOKENS = ("kxbtc", "kxeth", "kxcrypto", "kxsol", "kxxrp", "kxdoge")


def summarize_market_title(title: str) -> str:
    text = " ".join(str(title or "").replace("\n", " ").split())
    asset = _crypto_asset_from_text(text)
    if asset == "Bitcoin":
        return "Bitcoin Price Market"
    if asset == "Ethereum":
        return "Ethereum Price Market"
    lower = text.lower()
    if any(token in lower for token in ("rain", "temperature", "snow", "weather")):
        return "Weather: Rain / Temperature Market"
    if any(token in lower for token in ("cpi", "fed", "inflation", "rates", "econom")):
        return "Economic Data Market"
    if "runs scored" in lower or ("mlb" in lower and "run" in lower):
        return "MLB Multi-Game Runs Market"
    if any(token in lower for token in ("nfl", "nba", "mlb", "nhl", "sports")):
        return "Sports Market"
    if "ensemble" in lower:
        return "Ensemble Model Market"
    cleaned = text
    for prefix in ("yes ", "no ", "will "):
        if cleaned.lower().startswith(prefix):
            cleaned = cleaned[len(prefix) :]
            break
    if "," in cleaned and len(cleaned) > 72:
        cleaned = cleaned.split(",", 1)[0]
    if len(cleaned) > 86:
        cleaned = f"{cleaned[:83].rstrip()}..."
    return cleaned or "Market"


def classify_market_category(title: str, series_ticker: str | None = None) -> str:
    text = f"{title or ''} {series_ticker or ''}"
    lower = text.lower()
    if any(token in lower for token in ("ensemble", "model tournament")):
        return "Ensemble"
    if _crypto_asset_from_text(text) or "crypto" in lower or _has_crypto_series_token(text):
        return "Crypto"
    if any(token in lower for token in ("rain", "temperature", "snow", "weather", "hurricane")):
        return "Weather"
    if any(token in lower for token in ("cpi", "fed", "inflation", "rates", "econom")):
        return "Economics"
    if any(
        token in lower
        for token in (
            "kxwc",
            "kxses",
            "kxsports",
            "nfl",
            "nba",
            "mlb",
            "nhl",
            "sports",
            "runs scored",
        )
    ):
        return "Sports"
    return "General"


def category_badge(category: str) -> str:
    labels = {
        "Sports": "Sports",
        "Weather": "Weather",
        "Crypto": "Crypto",
        "Economics": "Economics",
        "Ensemble": "Ensemble",
        "General": "General",
    }
    return labels.get(category, "General")


def _crypto_asset_from_text(text: str) -> str | None:
    lower = str(text or "").lower()
    for asset, pattern in _CRYPTO_ASSET_PATTERNS:
        if pattern.search(lower):
            return asset
    return None


def _has_crypto_series_token(text: str) -> bool:
    compact = re.sub(r"[^a-z0-9]", "", str(text or "").lower())
    return any(token in compact for token in _CRYPTO_SERIES_TOKENS)


def recommendation_label(side: str | None) -> str:
    if side == "BUY_YES":
        return "BUY YES"
    if side == "BUY_NO":
        return "BUY NO"
    return "NO TRADE"


def traffic_light_label(
    *,
    opportunity_score: Any,
    edge: Any,
    spread: Any = None,
    liquidity: Any = None,
    confidence: Any = None,
    is_fresh: bool = True,
    max_spread: Decimal = Decimal("0.10"),
) -> dict[str, str]:
    score_value = _decimal(opportunity_score)
    edge_value = _decimal(edge)
    spread_value = to_decimal(spread)
    liquidity_value = _decimal(liquidity)
    confidence_value = _decimal(confidence)
    high_spread = spread_value is not None and spread_value > max_spread
    low_liquidity = liquidity_value < Decimal("25")
    no_confidence = confidence_value < Decimal("20")
    if (
        not is_fresh
        or score_value < Decimal("60")
        or high_spread
        or low_liquidity
        or no_confidence
    ):
        return {"label": "Avoid", "kind": "avoid"}
    if (
        score_value >= Decimal("80")
        and edge_value >= Decimal("0.05")
        and is_fresh
        and not high_spread
    ):
        return {"label": "Strong Opportunity", "kind": "strong"}
    if score_value >= Decimal("60") or edge_value >= Decimal("0.03"):
        return {"label": "Watchlist", "kind": "watchlist"}
    return {"label": "Avoid", "kind": "avoid"}


def risk_meter(
    *,
    opportunity_score: Any,
    edge: Any,
    spread: Any = None,
    liquidity: Any = None,
    confidence: Any = None,
    is_fresh: bool = True,
    max_spread: Decimal = Decimal("0.10"),
) -> dict[str, Any]:
    score_value = _decimal(opportunity_score)
    edge_value = _decimal(edge)
    spread_value = to_decimal(spread)
    liquidity_value = _decimal(liquidity)
    confidence_value = _decimal(confidence)
    factors: list[dict[str, str]] = []
    risk_points = 0

    if is_fresh:
        factors.append({"kind": "good", "label": "Fresh data"})
    else:
        factors.append({"kind": "warn", "label": "Stale data"})
        risk_points += 3

    if edge_value >= Decimal("0.05"):
        factors.append({"kind": "good", "label": "Strong edge"})
    elif edge_value >= Decimal("0.03"):
        factors.append({"kind": "warn", "label": "Thin edge"})
        risk_points += 1
    else:
        factors.append({"kind": "warn", "label": "Weak edge"})
        risk_points += 2

    if liquidity_value < Decimal("30"):
        factors.append({"kind": "warn", "label": "Low liquidity"})
        risk_points += 2
    else:
        factors.append({"kind": "good", "label": "Usable liquidity"})

    if spread_value is not None and spread_value > max_spread:
        factors.append({"kind": "warn", "label": "Wide spread"})
        risk_points += 2
    else:
        factors.append({"kind": "good", "label": "Acceptable spread"})

    if confidence_value < Decimal("30"):
        factors.append({"kind": "warn", "label": "Weak model confidence"})
        risk_points += 2
    if score_value < Decimal("60"):
        risk_points += 2

    if risk_points >= 5:
        level = "High"
    elif risk_points >= 2:
        level = "Medium"
    else:
        level = "Low"
    filled = max(1, min(10, 10 - risk_points))
    return {
        "level": level,
        "filled": filled,
        "bars": "#" * filled + "-" * (10 - filled),
        "factors": factors,
    }


def format_edge_cents(value: Any) -> str:
    decimal_value = to_decimal(value)
    if decimal_value is None:
        return "n/a"
    return f"{(decimal_value * Decimal('100')).quantize(Decimal('0.1'))}c"


def format_probability(value: Any) -> str:
    decimal_value = to_decimal(value)
    if decimal_value is None:
        return "n/a"
    return f"{(decimal_value * Decimal('100')).quantize(Decimal('0.1'))}%"


def format_time_remaining(value: Any) -> str:
    minutes = to_decimal(value)
    if minutes is None:
        return "n/a"
    if minutes < 0:
        return "closed"
    if minutes < Decimal("60"):
        return f"{minutes.quantize(Decimal('1'))}m"
    hours = minutes / Decimal("60")
    if hours < Decimal("48"):
        return f"{hours.quantize(Decimal('0.1'))}h"
    days = hours / Decimal("24")
    return f"{days.quantize(Decimal('0.1'))}d"


def is_fresh_timestamp(value: Any, *, fresh_data_minutes: int = 15) -> bool:
    if value is None:
        return False
    timestamp = value
    if isinstance(timestamp, str):
        try:
            timestamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except ValueError:
            return False
    if not isinstance(timestamp, datetime):
        return False
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    age_minutes = (utc_now() - timestamp.astimezone(UTC)).total_seconds() / 60
    return age_minutes <= fresh_data_minutes


def _decimal(value: Any) -> Decimal:
    return to_decimal(value) or Decimal("0")
