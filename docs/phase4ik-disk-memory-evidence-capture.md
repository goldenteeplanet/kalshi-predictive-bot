# Phase 4IK — Disk and memory evidence capture

Phase 4IK canonicalizes a captured disk, inode, memory, swap, and OOM-counter snapshot. Mount and snapshot identities are hashed; raw command output and mount paths are excluded. Derived capacity ratios use integer basis points for deterministic evidence.

Evidence is fresh through the exact 120-second endpoint. Zero required denominators or incomplete data is `PARTIAL`; stale evidence is `STALE`; future or arithmetically contradictory evidence is `TAMPERED`.

The normalizer performs no live filesystem, memory, or process inspection. It grants no recovery, service-control, WSL/Windows restart, order, or execution authority.
