# Microstructure depth semantics

Newly computed depth payloads carry `depth_semantics: best_bid_quantity_v2`.
`top_of_book_depth` sums the quantities at the highest populated YES bid and
highest populated NO bid. Duplicate entries at the same best price are summed;
empty sides contribute zero. Level ordering does not affect the result.

`yes_bid_depth`, `no_bid_depth`, `total_depth`, and the existing all-level
`imbalance` retain their prior definitions. In particular, this change does not
turn the existing imbalance into a top-level imbalance or introduce a microprice.

The marker is retained in a feature's `raw_json.current_depth` and in newly
persisted depth snapshots' `raw_json`. Historical payloads without this marker
must retain their original interpretation: the old `top_of_book_depth` field
summed all levels. Do not relabel, overwrite, or combine those historical values
with version 2 values under one feature definition.

This change performs no migration and does not authorize rebuilding a frozen
campaign. New research using this quantity must declare version 2 before
collection and keep historical, development, and holdout cohorts separate.
