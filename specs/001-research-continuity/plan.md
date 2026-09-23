# Implementation Plan: Research Continuity Store

**Feature**: `001-research-continuity` | **Branch**: `wt/research-store` | **Date**: 2026-09-22
**Spec**: `specs/001-research-continuity/spec.md` | **Constitution**: `.specify/memory/constitution.md`

## Summary

A single stdlib-only Python CLI, `skills/kalshi/scripts/research_store.py`, backed by one SQLite
file under the owning profile's `research/` directory. It ingests Kalshi settled ladders and
quotes plus Yahoo daily closes into an idempotent, provenance-stamped schema; reconstructs price
bands; persists fitted models and predictions; and emits a bounded digest of what changed since
the last run.

The technical approach is deliberately unglamorous: SQLite + stdlib + a runnable script. That is
forced by the execution environment (see Constraints), not by preference.

## Technical Context

- **Language**: Python 3, stdlib only (`sqlite3`, `argparse`, `json`, `math`, `urllib.request`).
- **Storage**: one SQLite database, `kalshi.sqlite`, at `<profile>/research/`.
- **Runtime**: invoked as a script by an unattended scheduled run.
- **Concurrency**: single writer; no locking layer specified.
- **Target platform**: Linux, host `python3` 3.13.5, SQLite 3.46.1.

## Constitution Check

| Principle | Gate | Status |
|---|---|---|
| I. Spec-First | Spec exists before code | ⚠️ Retro-specified — code merged first (PR #1), this feature directory created after. See Complexity Notes. |
| II. TDD | Tests exist and are green | ✅ 24 stdlib tests, all passing |
| III. GitHub Flow | Work on `wt/*`, integrate via PR | ✅ `wt/research-store`, PR #1 |
| IV. Independent Review | Different lineage reviewer converged | ⚠️ Reviewer was independent lineage and re-verified 9 ACs, but ran no `speckit-converge` — impossible without this directory |
| V. Merge Authority | Only Jarvis merges | ✅ Merged by the coordinator |
| VI. Read-Only Public Data | No signing, no orders | ✅ Public unauthenticated endpoints only |
| VII. Artifacts Never In Git | Store not committed | ✅ Store lives under the profile; `.gitignore` covers transient caches |
| VIII. Evidence Over Assertion | Claims carry reproducible evidence | ✅ Reviewer independently reproduced counts and parity |

## Project Structure

```
skills/kalshi/
├── SKILL.md
├── references/
│   ├── api-endpoints.md
│   └── research-continuity.md          # operator-facing reference
└── scripts/
    ├── kalshi.py                       # the 11-subcommand market CLI (unchanged)
    ├── research_store.py               # THIS FEATURE
    └── tests/
        └── test_research_store.py      # 24 stdlib unittest tests
specs/001-research-continuity/
├── spec.md
├── plan.md
└── tasks.md
```

## Data Model

Ten tables. `schema_version` records the revision. `series` is the family key; families are
opaque strings — no series code or ticker is hardcoded anywhere in the logic (FR-011).

| Table | Role | Provenance |
|---|---|---|
| `series` | family registry | — |
| `markets` | individual markets + reconstructed band | `asof`, `fetched_at` |
| `observations` | world facts over time | `asof`, `fetched_at` |
| `settlements` | resolved market prints | `asof`, `fetched_at` |
| `quotes` | sampled market-implied prices | `asof`, `fetched_at` |
| `models` | persisted fits + `fit_date`, `n_obs`, `inputs_rev` | — |
| `predictions` | recorded expectations, later scored | — |
| `runs` | run ledger | — |
| `cache_meta` | `store_rev`, per-key freshness | `asof`, `fetched_at` |
| `schema_version` | schema revision | — |

### Classification taxonomy

- **STABLE** — facts that do not change once observed (a settled print). Cached, and safe to trust.
- **APPENDABLE** — grows monotonically (daily closes). Re-fetched by delta, not overwritten.
- **DERIVED** — computed from other rows with a dated fit. Carries `inputs_rev` so drift is visible.

## Key Design Decisions

**D1 — SQLite, not JSON/markdown notebooks.** A per-family JSON file (`KXDIESELD.json` +
`.md`) was the simpler option and was rejected because the digest must answer cross-family
questions ("what is ripe for scoring?") and because idempotent re-ingest needs a uniqueness
constraint, which a flat file cannot express cheaply.

**D2 — Stdlib-only OLS via Gaussian elimination.** The host `python3` has no numpy/pandas/
scipy/statsmodels, and `execute_code` is blocked in unattended sessions. Hand-rolled elimination
is the only option that runs. Cost: numerical robustness is ours to own.

**D3 — Families are opaque keys (FR-011).** The estimator is named generically (`diff_ols`, not
`diesel_ols`) so the core carries no genre-specific vocabulary. `--model` is free-form; when the
name matches an estimator, that estimator runs.

**D4 — `asof` comes from `regularMarketTime`, not the bar timestamp.** Verified: the last daily
bar's `timestamp` is the session *open* in exchange-local terms, so using it would attribute a
close to the wrong day. `meta.regularMarketTime` is the true observation time.

**D5 — Store under the profile root, never `TMPDIR`.** `TMPDIR` is
`~/.hermes/cache/scratch`, pruned 24h after last write. Several of this design's own probe
scripts were pruned before review — the argument for durability, demonstrated by accident.

**D6 — Batched candlesticks over per-market calls.** `GET /markets/candlesticks` with
comma-separated `market_tickers` returns a whole ladder in one call: 1 call / 0.06s vs 21 calls /
2.8s — 47× faster. Note it takes comma-separated tickers, the *opposite* of
`/markets/orderbooks`, which requires repeated `tickers` params.

**D7 — Reconstruct the band from the settled ladder, and also capture Kalshi's own settled
print.** The reconstruction (max YES floor, min NO floor above → midpoint) is the STABLE
derivation. The exchange's own `expiration_value` is captured alongside it as ground truth. Parity
was verified: 26SEP22 mid 6.5275 vs truth 6.5276; 26SEP21 mid 6.5125 vs 6.5107.

## Constraints & Environment Facts

- No `sqlite3` binary on PATH; `python3 -m sqlite3` works, SQLite 3.46.1.
- `execute_code` is blocked in unattended sessions; inline `python3 -c` and heredocs are blocked
  as dangerous commands → the CLI must be a runnable script invoked normally.
- `/series` is a single ~18MB unpaginated dump; client-side search with a 600s TTL cache.
- Only ~316 hourly bars are available per market, so a pre-close sample must be taken inside
  roughly the last day — which is why calibration is back-fillable only from the `t−59 min` bar.

## Complexity Notes

- **Retro-specification (Constitution I)**: the code was built and merged before a Spec Kit
  feature directory existed, because the design was carried as a Kanban attachment rather than
  through the SDD cycle. This plan and its siblings exist to make the converge gate runnable
  going forward. Constitution I is satisfied *from this point on*; the historical gap is recorded
  rather than papered over.
- **No convergence was run on PR #1 (Constitution IV)**: the reviewer independently verified nine
  acceptance criteria, but `speckit-converge` could not run — there was no `spec.md`, `plan.md`,
  or `tasks.md`. The first converge run against this feature directory is the compensating check.

## Verification Gates

- `python3 -m unittest discover -s skills/kalshi/scripts/tests` → 24 tests, green.
- Digest on a populated store → bounded by `--max-lines`, ≤60 lines.
- Digest on an empty store → exit 0, "no prior prediction to score".
- `stale --json` → parseable JSON with `by_class`.
- Re-ingest identical payload → zero row-count deltas.
