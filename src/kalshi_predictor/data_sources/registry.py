"""One immutable provider catalog; no automatic API connections or upgrades.

Feature/family labels describe intended research scope, not proof that a source
currently improves a model or is eligible as settlement truth. Provider-specific
adapters must validate exact semantics. Freshness is deliberately unconfigured
until an explicit use-specific policy is supplied to assess_source.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from types import MappingProxyType

from kalshi_predictor.data_sources.contracts import (
    ConfigurationSource,
    CredentialMetadata,
    CredentialStatus,
    SourceDefinition,
)

_DEFINITIONS = (
    SourceDefinition(
        "BLS",
        "economics",
        1,
        True,
        ("BLS_API_KEY", "BLA_API_KEY", "BLA_KEY", "BLA"),
        ("release_value", "release_identity", "revision_metadata"),
        ("inflation", "labor"),
    ),
    SourceDefinition(
        "FRED",
        "economics",
        1,
        True,
        ("FRED_API_KEY",),
        ("economic_context",),
        ("inflation", "rates", "labor", "gdp"),
    ),
    SourceDefinition(
        "ALFRED",
        "economics",
        1,
        True,
        ("FRED_API_KEY", "ALFRED_API_KEY"),
        ("point_in_time_vintage",),
        ("inflation", "rates", "labor", "gdp"),
    ),
    SourceDefinition(
        "BEA",
        "economics",
        2,
        True,
        ("BEA_API_KEY", "BEA_USER_ID"),
        ("national_accounts",),
        ("gdp", "personal_income", "pce"),
    ),
    SourceDefinition(
        "SYNOPTIC",
        "weather",
        1,
        True,
        ("SYNOPTIC_DATA_TOKEN", "SYNOPTIC_TOKEN", "SYNOPTIC_API_KEY"),
        ("station_metadata", "observation", "quality_control"),
        ("weather",),
    ),
    SourceDefinition(
        "DATABENTO",
        "market_data",
        2,
        True,
        ("DATABENTO_API_KEY",),
        ("returns", "volatility", "depth", "spread", "term_structure"),
        ("rates", "equities", "commodities"),
    ),
    SourceDefinition(
        "THE_ODDS_API",
        "sports",
        2,
        True,
        ("THE_ODDS_API_KEY", "ODDS_API_KEY"),
        ("bookmaker_consensus", "dispersion", "line_movement"),
        ("sports_binary",),
    ),
    SourceDefinition(
        "NEWSAPI_AI",
        "news",
        2,
        True,
        ("NEWSAPI_AI_KEY", "NEWSAPI_AI_API_KEY"),
        ("timestamped_event", "semantic_event_link"),
        ("economics", "politics", "crypto"),
    ),
    SourceDefinition(
        "COINGECKO",
        "crypto",
        1,
        True,
        ("COINGECKO_API_KEY", "COINGECKO_DEMO_API_KEY", "COINGECKO_PRO_API_KEY"),
        ("aggregate_price", "volume", "cross_source_divergence"),
        ("crypto",),
    ),
    SourceDefinition(
        "ALPHA_VANTAGE",
        "market_data",
        2,
        True,
        ("ALPHA_VANTAGE_API_KEY",),
        ("lower_frequency_context", "secondary_validation"),
        ("equities", "fx", "commodities"),
    ),
    SourceDefinition(
        "UNUSUAL_WHALES",
        "market_research",
        2,
        True,
        ("UNUSUAL_WHALES_API_KEY", "UNUSUAL_WHALES_TOKEN"),
        ("options_flow", "event_context", "unusual_prediction_market_activity"),
        ("equities", "economics", "prediction_markets"),
    ),
    SourceDefinition(
        "ODDPOOL",
        "historical_corpus",
        3,
        True,
        ("ODDPOOL_API_KEY",),
        ("market_search", "historical_orderbook"),
        ("prediction_markets",),
        rights_corpus="PredictionMarketBench",
    ),
    SourceDefinition(
        "PREDICTION_MARKET_BENCH",
        "historical_corpus",
        3,
        False,
        (),
        ("historical_replay",),
        ("prediction_markets",),
        rights_corpus="PredictionMarketBench",
    ),
    SourceDefinition(
        "PREDICTION_MARKETS_PUBLIC",
        "historical_corpus",
        3,
        False,
        (),
        ("historical_replay",),
        ("prediction_markets",),
        rights_corpus="Prediction_Markets_Public",
    ),
    SourceDefinition(
        "AGENTTRADER",
        "historical_corpus",
        3,
        False,
        (),
        ("synthetic_replay",),
        ("prediction_markets",),
        rights_corpus="AgentTrader",
    ),
    SourceDefinition(
        "KALSHI",
        "public_exchange",
        1,
        False,
        (),
        ("market_metadata", "orderbook", "public_lifecycle"),
        ("prediction_markets",),
    ),
    SourceDefinition("COINBASE", "crypto", 1, False, (), ("exchange_spot_reference",), ("crypto",)),
    SourceDefinition(
        "NWS", "weather", 1, False, (), ("hourly_forecast", "station_metadata"), ("weather",)
    ),
)

PROVIDERS = MappingProxyType({item.provider_name: item for item in _DEFINITIONS})


def get_source(provider_name: str) -> SourceDefinition:
    if provider_name not in PROVIDERS:
        raise ValueError("UNREGISTERED_DATA_SOURCE")
    return PROVIDERS[provider_name]


def _alias_name(value: str) -> str:
    name = re.sub(r"[^A-Z0-9]+", "_", value.upper()).strip("_")
    return name.removesuffix("_TXT")


def credential_metadata(
    provider_name: str,
    *,
    available_aliases: Iterable[str] = (),
    configuration_source: ConfigurationSource = ConfigurationSource.NOT_APPLICABLE,
) -> CredentialMetadata:
    """Consume detected credential NAMES only, never mappings or secret values."""
    definition = get_source(provider_name)
    if not isinstance(configuration_source, ConfigurationSource):
        raise ValueError("CANONICAL_CONFIGURATION_SOURCE_REQUIRED")
    if isinstance(available_aliases, Mapping | str | bytes):
        raise ValueError("CREDENTIAL_NAMES_ONLY_REQUIRED")
    names = tuple(available_aliases)
    if any(not isinstance(name, str) or len(name) > 100 for name in names):
        raise ValueError("CREDENTIAL_ALIAS_METADATA_INVALID")
    if not definition.credential_required:
        return CredentialMetadata(
            provider_name,
            CredentialStatus.NOT_REQUIRED,
            None,
            ConfigurationSource.NOT_APPLICABLE,
            data_access_available=True,
        )
    for preferred in definition.credential_aliases:
        for original in names:
            if _alias_name(original) == preferred:
                if configuration_source == ConfigurationSource.NOT_APPLICABLE:
                    raise ValueError("CONFIGURED_CREDENTIAL_SOURCE_METADATA_REQUIRED")
                notice = "LEGACY_BLA_ALIAS_ACCEPTED" if preferred.startswith("BLA") else None
                management_only = provider_name == "SYNOPTIC" and preferred == "SYNOPTIC_API_KEY"
                if management_only:
                    notice = "MANAGEMENT_KEY_ONLY_DATA_TOKEN_REQUIRED"
                return CredentialMetadata(
                    provider_name,
                    CredentialStatus.CONFIGURED,
                    original,
                    configuration_source,
                    notice,
                    data_access_available=not management_only,
                )
    return CredentialMetadata(
        provider_name, CredentialStatus.NOT_CONFIGURED, None, ConfigurationSource.NOT_APPLICABLE
    )
