# Clean-room prediction-market benchmark

This package is independently implemented from public behavioral descriptions.
It does not contain PredictionMarketBench source code or bundled episode data.

Supported local inputs:

- Repository-owned synthetic JSON episodes
- User-owned JSON and CSV replay files
- User-owned Parquet files when installed with `.[benchmark]`
- Read-only SQLite forecast/ranking exports

The benchmark never submits exchange orders and does not write the trading
database. Simulated orders, fills, fees, positions, and equity exist only in
memory and report artifacts.
