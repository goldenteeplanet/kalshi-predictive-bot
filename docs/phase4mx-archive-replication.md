# Phase 4MX — Archive Replication and Corruption Repair

## Outcome

Phase 4MX implements a deterministic in-memory 2-of-3 XOR erasure-code model. Every shard is bound
to archive identity, generation, custody anchor, content digest, encoding parameters, index, and a
distinct placement domain. Any two valid shards reconstruct the exact original bytes.

## Repair boundary

Corruption, swapped metadata, insufficient sources, duplicate failure domains, stale generations,
and split-brain content refuse. Repair requires two independently placed valid shards, is
idempotent, discards prepared output after simulated crashes, and undergoes independent
post-repair reconstruction.

## Reproducible evidence

- complete Phase 4MP–4MX suite: 89 passed
- reconstructed payload bytes: 40
- content SHA-256: `dd46050422590e0fbeb6b60c197609bfcbd71029f6ef2ee6e776afbce2d16409`
- 2-of-3 reconstruction SHA-256: `13e40c2192273c629b64033e5d7fbbafc6c0fd15a9a173e658e05fdc77b92deb`
- deterministic repair SHA-256: `01f7a8bb8484495a6aba40aee4ea57e7989ad2874390b2e69411fd28565b6217`
- independent post-repair audit SHA-256: `b7267e4a139eefedfe9a7a380595a8a89af28013828e4b9c6649dda43d500858`

## Safety and removal

Encoding, reconstruction, and repair operate only on in-memory byte arrays. No filesystem,
network, runtime, service, acceptance, repair-execution, or order capability exists. Remove the
three phase files to roll back.

## Next phase

Phase 4MY — Replica placement policy, correlated-failure, and capacity-exhaustion proof.
