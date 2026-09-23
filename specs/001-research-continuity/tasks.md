# Tasks: Research Continuity Store

**Feature**: `001-research-continuity` | **Branch**: `wt/research-store` | **Date**: 2026-09-22
**Input**: `specs/001-research-continuity/spec.md`, `plan.md`

**Note**: statuses below record work already built and merged as PR #1, re-expressed as Spec Kit
tasks so `speckit-converge` has a runnable target. A task is `[x]` only where the evidence column
names a command that was actually run.

## Phase 1: Setup

- [x] T001 Create the `skills/kalshi/scripts/tests/` layout and confirm stdlib `unittest` discovery works from the repo root
- [x] T002 Confirm the execution environment: `python3` 3.13.5, SQLite 3.46.1, no numpy/pandas/scipy, `sqlite3` binary absent, `execute_code` blocked in cron sessions per plan Constraints

## Phase 2: Foundation

- [x] T003 Implement `init` creating the ten-table schema and recording `schema_version` per FR-001 (evidence: `init` → `schema_version 1, 10 tables`)
- [x] T004 Implement profile-root store path resolution so the store never lands in cwd or `TMPDIR` per FR-012 (evidence: resolves to `<profile>/research/kalshi.sqlite`)
- [x] T005 Implement the STABLE/APPENDABLE/DERIVED classification taxonomy from plan D1 classification section
- [x] T006 Implement `cache_meta` store-revision bookkeeping so a run can compute "what changed since last run" per FR-006

## Phase 3: Ingestion (US2)

- [x] T007 Implement `ingest-settled` for a family's settled ladder, using the batched `GET /markets?status=settled&series_ticker=` path per plan D6
- [x] T008 Enforce idempotency on re-ingest so counts are unchanged for an identical payload per FR-002 (evidence: observations 164→164, settlements 22→22, markets 617→617)
- [x] T009 Implement `ingest-closes` for Yahoo daily closes, sourcing `asof` from `meta.regularMarketTime` and not the bar timestamp per plan D4 / FR-003
- [x] T010 Implement `ingest-quotes` sampling market-implied quotes with `asof` and `fetched_at` per FR-003
- [x] T011 Enforce non-null `asof` and `fetched_at` on all four provenance-bearing tables per SC-007 (evidence: PRAGMA not-null check passes)
- [x] T012 Implement band reconstruction from the settled ladder (max YES floor, min NO floor above → midpoint) per FR-004
- [x] T013 Capture Kalshi's own `expiration_value` as ground truth alongside the reconstructed band per plan D7

## Phase 4: Derivation & Digest (US1, US3, US4)

- [x] T014 Implement `fit` with the generic estimator registry (`realized_vol`, `diff_ols`, `conditional_hit_rate`, `calibration`), keeping family names out of the core per FR-011 / plan D3
- [x] T015 Persist `fit_date`, `n_obs`, and `inputs_rev` with every fit so drift is visible per FR-005 / US4
- [x] T016 Implement the stdlib Gaussian-elimination OLS estimator so fitting works with no third-party numeric stack per plan D2
- [x] T017 Implement `digest --since-last-run --max-lines N` with the five sections (expectation check, store contents, model drift, open market delta, unresolved/due) per FR-006 (evidence: 21 lines on a populated store, within the 60-line budget per SC-003)
- [x] T018 Implement graceful degradation on an absent or empty store — exit 0, report what is missing, fabricate nothing per FR-009 / SC-004 (evidence: empty store → "no prior prediction to score", exit 0)
- [x] T019 Implement `stale` with a `--json` form carrying a `by_class` breakdown per FR-007 / SC-006
- [x] T020 Implement `record-prediction --file` so a run leaves an expectation for its successor per FR-008
- [x] T021 Implement `resolve-predictions` to score ripe predictions and maintain the running ledger per FR-008
- [x] T022 Implement `series --tail N` to inspect a family's observation series

## Phase 5: Verification & Docs

- [x] T023 Write the 24-test stdlib suite covering schema, idempotency, band parity, digest bounds, staleness JSON, and provenance per Constitution II (evidence: `Ran 24 tests in 1.433s — OK`)
- [x] T024 Author `references/research-continuity.md` as the operator-facing reference for the store per Constitution VII
- [x] T025 Verify band-reconstruction parity against the settled print per SC-005 (evidence: 26SEP22 mid 6.5275 vs truth 6.5276; 26SEP21 mid 6.5125 vs 6.5107)
- [x] T026 Verify the batched-candlesticks path is materially faster than per-market calls per plan D6 (evidence: 1 call/0.06s vs 21 calls/2.8s)
- [x] T027 Independently re-verify the acceptance criteria under a different model lineage per Constitution IV (evidence: reviewer reproduced counts, parity, digest length, `n_obs 21→14` refit delta)

## Phase 6: Convergence Prerequisites

- [x] T028 Open PR #1 against `main` under the `wt/research-store` branch per Constitution III
- [x] T029 Merge PR #1 to `main` as the coordinator per Constitution V
- [ ] T030 Run `speckit-converge` against this feature directory to close the process gap recorded in the plan's Complexity Notes per Constitution IV

## Phase 7: Positions & realized P&L (schema v2, addendum 2026-09-23)

Card `t_4302ce7e`. Implements FR-013…FR-016. All rows written TDD (red first).

- [x] T031 Schema v2: `positions` table + nullable `predictions.position_id`, idempotent v1→v2 migration preserving every row per FR-013 (evidence: `test_v1_store_migrates_to_v2_preserving_rows` green; live-store copy migrates with identical counts, `schema_version` rows `[1,2]`)
- [x] T032 `record-position --file` with computed taker fee (round UP of `M * 0.07 * C * P * (1-P)`), run begin/touch, and `(market_ticker, side, opened_at)` idempotency per FR-014 (evidence: `test_record_position_computes_the_fee`, `test_record_position_is_idempotent` — 24.9 @ 0.19 → fee 0.27)
- [x] T033 Settlement in `resolve-predictions`: explicit `position_id` links always settle; ticker side-match only when `direction` implies a side (absent direction → explicit links only, FR-015); idempotent re-resolve (evidence: `test_absent_direction_settles_explicit_links_only`, `test_expected_sides_never_invent_a_side`, `test_reresolve_is_idempotent`)
- [x] T034 `pnl [--open]`: realized rows + totals, and a stored-quotes-only mark for open positions, no network per FR-016 (evidence: `test_pnl_totals`, `test_pnl_open_marks_from_stored_quotes`, `test_pnl_on_an_empty_store_degrades_gracefully`)
- [x] T035 Positions test suite green: 50 stdlib unittest tests, `python3 skills/kalshi/scripts/tests/test_research_store.py` → `Ran 50 tests in 3.250s — OK`
