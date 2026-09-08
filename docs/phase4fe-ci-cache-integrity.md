# Phase 4FE — CI Cache Integrity

Cache identity binds the exact lockfile set, interpreter identity, platform, schema bundle, and test configuration. A restored manifest is usable only when its identity exactly matches; absence is a normal miss, while any mismatch is rejected as stale or potentially poisoned. Both cases fall back to a clean install and full validation. The audit never restores or writes a cache itself.
