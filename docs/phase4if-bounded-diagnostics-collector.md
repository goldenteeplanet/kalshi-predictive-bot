# Phase 4IF — Bounded diagnostics collector

Phase 4IF builds a bounded, deterministic envelope from already-captured diagnostic metadata. Samples contain redacted identifiers, source codes, timestamps, content hashes, sizes, line counts, and completeness/truncation markers—never raw output.

Defaults limit a bundle to 16 records, 256 KiB, 5,000 lines, and an inclusive collection window. Exact bounds pass. Excess, out-of-window, duplicate, malformed, or tampered evidence is refused; truncation or incomplete capture is explicitly `PARTIAL`.

The collector executes no command and reads or writes no external resource. A collected bundle grants no recovery, service-control, WSL/Windows restart, order, or execution authority.
