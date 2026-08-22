# Canonical Wrapper Inventory

Generated: `2026-08-22T01:46:07.813989+00:00`
Status: `READY`

## Lane totals

| Lane | Commands | Tables | Artifacts | Writers | Counters |
|---|---:|---:|---:|---:|---:|
| GH2_OPERATIONAL_SOAK | 9 | 9 | 6 | 1 | 2 |
| CURRENT_MARKET_RESEARCH | 337 | 55 | 6 | 3 | 4 |
| HISTORICAL_REPLAY | 51 | 53 | 3 | 2 | 3 |
| GUARDED_PAPER_LEARNING | 36 | 25 | 3 | 2 | 4 |

## Phase 3BB weather wrapper chain

| Command | Writer callable | Transaction | Writer semantics | Disposition |
|---|---|---|---|---|
| `phase3bb-r2-weather-fast-lane` | `write_phase3bb_r2_weather_fast_lane_report` | commit | current-research database writer | compatibility wrapper retained |
| `phase3bb-weather-fast-lane` | `write_phase3bb_weather_fast_lane_report` | none | read-only diagnostic/report writer | compatibility wrapper retained |
| `phase3bb-r44-weather-catalog-hook-runtime-verification` | `write_phase3bb_r44_weather_catalog_hook_runtime_verification_report` | rollback | read-only diagnostic/report writer | compatibility wrapper retained |
| `phase3bb-r45-weather-freshness-to-ranking-impact` | `write_phase3bb_r45_weather_freshness_to_ranking_impact_report` | rollback | read-only diagnostic/report writer | compatibility wrapper retained |
| `phase3bb-r46-cloud-scheduler-weather-writer-gate-repair` | `write_phase3bb_r46_cloud_scheduler_weather_writer_gate_repair_report` | rollback | conditional external scheduler writer | compatibility wrapper retained |
| `phase3bb-r47-weather-current-window-series-discovery-linkability-repair` | `write_phase3bb_r47_weather_current_window_series_discovery_report` | rollback | read-only diagnostic/report writer | compatibility wrapper retained |
| `phase3bb-r48-weather-feature-refresh-runtime-verification` | `write_phase3bb_r48_weather_feature_refresh_runtime_verification_report` | rollback | read-only diagnostic/report writer | compatibility wrapper retained |
| `phase3bb-r49-weather-missing-link-apply-after-feature-refresh` | `write_phase3bb_r49_weather_missing_link_apply_after_feature_refresh_report` | rollback | read-only diagnostic/report writer | compatibility wrapper retained |
| `phase3bb-r50-weather-post-link-ranking-fast-lane-recheck` | `write_phase3bb_r50_weather_post_link_ranking_fast_lane_recheck_report` | rollback | read-only diagnostic/report writer | compatibility wrapper retained |
| `phase3bb-r51-weather-ranking-path-repair` | `write_phase3bb_r51_weather_ranking_path_repair_report` | rollback | read-only diagnostic/report writer | compatibility wrapper retained |
| `phase3bb-r52-weather-ev-fair-value-diagnostic` | `write_phase3bb_r52_weather_ev_fair_value_diagnostic_report` | rollback | read-only diagnostic/report writer | compatibility wrapper retained |
| `phase3bb-r53-weather-current-window-cadence-preview-narrowing-repair` | `write_phase3bb_r53_weather_current_window_cadence_report` | rollback | read-only diagnostic/report writer | compatibility wrapper retained |
| `phase3bb-r54-weather-missing-link-apply-deferral` | `write_phase3bb_r54_weather_missing_link_apply_deferral_report` | rollback | read-only diagnostic/report writer | compatibility wrapper retained |
| `phase3bb-r55-weather-ranking-path-retry` | `write_phase3bb_r55_weather_ranking_path_retry_report` | rollback | read-only diagnostic/report writer | compatibility wrapper retained |
| `phase3bb-r57-weather-selected-window-pipeline-speed-repair` | `write_phase3bb_r57_weather_selected_window_pipeline_report` | rollback | read-only diagnostic/report writer | compatibility wrapper retained |
| `phase3bb-r58-weather-selected-window-forecast-feature-alignment-repair` | `write_phase3bb_r58_weather_selected_window_alignment_report` | rollback | read-only diagnostic/report writer | compatibility wrapper retained |
| `phase3bb-r59-weather-catalog-refresh-r57-retry` | `write_phase3bb_r59_weather_catalog_refresh_r57_retry_report` | rollback | read-only diagnostic/report writer | compatibility wrapper retained |
| `phase3bb-r60-weather-next-window-lead-time-scheduler-repair` | `write_phase3bb_r60_weather_next_window_lead_time_report` | rollback | read-only diagnostic/report writer | compatibility wrapper retained |
| `phase3bb-r42-weather-fast-lane-post-unblock-verification` | `write_phase3bb_r42_weather_fast_lane_post_unblock_report` | rollback | read-only diagnostic/report writer | compatibility wrapper retained |
| `phase3bb-r43-weather-catalog-scheduler-hook` | `write_phase3bb_r43_weather_catalog_scheduler_hook_report` | rollback | conditional external scheduler writer | compatibility wrapper retained |

## Consolidated same-lane helpers

- `kalshi_predictor.phase3bb_weather_common` owns check, first_line, legacy_check, mark_executable, stdout, tail, target_payload, write_legacy_probe_csv, write_probe_csv, write_rows_csv, write_sorted_rows_csv. Command names, writer callables, outputs, and transactions unchanged.
- `kalshi_predictor.current_research_common` owns crypto_candidate_sort_key, decode_list, format_cents, int_from_float_or_none, int_or_none, latest_crypto_v2_forecast, latest_market_snapshot, latest_risk_decisions_by_ticker, markdown_cell, read_json, read_json_required. Public commands and private compatibility aliases retain their prior call signatures and return types.
- `kalshi_predictor.historical_replay_common` owns has_usable_outcome, is_local_derived_composite_ticker, markdown_cell_empty, markdown_cell_none, normalize_result, settlement_to_y_true, source_is_closed_without_outcome, source_is_settled, trade_from_decision. Historical commands retain their public names; no current forecast, paper, or GH-2 writer imports this module.
- `kalshi_predictor.guarded_paper_common` owns int_or_zero. Learning and paper-readiness callers retain their private aliases; order, fill, sizing, risk, settlement, and P&L writers remain isolated.

## Remaining exact Current-Market Research duplicates

No exact helper-body duplicates remain in the scanned research families.

## Remaining exact Historical Replay duplicates

No exact helper-body duplicates remain in the scanned replay families.

## Guarded Paper eligible exact duplicates

No eligible pure helper duplicates remain after consolidation.

## Guarded Paper stateful duplicates kept isolated

- `73f29a5a7cad` (stateful paper identity or writer contract): `paper/ledger.py:_pending_position`, `position_sizing/service.py:_pending_position`

## Cross-lane exact matches not merged

- `paper_trading_gap.py:_read_json`, `phase3bb_r3_activation.py:_read_json`: Guarded Paper and Current-Market Research artifact readers have different lane ownership.

## Command consolidation gate

No Phase 3BB weather command was removed or aliased: each calls a distinct writer and retains a distinct artifact contract. Only byte-for-byte-equivalent same-lane helper implementations were consolidated.
