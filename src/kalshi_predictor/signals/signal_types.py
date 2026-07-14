from dataclasses import dataclass, field
from typing import Any

WEATHER_SIGNAL = "Weather Signal"
CRYPTO_SIGNAL = "Crypto Signal"
ECONOMIC_SIGNAL = "Economic Signal"
MARKET_DIVERGENCE_SIGNAL = "Market Divergence Signal"
LIQUIDITY_SIGNAL = "Liquidity Signal"
SPREAD_COMPRESSION_SIGNAL = "Spread Compression Signal"
MOMENTUM_SIGNAL = "Momentum Signal"
ENSEMBLE_AGREEMENT_SIGNAL = "Ensemble Agreement Signal"
OPPORTUNITY_SCORE_SIGNAL = "Opportunity Score Signal"
FRESH_DATA_SIGNAL = "Fresh Data Signal"
NEWS_SIGNAL = "News Signal"
BREAKING_NEWS_SIGNAL = "Breaking News Signal"
ECONOMIC_NEWS_SIGNAL = "Economic News Signal"
CRYPTO_NEWS_SIGNAL = "Crypto News Signal"
WEATHER_NEWS_SIGNAL = "Weather News Signal"
SPORTS_NEWS_SIGNAL = "Sports News Signal"
SPORTS_SIGNAL = "Sports Signal"
MLB_SIGNAL = "MLB Signal"
NBA_SIGNAL = "NBA Signal"
NFL_SIGNAL = "NFL Signal"
NHL_SIGNAL = "NHL Signal"
TEAM_STRENGTH_SIGNAL = "Team Strength Signal"
INJURY_SIGNAL = "Injury Signal"
REST_SIGNAL = "Rest Signal"
ODDS_SIGNAL = "Odds Signal"
WEATHER_SPORTS_SIGNAL = "Weather Sports Signal"
TRAVEL_SIGNAL = "Travel Signal"
MICROSTRUCTURE_SIGNAL = "Microstructure Signal"
SPREAD_TIGHTENING_SIGNAL = "Spread Tightening Signal"
LIQUIDITY_IMPROVEMENT_SIGNAL = "Liquidity Improvement Signal"
ORDERBOOK_IMBALANCE_SIGNAL = "Orderbook Imbalance Signal"
PRICE_DISLOCATION_SIGNAL = "Price Dislocation Signal"
LATE_MOVE_SIGNAL = "Late Move Signal"
SMART_MONEY_HEURISTIC_SIGNAL = "Smart Money Heuristic Signal"
META_SELECTION_SIGNAL = "Meta Selection Signal"
MODEL_TRUST_SIGNAL = "Model Trust Signal"
MODEL_DISAGREEMENT_SIGNAL = "Model Disagreement Signal"
FALLBACK_SIGNAL = "Fallback Signal"
SPECIALIZED_MODEL_ADVANTAGE_SIGNAL = "Specialized Model Advantage Signal"


@dataclass(frozen=True)
class SignalDefinition:
    signal_name: str
    category: str
    description: str
    status: str = "Active"
    metadata: dict[str, Any] = field(default_factory=dict)


