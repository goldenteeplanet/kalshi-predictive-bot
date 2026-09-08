# Phase 4FI — Reproducible CI Environment

The required safety workflow binds Python 3.11.9, exact action commit SHAs, and all declared direct/runtime development requirements through `requirements-ci.lock`. The audit hashes that lock, `pyproject.toml`, and the workflow into a single logical environment identity and refuses ranges, duplicate constraints, floating actions, or a mismatched interpreter. Transitive resolution remains a recorded residual risk until a hash-complete cross-platform lock is adopted; the complete logical test result remains the equivalence criterion.
