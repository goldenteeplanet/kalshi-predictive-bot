# Phase 4IH — WSL status evidence capture

Phase 4IH canonicalizes already-captured WSL distribution metadata. It retains hashed distribution identity, lifecycle state, WSL version, default marker, timestamp, completeness, and integrity data. Distribution names and raw command output are excluded.

The default limit is 16 distributions and the exact limit passes. A complete capture requires at least one distribution and exactly one default. Empty, missing-default, or incomplete evidence is `PARTIAL`; duplicate identities, multiple defaults, future data, malformed data, or tampering fails closed.

The normalizer never invokes `wsl.exe`, changes distribution state, or shuts down WSL. It grants no recovery, service-control, host restart, order, or execution authority.
