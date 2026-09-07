# Phase 4BW — Read-Only Profiler Harness

Phase 4BW profiles deterministic local computation using a hash-valid Phase 4BV synthetic fixture
pack. It records elapsed nanoseconds, peak traced bytes, output hashes, and whether each fixture
remains inside its declared offline regression envelope.

The profiler has no database adapter, exchange client, network dependency, subprocess invocation,
service control, or execution authority. Input and manifest hashes, fixture links, ordering, and
non-authorizing flags are validated before measurement. Invalid evidence fails closed. Results are
hash-protected and published atomically.

Envelope misses are reported as profiling results, never converted into trade authority. These
measurements characterize the local synthetic workload only; they are not a production SLO.
