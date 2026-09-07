# Phase 4AZ — Long-chain checkpoint and retention safety

Phase 4AZ provides a generic append-only history for hash-protected Phase 4A artifacts. Deterministic
entry filenames bind sequence, UTC timestamp, and source hash. Every entry binds its predecessor;
each atomic checkpoint binds the prior checkpoint, chain head, retained manifest root, and retention
anchor. The manifest indexes all previously seen source hashes to prevent duplicates after pruning.

Bounded retention operates only inside a directory carrying a valid
`phase4az.disposable-history-directory.v1` marker. It deletes only old entry filenames that were
loaded from the immediately validated manifest; unrelated user files are untouched. A hash-protected
pruning proof records each pruned entry hash and the surviving anchor, preserving validation across
the retained boundary.

Validation detects duplicate sources, missing entries/checkpoints/pruning proofs, broken links,
sequence gaps, out-of-order timestamps, deterministic-filename drift, source or entry tampering,
manifest-root drift, and checkpoint/pruning tampering. Reconstruction emits a
`phase4az.tamper-evident-archive-bundle.v1` with retained hashes and cross-phase lineage hashes.

Focused tests cover valid long chains, checkpoint links, archive reconstruction, duplication,
missing links/files, tampering at every layer, chronology, safe retention, pruning anchors,
untracked-file preservation, invalid limits/markers, and the artifact-only static surface.

