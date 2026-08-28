# Phase 4IL — Network and DNS evidence capture

Phase 4IL canonicalizes already-captured DNS, route, TCP, TLS, and HTTP probe metadata. Probe and target identities are hashed; raw hostnames, addresses, payloads, and responses are excluded.

The default limit is 64 probes within an inclusive time window; exact bounds pass. Empty or incomplete evidence is `PARTIAL`; out-of-window or excessive evidence is refused; duplicate probes, contradictory success/result codes, malformed data, or tampering fails closed.

The normalizer opens no socket and performs no DNS or HTTP request. It grants no recovery, service-control, WSL/Windows restart, order, or execution authority.
