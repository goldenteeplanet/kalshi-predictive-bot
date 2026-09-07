# Phase 4BV — Deterministic Performance Fixture Pack

Phase 4BV provides five synthetic, deterministic workloads for offline performance work. The pack
contains `small`, `medium`, `large`, `sparse`, and `burst` fixtures with fixed generation rules,
stable canonical hashes, deterministic filenames, and explicit elapsed-time and peak-memory
envelopes.

The fixtures contain no copied market data, credentials, production identifiers, database access,
exchange clients, or execution authority. Envelope values are regression ceilings for later offline
profiling, not claims about production latency and not permission to trade.

## Artifacts

- `phase4bv-{name}.json`: one synthetic fixture per workload class.
- `phase4bv-manifest.json`: ordered names, filenames, counts, hashes, and envelopes.
- `phase4bv-pack.json`: self-contained manifest and fixture set for validation and transport.

Every layer is hash-protected. Validation fails closed on an unknown, duplicate, missing, reordered,
tampered, shape-inconsistent, or authorizing fixture. Publication stages complete JSON documents,
flushes them, and atomically replaces their deterministic destinations.

## Offline use

```powershell
wsl.exe bash -lc "cd '/mnt/c/Users/user1/OneDrive/Documents/Dejoia Trading Bot/kalshi-predictive-bot' && PYTHONPATH=src /home/james/kalshi-runtime-src/.venv/bin/python scripts/local/phase4bv_performance_fixtures.py --output-directory /tmp/phase4bv"
```

Use only a disposable output directory. The command creates artifact files only and never reads or
writes either application database.
