# Phase 4IG — Process-tree evidence capture

Phase 4IG canonicalizes already-captured process-tree metadata. It retains only hashes for process, parent, and executable identity plus state, start time, completeness, and integrity data. Command lines, environment variables, raw PIDs, paths, and arguments are excluded.

Defaults limit a tree to 128 nodes and depth 16; exact bounds pass. Duplicate identities, cycles, or future nodes are `TAMPERED`; missing parents or incomplete nodes are `PARTIAL`; excessive depth or node count is refused.

The capture normalizer performs no live process enumeration or control. It grants no recovery, service-control, WSL/Windows restart, order, or execution authority.
