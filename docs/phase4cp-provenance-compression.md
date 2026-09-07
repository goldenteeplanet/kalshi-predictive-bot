# Phase 4CP — Market-Data Provenance Compression

Phase 4CP replaces repeated provenance objects with canonical content-addressed references. The output embeds a dictionary keyed by each provenance object's canonical hash and preserves record order and payloads.

The compressor independently reconstructs all original records before returning a report. It requires exact equality and an identical canonical reconstruction hash; byte savings are merely measured and never substitute for reconstructability. Missing references, altered dictionary values, malformed compressed records, duplicate record identifiers, or report tampering fail closed.

Inputs are exact-schema, hash-protected, and bounded to 20,000 records. Reports are deterministic, canonically hash-protected, and atomically published. The phase changes no runtime representation and has no database, network, exchange, service-control, or production-writer capability.
