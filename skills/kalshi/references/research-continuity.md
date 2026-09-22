# Research continuity store

How the `kalshi-bot` profile accumulates research across runs instead of
re-deriving it. This is the contract for `scripts/research_store.py` and for the
SQLite store it owns.

Design authority: card `t_367040dd` (`research-continuity-design.md`), approved
by independent review. Implementation: card `t_eb12306c`.

## The split

```
CODE  (this repo, versioned, PR'd)          skills/kalshi/scripts/research_store.py
DATA  (private to the profile, never git)   ~/.hermes/profiles/kalshi-bot/research/kalshi.sqlite
```

The store lives **under the profile root**. `TMPDIR` in a Hermes profile is
`<profile>/cache/scratch`, which is pruned 24 hours after the last write, so
nothing durable may live there — an evidence run's entire output evaporates.
`KALSHI_RESEARCH_DB` overrides the path; the default is the profile root above.

The bot is **read-only**. Every endpoint the store uses is public and
unauthenticated; nothing here signs a request or touches an account.

## The generic core

`family`, `series_ticker`, `kind`, `source` and `model_name` are **opaque
strings**. There is no market-family-specific column, default or branch in the
schema or the code. Gas, diesel, WTI and next month's shipping-count market all
use the same tables, and family behaviour (strike step, settlement timezone,
which source, which model, which lags) is *data supplied at ingest time*.

The one place the code reads structure out of a ticker is `_obs_date_for`, which
takes the print's own date from the event ticker's day stamp (`KXAAAGASD-26SEP22`
→ `2026-09-22`). That is Kalshi's own ticker convention, not a family
assumption, and it is load-bearing: converting a 03:59Z close into
`America/New_York` would file that ladder one day early.

## Schema — ten tables

`schema_version`, `series`, `observations`, `settlements`, `markets`, `quotes`,
`models`, `predictions`, `cache_meta`, `runs`.

| Table | Holds | Natural key (idempotent writes) |
|---|---|---|
| `series` | one row per tracked series; the family's opaque knobs | `series_ticker` |
| `observations` | the reconstructed/actual series (bands and points) | `(series_id, obs_date, source)` |
| `settlements` | one row per settled event, with the reconstructed band | `event_ticker` |
| `markets` | settled and open strikes, rules hash | `market_ticker` |
| `quotes` | market-implied price at a fixed offset before close | `(market_ticker, hours_before_close)` |
| `models` | **DERIVED** parameters, persisted so drift is queryable | `(series_id, model_name, fit_date)` |
| `predictions` | the bot's own expectation, and its later resolution | `id` |
| `cache_meta` | the freshness contract, plus the reserved `store_rev` | `key` |
| `runs` | the run ledger — what makes "since last run" a join | `id` |

Every write is `INSERT … ON CONFLICT(natural key) DO UPDATE`, so re-running a
fetch is free. `ingest-settled` additionally skips events already stored as
`finalized` unless `--rebuild` is passed.

