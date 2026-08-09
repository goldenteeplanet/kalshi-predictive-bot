# kalshi-sdk Conformance Harness

This development-only harness compares the bot's local orderbook behavior with the typed models
from `kalshi-sdk` 10.0.0 at source commit
`a5ef152a9e0a266ade2cf73cef950825fe0421c1`.

Source: <https://github.com/TexasCoding/kalshi-python-sdk>

License: MIT, Copyright (c) 2026 Texas Coding.

## Boundary

- The dependency is optional and requires Python 3.12 or newer.
- Production modules do not import the SDK.
- No SDK client, authentication, order, account, or portfolio API is used.
- Fixtures must contain only public GET/HEAD material and are rejected if credential fields,
  private endpoints, or mutating methods appear.
- The included stream is synthetic protocol data and makes no historical or fill claim.
- No recordings are made by this repository.

## Verification

```bash
python -m pip install -e '.[conformance]'
python -m pytest tests/test_kalshi_sdk_conformance.py \
  tests/test_kalshi_client_rate_limit.py \
  tests/test_gh1_websocket_orderbooks.py -q
```

The existing client remains the runtime authority. SDK disagreement is reported as protocol drift;
it never silently rewrites data or enables an execution path.
