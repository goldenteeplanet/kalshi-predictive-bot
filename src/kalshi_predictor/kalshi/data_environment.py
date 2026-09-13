"""Exact public endpoint identity; never infer environment from credentials."""

from urllib.parse import urlsplit


def endpoint_environment(url: str, *, websocket: bool = False) -> str:
    parsed = urlsplit(url)
    expected_scheme = "wss" if websocket else "https"
    expected_path = "/trade-api/ws/v2" if websocket else "/trade-api/v2"
    hosts = (
        {
            "external-api-ws.kalshi.com": "production",
            "api.elections.kalshi.com": "production",
            "external-api-ws.demo.kalshi.co": "demo",
            "demo-api.kalshi.co": "demo",
        }
        if websocket
        else {
            "external-api.kalshi.com": "production",
            "api.elections.kalshi.com": "production",
            "external-api.demo.kalshi.co": "demo",
            "demo-api.kalshi.co": "demo",
        }
    )
    if (
        parsed.scheme != expected_scheme
        or parsed.path.rstrip("/") != expected_path
        or parsed.hostname not in hosts
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 443)
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("UNVERIFIED_MARKET_DATA_ENDPOINT")
    return hosts[parsed.hostname]


def matched_environment(rest_url: str, websocket_url: str) -> str:
    rest = endpoint_environment(rest_url)
    if rest != endpoint_environment(websocket_url, websocket=True):
        raise ValueError("MARKET_DATA_ENVIRONMENT_MISMATCH")
    return rest
