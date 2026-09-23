# Feature Specification: Research Continuity Store

**Feature Branch**: `wt/research-store`
**Created**: 2026-09-22
**Status**: Built (retro-specified — code merged as PR #1; this spec establishes the artifacts `speckit-converge` requires)
**Input**: User description: "This is very detailed. We'll want to make sure that day over day, it doesn't have to do all the research from scratch."

> **Scope note (human directive)**: this spec covers the continuity store only — the mechanism
> by which a scheduled research run accumulates state instead of re-deriving it. It does not
> prescribe which markets the run researches, nor the research methodology itself. Per-family
> and per-target add-ons are separate deliverables.

## Why This Exists (measured)

The 2026-09-22 gas/diesel research run took **33m53s, 96 API calls, and 94 tool calls**, of which
**37 were `write_file`** — hand-written throwaway scripts re-deriving facts the run had already
fetched (session `20260922_171603_2d8add`, source `oneshot`).

Two structural causes, both verified:

1. **No durable state.** Every invocation is a fresh session that re-derives the world from zero.
2. **Everything the run derived is re-fetchable in one call.** The expensive part was never the
   data; it was the *rediscovery of the data's shape*, repeated from scratch each time.

The value of this feature is therefore measurable as a reduction in wall clock and tool calls on
the *second* and subsequent runs of the same family.

## User Scenarios & Testing

### User Story 1 - A Run Knows What Its Predecessor Concluded (Priority: P1)

A scheduled research run begins by reading a digest of what changed since the last run. It sees
its predecessor's recorded expectation for an open market, the observations that have landed
since, and whether any prediction has become ripe for scoring. It does not re-derive any of it.

**Why this priority**: this is the entire feature. Without it, the store is write-only overhead.

**Independent Test**: run the digest against a store containing a prior prediction and confirm it
reports that prediction, the observation delta, and any newly-ripe items — without the caller
supplying any of those facts.

**Acceptance Scenarios**:

1. **Given** a store with a prior recorded prediction on an unresolved market, **When** the digest
   runs, **Then** it reports the prediction in an EXPECTATION CHECK section.
2. **Given** a store with a prior run, **When** the digest runs, **Then** it reports a non-zero
   observation/market row delta and a store revision greater than the recorded last-run revision.
3. **Given** an empty store, **When** the digest runs, **Then** it reports "no prior prediction to
   score" and zero rows, exits 0, and does **not** fail or fabricate history.

### User Story 2 - Observations Accumulate With Provenance (Priority: P1)

Ingesting the same settled ladder twice does not duplicate rows, and every ingested row carries
both an `asof` (when the fact was true in the world) and a `fetched_at` (when we learned it).

**Why this priority**: idempotency and provenance are what make the store trustworthy across days.
Without `asof`/`fetched_at` a stale figure is indistinguishable from a fresh one.

**Independent Test**: ingest a family's settled ladder, count rows, ingest the identical payload
again, count again — the deltas must be zero.

**Acceptance Scenarios**:

1. **Given** a settled ladder already ingested, **When** the identical payload is ingested again,
   **Then** the counts for `observations`, `settlements`, and `markets` are unchanged.
2. **Given** any row in `observations`, `settlements`, `markets`, or `quotes`, **When** it is read,
   **Then** both `asof` and `fetched_at` are non-null.
3. **Given** a market in a price band, **When** the band is reconstructed from the settled ladder,
   **Then** the reconstructed midpoint agrees with the settled print to within ±0.002.

### User Story 3 - Staleness Is Reported, Not Silently Tolerated (Priority: P2)

A run can ask which artifacts violate a freshness rule, and the answer is structured enough to act
on programmatically.

**Why this priority**: P2 — the digest works without it, but an unattended run needs a machine-
readable staleness signal to decide whether to re-fetch before reasoning.

**Independent Test**: ask for stale artifacts with `--json` and parse the result.

**Acceptance Scenarios**:

1. **Given** artifacts older than their freshness rule, **When** `stale --json` runs, **Then** the
   output is valid JSON containing a `by_class` breakdown.
2. **Given** a store where nothing is stale, **When** `stale --json` runs, **Then** it reports an
   empty set rather than erroring.

### User Story 4 - Model State Is Dated, So Drift Is Visible (Priority: P2)

A fitted model's parameters persist alongside the window and input revision that produced them, so
a later run can see the fit has drifted rather than silently reusing stale coefficients.

**Why this priority**: P2 — improves later-run quality, but the store is useful without it.

**Independent Test**: fit a model, refit with a narrowed window, confirm `n_obs` changes and the
prior fit remains readable.

**Acceptance Scenarios**:

1. **Given** a fitted model, **When** it is persisted, **Then** `fit_date`, `n_obs`, and
   `inputs_rev` are recorded with it.
2. **Given** a persisted fit, **When** the same family is refitted over a shorter window, **Then**
   the new fit reports a smaller `n_obs` and the digest reports the change.

### Edge Cases

- **Empty store**: every digest section degrades to a "nothing yet" line; exit 0. No invented
  history.
- **Pruned evidence**: the store must not depend on `TMPDIR` (pruned 24h after last write) — this
  already bit the design's own probe scripts, six of which were pruned before review.
- **Unattended execution**: `execute_code` is blocked and inline `python3 -c` is treated as a
  dangerous command in cron sessions, so the CLI must be a runnable script invoked normally.
- **No third-party packages**: the host `python3` is 3.13.5 with no numpy/pandas/scipy/statsmodels.
- **Collinear regressors**: a fit may legitimately fail on a short window (singular design matrix).
  That is a diagnostic to surface, not a crash to hide.
- **Corrupt or absent store**: the digest reports the store missing rather than raising, so the
  caller can proceed honestly.

## Requirements

### Functional Requirements

- **FR-001**: The system MUST create and patch a versioned schema, recording `schema_version`.
- **FR-002**: The system MUST ingest a family's settled ladder idempotently.
- **FR-003**: The system MUST ingest daily closes and market-implied quotes with `asof` and
  `fetched_at` provenance on every row.
- **FR-004**: The system MUST reconstruct a market's settled price band and midpoint from the
  settled ladder.
- **FR-005**: The system MUST fit and persist a named model with `fit_date`, `n_obs`, and
  `inputs_rev`.
- **FR-006**: The system MUST produce a digest of what changed since the last run, bounded by a
  caller-supplied line budget.
- **FR-007**: The system MUST report artifacts violating a freshness rule, with a structured
  (`--json`) form and a `by_class` breakdown.
- **FR-008**: The system MUST record a run's prediction and later score it once ripe.
- **FR-009**: The system MUST degrade gracefully on an absent, empty, or partially-populated store,
  exiting 0 and reporting what is missing.
- **FR-010**: The system MUST be stdlib-only and invoke without network access for read operations
  over an already-populated store.
- **FR-011**: Families MUST be opaque keys. No family-specific names, series codes, or market
  tickers may be hardcoded in the store's logic.
- **FR-012**: The system MUST resolve its store location under the owning profile's directory, not
  the current working directory and not `TMPDIR`.

### Key Entities

- **series**: a named family of related markets (opaque key).
- **markets**: an individual market, with ticker, series, and price band.
- **observations**: a point-in-time datum about the world, with `asof` and `fetched_at`.
- **settlements**: a resolved market's final print.
- **quotes**: a sampled market-implied price.
- **models**: a persisted fit with parameters, `fit_date`, `n_obs`, `inputs_rev`.
- **predictions**: a recorded expectation, later scored.
- **runs**: a research-run ledger entry.
- **cache_meta**: store revision and per-key freshness bookkeeping.

## Success Criteria

- **SC-001**: A second run of the same family performs measurably fewer tool calls than the 94-call
  baseline, with wall clock under the 33m53s baseline.
- **SC-002**: Re-ingesting an identical settled-ladder payload changes zero row counts.
- **SC-003**: The digest on a populated store is under 60 lines and fits the caller's budget.
- **SC-004**: The digest on an empty store exits 0 and reports "no prior prediction to score".
- **SC-005**: Band reconstruction agrees with the settled print to within ±0.002 (measured 6.5275
  vs 6.5276 on 26SEP22; 6.5125 vs 6.5107 on 26SEP21).
- **SC-006**: `stale --json` emits parseable JSON with a `by_class` key.
- **SC-007**: Every row in the four provenance-bearing tables has non-null `asof` and `fetched_at`.

## Assumptions

- The research run is scheduled and unattended, so graceful degradation matters more than loud
  failure.
- The store is single-writer; no concurrent-run locking is specified.
- Only read-only, keyless public market data is in scope (Constitution VI).

## Dependencies

- Kalshi public API (`https://api.elections.kalshi.com/trade-api/v2`) for settled ladders and quotes.
- Yahoo Finance for daily closes (`meta.regularMarketTime` is the `asof` source, not the bar
  timestamp).
