import re
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class CryptoAsset:
    symbol: str
    name: str
    coinbase_product: str
    coingecko_id: str
    min_target_price: Decimal
    max_target_price: Decimal
    event_prefixes: tuple[str, ...]
    aliases: tuple[str, ...]

    def matches_target_price(self, value: Decimal) -> bool:
        return self.min_target_price <= value <= self.max_target_price


SUPPORTED_CRYPTO_ASSETS: tuple[CryptoAsset, ...] = (
    CryptoAsset(
        symbol="BTC",
        name="Bitcoin",
        coinbase_product="BTC-USD",
        coingecko_id="bitcoin",
        min_target_price=Decimal("10000"),
        max_target_price=Decimal("250000"),
        event_prefixes=("KXBTC",),
        aliases=("btc", "xbt", "bitcoin"),
    ),
    CryptoAsset(
        symbol="ETH",
        name="Ethereum",
        coinbase_product="ETH-USD",
        coingecko_id="ethereum",
        min_target_price=Decimal("300"),
        max_target_price=Decimal("15000"),
        event_prefixes=("KXETH",),
        aliases=("eth", "ethereum", "ether"),
    ),
    CryptoAsset(
        symbol="SOL",
        name="Solana",
        coinbase_product="SOL-USD",
        coingecko_id="solana",
        min_target_price=Decimal("10"),
        max_target_price=Decimal("500"),
        event_prefixes=("KXSOLE", "KXSOL"),
        aliases=("sol", "solana"),
    ),
    CryptoAsset(
        symbol="XRP",
        name="XRP",
        coinbase_product="XRP-USD",
        coingecko_id="ripple",
        min_target_price=Decimal("0.2"),
        max_target_price=Decimal("10"),
        event_prefixes=("KXXRP",),
        aliases=("xrp", "ripple"),
    ),
    CryptoAsset(
        symbol="DOGE",
        name="Dogecoin",
        coinbase_product="DOGE-USD",
        coingecko_id="dogecoin",
        min_target_price=Decimal("0.01"),
        max_target_price=Decimal("1"),
        event_prefixes=("KXDOGE",),
        aliases=("doge", "dogecoin"),
    ),
)

SUPPORTED_CRYPTO_SYMBOLS = tuple(asset.symbol for asset in SUPPORTED_CRYPTO_ASSETS)
DEFAULT_CRYPTO_SYMBOLS = ",".join(SUPPORTED_CRYPTO_SYMBOLS)


def supported_crypto_asset(symbol: str) -> CryptoAsset | None:
    normalized = symbol.strip().upper()
    return next((asset for asset in SUPPORTED_CRYPTO_ASSETS if asset.symbol == normalized), None)


def coingecko_id_for_symbol(symbol: str) -> str:
    asset = supported_crypto_asset(symbol)
    return asset.coingecko_id if asset else symbol.strip().lower()


def coinbase_product_for_symbol(symbol: str) -> str:
    asset = supported_crypto_asset(symbol)
    normalized = symbol.strip().upper()
    return asset.coinbase_product if asset else f"{normalized}-USD"


def symbol_for_target_price(value: Decimal) -> str | None:
    for asset in SUPPORTED_CRYPTO_ASSETS:
        if asset.matches_target_price(value):
            return asset.symbol
    return None


def symbol_from_event_ticker(value: object) -> str | None:
    text = str(value or "").upper()
    for asset in SUPPORTED_CRYPTO_ASSETS:
        if any(text.startswith(prefix) for prefix in asset.event_prefixes):
            return asset.symbol
    return None


def symbols_from_event_tickers(values: Iterable[object]) -> list[str]:
    symbols: list[str] = []
    for value in values:
        symbol = symbol_from_event_ticker(value)
        if symbol is not None:
            symbols.append(symbol)
    return symbols


def symbol_from_alias_text(text: str) -> str | None:
    normalized = text.lower()
    for asset in SUPPORTED_CRYPTO_ASSETS:
        for alias in asset.aliases:
            if re.search(rf"(^|[^a-z0-9]){re.escape(alias)}([^a-z0-9]|$)", normalized):
                return asset.symbol
    return None