Two sources describe the same reconstructed print and both are kept:
`kalshi_settlement` (the reconstructed band from settled strikes) and
`kalshi_expiration_value` (the exchange's own settlement print). The band is the
series of record; the exchange print is the ground truth the band is scored
against. `series_observations()` picks one row per date using `SOURCE_PRIORITY`,
so a family with both sources still yields one series.

## STABLE / APPENDABLE / DERIVED

**STABLE — cache forever; refetching is a bug.** Settled strike results and the
reconstructed band for a past date (final only when every strike has a result and
`status='finalized'`); the reconstructed series; past daily closes; historical
pre-close quotes — the bar before a close is immutable *and still fetchable after
settlement*, which is what makes calibration back-fillable rather than a live
capture race; the rules text of a settled market, stored as `rules_hash` (a hash
change on a settled market is an alert).

**APPENDABLE — new rows every run, idempotent by natural key.** Today's open
ladder; the newest daily closes; today's pre-close sample; the run's own
predictions; the `runs` row itself.

**DERIVED — never fetched; recomputed cheaply, parameters persisted.** Realized
vol, pass-through regression, the diesel OLS, state-conditional hit rates, the
calibration edge table. The rule: DERIVED objects are never stored as *values*.
What is stored is the parameter set plus `fit_date`, `n_obs` and `inputs_rev`, so
the next run prints
`diesel_ols resid_sd 0.01419 -> 0.01751 (+23.5%) R2 0.731 -> 0.680 n 37 -> 49`
instead of silently re-fitting. **Drift becomes visible, which is the goal.**

The estimator is chosen independently of the model name: `--model` is an opaque
label the caller owns, `--estimator` (or the model name if it matches one) is one
of `realized_vol`, `diff_ols`, `conditional_hit_rate`, `calibration`. That keeps
`diesel_ols` and `gas_ols` from needing two code paths.

`diff_ols` fits `y_t = a + b·Δprimary_{t-1} + Σ_c exog_pct_lag_k` on a *fixed
window* grid (lag 0 = the most recent exogenous change at or before the date,
lag k = k entries earlier in the exogenous series' own calendar). Every
regressor is knowable at `t-1`; feeding the current change into the AR column
would make the regressor equal the dependent and return `R² = 1`, `resid_sd = 0`.

## Freshness contract

Every cached row carries **two** timestamps, and they are not the same thing:

- **`asof`** — the source's own claim of currency (Yahoo `regularMarketTime`, a
  Kalshi `close_time`/`settlement_ts`, the print's date).
- **`fetched_at`** — when *we* pulled it.

Both are `NOT NULL` on every fetch-written table (`observations`, `settlements`,
`markets`, `quotes`). A missing `asof` is a bug, not a default — "a price with no
timestamp is worthless" is a schema constraint here, not a good intention.

| Class | Refetch rule |
|---|---|
| settled ladder, `close_ts < now` | **never**; revalidate only if `status != 'finalized'` |
| past daily close | **never** |
| historical pre-close quote | **never** once written — the capture window and offset are recorded |
| open ladder / live quotes | refetch when `fetched_at` older than **15 min**, and always immediately before a recommendation |
| today's partial close | refetch when the newest bar's date < the session date, or `fetched_at` older than **30 min** |
| DERIVED model | invalidated when any input row was fetched after `models.fit_date` |
| digest | regenerated on demand, carries `generated_at` and a `digest_hash` |

`research_store.py stale` prints every artifact whose rule is violated, and the
digest's second line reports the count, so a run that proceeds on stale data says
so out loud rather than assuming.

## The digest

```
research_store.py digest --since-last-run --max-lines 60
```

Printed at the **start** of every run, before any research decision. Five
sections, every number a query:

1. **EXPECTATION CHECK** — yesterday's prediction versus what actually happened,
   plus the running ledger. This is the section that requires a `predictions`
   table; without it "today differs from yesterday's expectation" is
   unexpressible.
2. **NEW SINCE LAST RUN** — row counts and the per-family recent prints.
3. **MODEL DRIFT** — refit deltas from `models` (`fit_date`, `n_obs`, `resid_sd`,
   `R²`, top parameter changes).
4. **OPEN MARKET DELTA** — open ladders, strike counts and the >5-point movers.
5. **UNRESOLVED / DUE** — pending predictions and imminent closes.

Budget is a hard cap (`--max-lines`, default 60). It is prompt-injected, so every
line is a permanent per-run token tax; ~30-60 lines is the whole point versus the
~59k tokens of tool output an unassisted run churns. Sections are trimmed
oldest-first when over budget, so a long gap degrades gracefully.

### Run boundaries

The digest *is* the run boundary. `digest` closes the open run and opens a new
one; `record-prediction` joins the open run. `--since-last-run` therefore means
"rows fetched since the previous run's own start" — which is exactly the previous
run's work. The previous run's `finished_at` is stamped with its own start, not
with the current clock: stamping it with "now" would set the next run's window
start at (or after) rows the previous run had already written and hide them.

`store_rev` is a global monotonic write counter; the digest prints
`store rev N (+delta)` where the delta is measured against the revision the
previous run *began* from, so it is that run's own growth rather than always
zero.

## Units — the scoring rule

`predictions.error` is scored in the units of `forecast_sd`, and the units are
never mixed:

- a prediction with a `point_forecast` and a stored print for the target date is
  scored in the **series' own units** (price), and the outcome is the print's
  direction;
- otherwise a `market_ticker` prediction is scored in **probability units**
  against the settled result.

`resolve-predictions` reports which happened. Z-scoring a probability error
against a price sd would be meaningless, so the digest only reports
"inside ±1 sd" over the price-unit predictions and reports the counts of each.

## Commands

```
research_store.py init
research_store.py ingest-settled --family KXDIESELD [--since DATE] [--kind K]
                                [--unit U] [--settlement-tz TZ]
                                [--strike-step X] [--rebuild] [--no-open]
research_store.py ingest-closes  --symbol HO=F [--symbol RB=F] [--since DATE]
research_store.py ingest-quotes  --family F [--backfill N] [--since DATE]
                                [--offset-hours 3]
research_store.py fit --family F --model NAME [--estimator E] [--exog S]
                      [--lags 0:4] [--as-of DATE]
research_store.py digest [--since-last-run] [--family F] [--max-lines 60]
research_store.py stale
research_store.py record-prediction --file pred.json
research_store.py resolve-predictions [--as-of DATE]
research_store.py series --family F [--tail 20]
```

Every subcommand takes `--json` (one JSON object on stdout) and none is
interactive. This matters: `execute_code` is blocked in unattended sessions and
inline `python3 -c`/heredocs are blocked as dangerous commands, which is why the
unassisted evidence run wrote 35 single-purpose scripts. A first-class runnable
CLI removes that friction rather than designing around it.

Two windowing notes:

- `ingest-quotes --backfill N` selects a **fixed** window (the N most recent
  settled events, default 1). It is not "the N most recent unquoted events", so
  an identical re-run is a no-op. Deepen the back-fill by raising N or by passing
  `--since`.
- `fit --as-of DATE` fits using only observations dated on or before DATE. That
  is what produces a second `models` row on a different `fit_date` and therefore
  a real before→after drift delta.

## Evidence and tests

`scripts/tests/test_research_store.py` is a stdlib-only, network-free unit suite
(the fetch layer is monkeypatched with fixed fixtures). It covers the schema and
the NOT NULL freshness contract, absence of family-specific columns, band
parity, ingest idempotency, the five digest sections and the line budget,
staleness detection, fit metadata and refit deltas, and run-boundary semantics.

Reconstruction accuracy is a property of the strike grid, not of the code: the
band midpoint is wrong by at most **half the strike step**, because a print
sitting exactly on a strike is indistinguishable from one just below it. Measured
over 51 diesel and 53 gas events, every event lands within `step/2` (0.0025 for a
0.005 grid), and the reconstructed series is bit-identical to the evidence run's
own `panel2.json`. The two headline checks from the design:

```
KXDIESELD-26SEP22  mid 6.5275  truth 6.5276  err -0.0001
KXDIESELD-26SEP21  mid 6.5125  truth 6.5107  err +0.0018
```

## Batch-endpoint traps

Three of them bite this store directly and are documented with curl examples in
[`api-endpoints.md`](./api-endpoints.md) (see the batched-candlesticks and
batched-orderbooks sections) — do not re-derive them:

- `/markets/candlesticks` takes `market_tickers` **comma-separated** and nests its
  response as `{"markets":[{market_ticker, candlesticks}]}`; parsing it flat
  yields a silent "0 bars".
- `/markets/orderbooks` is the **opposite**: `tickers` must be repeated, and a
  comma-separated list returns 200 with one bogus entry whose ticker is the
  literal `"A,B"`.
- A market's hourly history caps at ~316 bars, so a pre-close sample must be
  taken within roughly the last day — and the `close − 59 min` bar stays
  recoverable *after* settlement, which is what makes calibration back-fillable.

## What is deliberately not persisted

- the ~18 MB `/series` dump — a 600 s TTL cache in `kalshi.py`, not continuity,
  and already correctly in the pruned temp dir;
- raw HTML/PDF artifacts — refetch on demand, store the numbers and a hash, not
  the blobs;
- secrets, tokens, anything account-shaped.