BUILTIN_SIGNALS = (
    SignalDefinition(
        WEATHER_SIGNAL,
        "Weather",
        "Weather-linked model or weather feature evidence supports the forecast.",
        metadata={"models": ["weather_v1", "weather_v2"]},
    ),
    SignalDefinition(
        CRYPTO_SIGNAL,
        "Crypto",
        "Crypto-linked model or crypto feature evidence supports the forecast.",
        metadata={"models": ["crypto_v1", "crypto_v2"]},
    ),
    SignalDefinition(
        ECONOMIC_SIGNAL,
        "Economic",
        "Economic model or external economic feature evidence supports the forecast.",
        metadata={"models": ["economic_v1"]},
    ),
    SignalDefinition(
        MARKET_DIVERGENCE_SIGNAL,
        "Market",
        "Model probability diverges from the current market-implied price.",
    ),
    SignalDefinition(
        LIQUIDITY_SIGNAL,
        "Market Quality",
        "Liquidity is high enough for the market to be reviewable.",
    ),
    SignalDefinition(
        SPREAD_COMPRESSION_SIGNAL,
        "Market Quality",
        "Bid/ask spread is tight enough that edge is less likely to disappear.",
    ),
    SignalDefinition(
        MOMENTUM_SIGNAL,
        "Momentum",
        "Recent linked external data or model features indicate directional momentum.",
    ),
    SignalDefinition(
        ENSEMBLE_AGREEMENT_SIGNAL,
        "Model",
        "Multiple component models point in the same direction.",
    ),
    SignalDefinition(
        OPPORTUNITY_SCORE_SIGNAL,
        "Opportunity",
        "Opportunity score is high enough to support review.",
    ),
    SignalDefinition(
        FRESH_DATA_SIGNAL,
        "Data Quality",
        "Latest market snapshot is fresh enough for paper/demo review.",
    ),
    SignalDefinition(
        NEWS_SIGNAL,
        "News",
        "Recent local news is linked to the market and may affect the forecast.",
        metadata={"models": ["news_v1"]},
    ),
    SignalDefinition(
        BREAKING_NEWS_SIGNAL,
        "News",
        "High-importance or breaking news is linked to the market.",
        metadata={"models": ["news_v1"]},
    ),
    SignalDefinition(
        ECONOMIC_NEWS_SIGNAL,
        "News",
        "Fed, CPI, jobs, rates, or other economic news is linked to the market.",
        metadata={"models": ["news_v1", "economic_v1"]},
    ),
    SignalDefinition(
        CRYPTO_NEWS_SIGNAL,
        "News",
        "BTC, ETH, or broader crypto news is linked to the market.",
        metadata={"models": ["news_v1", "crypto_v2"]},
    ),
    SignalDefinition(
        WEATHER_NEWS_SIGNAL,
        "News",
        "Weather alert or weather-related news is linked to the market.",
        metadata={"models": ["news_v1", "weather_v2"]},
    ),
    SignalDefinition(
        SPORTS_NEWS_SIGNAL,
        "News",
        "Sports news or injury information is linked to the market.",
        metadata={"models": ["news_v1"]},
    ),
    SignalDefinition(
        SPORTS_SIGNAL,
        "Sports",
        "Linked sports game features support the forecast.",
        metadata={"models": ["sports_v1", "mlb_v1", "nba_v1", "nfl_v1", "nhl_v1"]},
    ),
    SignalDefinition(
        MLB_SIGNAL,
        "Sports",
        "MLB-specific game intelligence supports the forecast.",
        metadata={"models": ["mlb_v1", "sports_v1"]},
    ),
    SignalDefinition(
        NBA_SIGNAL,
        "Sports",
        "NBA-specific game intelligence supports the forecast.",
        metadata={"models": ["nba_v1", "sports_v1"]},
    ),
    SignalDefinition(
        NFL_SIGNAL,
        "Sports",
        "NFL-specific game intelligence supports the forecast.",
        metadata={"models": ["nfl_v1", "sports_v1"]},
    ),
    SignalDefinition(
        NHL_SIGNAL,
        "Sports",
        "NHL-specific game intelligence supports the forecast.",
        metadata={"models": ["nhl_v1", "sports_v1"]},
    ),
    SignalDefinition(
        TEAM_STRENGTH_SIGNAL,
        "Sports",
        "Team strength edge contributes to the sports forecast.",
        metadata={"models": ["sports_v1", "mlb_v1", "nba_v1", "nfl_v1", "nhl_v1"]},
    ),
    SignalDefinition(
        INJURY_SIGNAL,
        "Sports",
        "Injury context contributes to the sports forecast.",
        metadata={"models": ["sports_v1", "mlb_v1", "nba_v1", "nfl_v1", "nhl_v1"]},
    ),
    SignalDefinition(
        REST_SIGNAL,
        "Sports",
        "Rest differential contributes to the sports forecast.",
        metadata={"models": ["sports_v1", "mlb_v1", "nba_v1", "nfl_v1", "nhl_v1"]},
    ),
    SignalDefinition(
        ODDS_SIGNAL,
        "Sports",
        "No-vig public/manual odds context contributes to the sports forecast.",
        metadata={"models": ["sports_v1", "mlb_v1", "nba_v1", "nfl_v1", "nhl_v1"]},
    ),
    SignalDefinition(
        WEATHER_SPORTS_SIGNAL,
        "Sports",
        "Weather context contributes to the sports forecast.",
        metadata={"models": ["sports_v1", "mlb_v1", "nfl_v1"]},
    ),
    SignalDefinition(
        TRAVEL_SIGNAL,
        "Sports",
        "Travel context contributes to the sports forecast.",
        metadata={"models": ["sports_v1", "mlb_v1", "nba_v1", "nfl_v1", "nhl_v1"]},
    ),
    SignalDefinition(
        MICROSTRUCTURE_SIGNAL,
        "Microstructure",
        "Stored market microstructure changed enough to support paper/demo review.",
        metadata={"models": ["microstructure_v1"]},
    ),
    SignalDefinition(
        SPREAD_TIGHTENING_SIGNAL,
        "Microstructure",
        "Bid/ask spread tightened, improving paper execution quality.",
        metadata={"models": ["microstructure_v1"]},
    ),
    SignalDefinition(
        LIQUIDITY_IMPROVEMENT_SIGNAL,
        "Microstructure",
        "Liquidity improved versus recent snapshots.",
        metadata={"models": ["microstructure_v1"]},
    ),
    SignalDefinition(
        ORDERBOOK_IMBALANCE_SIGNAL,
        "Microstructure",
        "Stored orderbook depth shows YES or NO pressure.",
        metadata={"models": ["microstructure_v1"]},
    ),
    SignalDefinition(
        PRICE_DISLOCATION_SIGNAL,
        "Microstructure",
        "Market price diverges from local model fair value or recent movement.",
        metadata={"models": ["microstructure_v1", "ensemble_v2"]},
    ),
    SignalDefinition(
        LATE_MOVE_SIGNAL,
        "Microstructure",
        "A late price, liquidity, or volatility move is visible near close.",
        metadata={"models": ["microstructure_v1"]},
    ),
    SignalDefinition(
        SMART_MONEY_HEURISTIC_SIGNAL,
        "Microstructure",
        "Possible informed-flow heuristic; cautious diagnostic only, never proof.",
        metadata={"models": ["microstructure_v1"]},
    ),
    SignalDefinition(
        META_SELECTION_SIGNAL,
        "Meta Model",
        "The meta model selected a trusted model for this market.",
        metadata={"models": ["meta_model_v1", "meta_ensemble_v1"]},
    ),
    SignalDefinition(
        MODEL_TRUST_SIGNAL,
        "Meta Model",
        "The selected model has a high local trust score.",
        metadata={"models": ["meta_model_v1", "meta_ensemble_v1"]},
    ),
    SignalDefinition(
        MODEL_DISAGREEMENT_SIGNAL,
        "Meta Model",
        "Candidate forecast models materially disagree.",
        metadata={"models": ["meta_model_v1", "meta_ensemble_v1"]},
    ),
    SignalDefinition(
        FALLBACK_SIGNAL,
        "Meta Model",
        "The meta selector used fallback logic because evidence was insufficient.",
        metadata={"models": ["meta_model_v1", "meta_ensemble_v1"]},
    ),
    SignalDefinition(
        SPECIALIZED_MODEL_ADVANTAGE_SIGNAL,
        "Meta Model",
        "A specialized model has enough category or signal support to beat the baseline.",
        metadata={"models": ["meta_model_v1", "meta_ensemble_v1"]},
    ),
)
