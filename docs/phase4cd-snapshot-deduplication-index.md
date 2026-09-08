# Phase 4CD — Snapshot Deduplication Index

Phase 4CD canonicalizes synthetic or copied order books into a deterministic in-memory content
index. A market snapshot is skipped only when its canonical content hash equals the latest indexed
hash and its sequence advances. New or changed content requests downstream recomputation.

Order-book levels are schema-validated, price-sorted, and hash-protected. Duplicate or regressing
sequences, duplicate price levels, malformed sides, impossible prices or quantities, tampered input,
and a simulated content-hash collision fail closed. The result updates only an artifact index and
does not access databases, collectors, services, networks, exchanges, or execution authority.
