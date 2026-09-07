# Phase 4CB — Pagination Efficiency Audit

Phase 4CB analyzes hash-protected captured pagination fixtures. It publishes per-page and aggregate
utilization, duplicate and unique record counts, empty pages, request attempts, retry amplification,
cursor continuity, and stop-condition violations.

Cursor/sequence corruption, malformed records, impossible page sizes, invalid attempt counts, and
tampered inputs fail closed. Premature capture stops, pages after terminal cursors, and disagreement
between `server_has_more` and `next_cursor` remain measurable audit findings rather than being hidden.
The tool performs no API requests and does not alter collectors, databases, services, or execution
authority.
